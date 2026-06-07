#!/usr/bin/env python3
"""
skill-usage.py — count how often each Claude Code skill is actually invoked,
and how many tokens each one costs.

GROUND TRUTH (no new instrumentation needed):
  Usage     -> ~/.claude/projects/**/*.jsonl   (every `Skill` tool_use is logged with a timestamp)
  Inventory -> ~/.claude/skills/*              (global skills, bare name)
               $PWD/.claude/skills/*           (project skills, if present)
               installed_plugins.json -> <installPath>/skills/*   (enabled plugin skills, name = plugin:skill)

TWO TOKEN COSTS:
  1. Always-on tax  -> every ENABLED skill's name+description sits in context every session,
                       paid even if never used. Scales with the NUMBER of enabled skills.
                       (column: tok_desc; the recurring bill.)
  2. On-invoke cost -> when a skill fires, its full SKILL.md body loads once.
                       (column: tok_body.)

Token counts use tiktoken (cl100k) if installed, else a chars/4 estimate. The dashboard
labels which one ran — the number is an estimate, never exact for Claude.

OUTPUTS:
  data/skill-usage.json   machine-readable
  dashboard.html          self-contained, sortable/filterable, with a donut + top consumers

CACHING: per-transcript by (mtime,size) in data/scan-cache.json -> reruns only parse new/changed files.

Usage:
  python3 skill-usage.py            # scan + write json + dashboard, print summary
  python3 skill-usage.py --open     # also open the dashboard
  python3 skill-usage.py --prune    # print the global/project never-used prune list (most tokens first)
  python3 skill-usage.py --no-scan  # reuse cached scan, just rebuild outputs
  python3 skill-usage.py --rescan   # ignore cache, full re-parse
"""
import json, os, glob, sys, subprocess
from datetime import datetime, timezone, timedelta

HOME = os.path.expanduser("~")
# Outputs go to a STABLE user dir, never next to this script — so it works whether installed
# as a plugin (script lives in the ephemeral plugin cache), dropped in ~/.claude/skills, or
# run from a clone. Override with SKILL_USAGE_OUT.
OUT_DIR = os.environ.get("SKILL_USAGE_OUT") or os.path.join(HOME, ".claude", "skill-usage")
DATA_DIR = os.path.join(OUT_DIR, "data")
CACHE_PATH = os.path.join(DATA_DIR, "scan-cache.json")
JSON_OUT = os.path.join(DATA_DIR, "skill-usage.json")
HTML_OUT = os.path.join(OUT_DIR, "dashboard.html")

PROJECTS_GLOB = os.path.join(HOME, ".claude", "projects", "**", "*.jsonl")
GLOBAL_SKILLS = os.path.join(HOME, ".claude", "skills")
INSTALLED_PLUGINS = os.path.join(HOME, ".claude", "plugins", "installed_plugins.json")

NOW = datetime.now(timezone.utc)
STALE_DAYS = 30
COOLING_DAYS = 14

# ---- tokenizer (tiktoken if available, else chars/4 estimate) ----
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    def est_tokens(t):
        return len(_ENC.encode(t or ""))
    TOKENIZER = "tiktoken cl100k (approx for Claude)"
except Exception:
    def est_tokens(t):
        return max(0, round(len(t or "") / 4))
    TOKENIZER = "chars/4 estimate"


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_skill_events(line):
    if '"name":"Skill"' not in line and '"name": "Skill"' not in line:
        return []
    try:
        o = json.loads(line)
    except Exception:
        return []
    ts = o.get("timestamp")
    msg = o.get("message", {})
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                nm = (b.get("input") or {}).get("skill")
                if nm:
                    out.append((ts, nm))
    return out


def scan_transcripts(use_cache=True, rescan=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = {}
    if use_cache and not rescan and os.path.isfile(CACHE_PATH):
        try:
            cache = json.load(open(CACHE_PATH))
        except Exception:
            cache = {}

    files = glob.glob(PROJECTS_GLOB, recursive=True)
    new_cache = {}
    parsed = reused = 0
    for fp in files:
        try:
            st = os.stat(fp)
        except OSError:
            continue
        sig = {"mtime": st.st_mtime, "size": st.st_size}
        prev = cache.get(fp)
        if prev and not rescan and prev.get("mtime") == sig["mtime"] and prev.get("size") == sig["size"]:
            new_cache[fp] = prev
            reused += 1
            continue
        hits = []
        try:
            for line in open(fp, errors="ignore"):
                for ts, nm in extract_skill_events(line):
                    hits.append([ts, nm])
        except Exception:
            pass
        sig["hits"] = hits
        new_cache[fp] = sig
        parsed += 1

    try:
        json.dump(new_cache, open(CACHE_PATH, "w"))
    except Exception:
        pass

    usage = {}
    for sig in new_cache.values():
        for ts, nm in sig.get("hits", []):
            usage.setdefault(nm, []).append(ts)
    return usage, {"files": len(files), "parsed": parsed, "reused": reused}


def read_frontmatter_and_body(path):
    """Return (description, body_text)."""
    desc = ""
    try:
        text = open(path, errors="ignore").read()
    except Exception:
        return desc, ""
    # frontmatter description (single-line)
    infm = False
    for l in text.splitlines():
        s = l.rstrip()
        if s.strip() == "---":
            if infm:
                break
            infm = True
            continue
        if infm and s.startswith("description:"):
            desc = s.split(":", 1)[1].strip().strip('"').strip("'")
            break
    return desc, text


def count_lines(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


# ---- lint thresholds (seeded from skill-authoring best practices) ----
DESC_MIN_CHARS = 40
DESC_MAX_CHARS = 1200
BODY_MAX_LINES = 500
TRIGGER_HINTS = ("use when", "use this when", "when the user", "when you", "trigger when", "activate")
DEPRECATED_PATTERNS = ()  # regex strings; seeded empty, filled from docs as needed


def lint_skill(name, desc, lines):
    """Mechanical, judgment-free checks. Returns {flags, desc_ok, body_ok}."""
    flags = []
    d = (desc or "").strip()
    desc_ok = True
    if not d:
        flags.append("desc_missing")
        desc_ok = False
    else:
        if len(d) < DESC_MIN_CHARS:
            flags.append("desc_short")
        if len(d) > DESC_MAX_CHARS:
            flags.append("desc_long")
        if not any(h in d.lower() for h in TRIGGER_HINTS):
            flags.append("desc_no_trigger")
    body_ok = True
    if lines and lines > BODY_MAX_LINES:
        flags.append("body_oversize")
        body_ok = False
    return {"flags": flags, "desc_ok": desc_ok, "body_ok": body_ok}


# Overridable; the AUTHORITATIVE off-stack call is made by the Claude pass reading the user's CLAUDE.md.
OFF_STACK_KEYWORDS = ("docker", "kubernetes", "tailwind")


def heuristic_baseline(count, days_ago, flags, source, text):
    """Cheap starting verdict so the dashboard is useful before the Claude pass runs."""
    t = (text or "").lower()
    off_stack = any(k in t for k in OFF_STACK_KEYWORDS)
    if count == 0:
        if off_stack or "deprecated_pattern" in flags:
            return "prune"
        return "turn_off"
    # Modernize means "edit the file" — only for skills we own (global/project), never plugins.
    if flags and source != "plugin":
        return "modernize"
    return "keep"


def build_inventory():
    """Return {skill_id: {source, plugin, lines, desc, tok_desc, tok_body, age_days, path}}."""
    inv = {}

    def add(sid, source, plugin, path):
        desc, body = read_frontmatter_and_body(path)
        try:
            age = (NOW.timestamp() - os.stat(path).st_mtime) / 86400.0
        except OSError:
            age = None
        lines_n = count_lines(body)
        inv[sid] = {
            "source": source, "plugin": plugin, "lines": lines_n,
            "desc": desc, "tok_desc": est_tokens(sid + ": " + desc),
            "tok_body": est_tokens(body), "age_days": round(age) if age is not None else None,
            "path": path, "lint": lint_skill(sid, desc, lines_n),
        }

    if os.path.isdir(GLOBAL_SKILLS):
        for d in sorted(os.listdir(GLOBAL_SKILLS)):
            md = os.path.join(GLOBAL_SKILLS, d, "SKILL.md")
            if os.path.isfile(md):
                add(d, "global", "", md)

    proj = os.path.join(os.getcwd(), ".claude", "skills")
    if os.path.isdir(proj):
        for d in sorted(os.listdir(proj)):
            md = os.path.join(proj, d, "SKILL.md")
            if os.path.isfile(md):
                add(d, "project", "", md)

    if os.path.isfile(INSTALLED_PLUGINS):
        try:
            data = json.load(open(INSTALLED_PLUGINS))
            for key, entries in (data.get("plugins") or {}).items():
                plugin = key.split("@")[0]
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    ip = e.get("installPath")
                    if not ip:
                        continue
                    sk_root = os.path.join(ip, "skills")
                    if not os.path.isdir(sk_root):
                        continue
                    for d in sorted(os.listdir(sk_root)):
                        md = os.path.join(sk_root, d, "SKILL.md")
                        if os.path.isfile(md):
                            add(f"{plugin}:{d}", "plugin", plugin, md)
        except Exception:
            pass
    return inv


def resolve(usage_name, inv):
    if usage_name in inv:
        return usage_name
    bare = usage_name.split(":")[-1]
    if bare in inv:
        return bare
    for sid in inv:
        if sid.endswith(":" + bare):
            return sid
    return None


def plugin_dup(basename, inv):
    """If a global/project bare skill is shadowed by a plugin skill of same basename, name it."""
    for sid, meta in inv.items():
        if meta["source"] == "plugin" and sid.endswith(":" + basename):
            return meta["plugin"]
    return None


def status_for(count, days):
    if count == 0:
        return "Never used"
    if days is None:
        return "Used"
    if days <= COOLING_DAYS:
        return "Active"
    if days <= STALE_DAYS:
        return "Cooling"
    return "Stale"


def build_rows(usage, inv):
    rows = []
    matched_ids = set()

    groups = {}
    for name, ts_list in usage.items():
        sid = resolve(name, inv)
        key = sid or name
        g = groups.setdefault(key, {"sid": sid, "names": set(), "ts": []})
        g["names"].add(name)
        g["ts"].extend(ts_list)

    def base_row(sid, meta):
        return {
            "tok_desc": meta["tok_desc"] if meta else None,
            "tok_body": meta["tok_body"] if meta else None,
            "lines": meta["lines"] if meta else None,
            "desc": meta["desc"] if meta else "",
            "source": meta["source"] if meta else "not-installed",
            "plugin": meta["plugin"] if meta else "",
            "lint": meta["lint"]["flags"] if meta else [],
        }

    for key, g in groups.items():
        sid = g["sid"]
        ts_list = g["ts"]
        dts = [d for d in (parse_ts(t) for t in ts_list if t) if d]
        last = max(dts) if dts else None
        first = min(dts) if dts else None
        days = (NOW - last).days if last else None
        meta = inv.get(sid) if sid else None
        if sid:
            matched_ids.add(sid)
        r = {"skill": sid or key, "invoked_as": ", ".join(sorted(g["names"])),
             "count": len(ts_list), "last": last.strftime("%Y-%m-%d") if last else None,
             "first": first.strftime("%Y-%m-%d") if first else None, "days_ago": days,
             "status": status_for(len(ts_list), days)}
        r.update(base_row(sid, meta))
        rows.append(r)

    for sid, meta in inv.items():
        if sid in matched_ids:
            continue
        r = {"skill": sid, "invoked_as": "", "count": 0, "last": None, "first": None,
             "days_ago": None, "status": "Never used"}
        r.update(base_row(sid, meta))
        rows.append(r)

    for r in rows:
        r["baseline"] = heuristic_baseline(
            r["count"], r["days_ago"], r.get("lint", []), r["source"],
            (r["skill"] + " " + (r.get("desc") or "")))

    rows.sort(key=lambda r: (-r["count"], r["days_ago"] if r["days_ago"] is not None else 99999))
    return rows


REC_PATH = os.path.join(DATA_DIR, "recommendations.json")
VERDICTS = ("keep", "modernize", "turn_off", "prune")


def load_recommendations():
    if os.path.isfile(REC_PATH):
        try:
            return json.load(open(REC_PATH))
        except Exception:
            return {}
    return {}


def merge_recommendations(rows, recs):
    """Set rec_* on each row from recs (source=claude) or fall back to baseline (source=heuristic)."""
    skills = (recs or {}).get("skills", {}) if isinstance(recs, dict) else {}
    for r in rows:
        rec = skills.get(r["skill"])
        if rec and rec.get("verdict") in VERDICTS:
            r["rec_verdict"] = rec["verdict"]
            r["rec_reason"] = rec.get("reason", "")
            r["rec_note"] = rec.get("modernize_note", "")
            r["rec_conf"] = rec.get("confidence", "")
            r["rec_source"] = "claude"
        else:
            r["rec_verdict"] = r.get("baseline", "keep")
            r["rec_reason"] = ""
            r["rec_note"] = ""
            r["rec_conf"] = ""
            r["rec_source"] = "heuristic"
    return rows


def rec_aggregates(rows):
    counts = {v: 0 for v in VERDICTS}
    token_impact = {"turn_off": 0, "prune": 0}
    for r in rows:
        v = r.get("rec_verdict", "keep")
        counts[v] = counts.get(v, 0) + 1
        if v in token_impact:
            token_impact[v] += r.get("tok_desc") or 0
    return {"counts": counts, "token_impact": token_impact}


def summarize(rows):
    installed = [r for r in rows if r["source"] != "not-installed"]
    used = [r for r in rows if r["count"] > 0]
    never = [r for r in installed if r["count"] == 0]
    stale = [r for r in used if r["status"] == "Stale"]

    def tok(rs):
        return sum(r["tok_desc"] or 0 for r in rs)

    tokens_by_status = {}
    for r in installed:
        tokens_by_status[r["status"]] = tokens_by_status.get(r["status"], 0) + (r["tok_desc"] or 0)

    agg = rec_aggregates(rows)

    return {
        "generated_at": NOW.strftime("%Y-%m-%d %H:%M UTC"),
        "tokenizer": TOKENIZER,
        "installed_total": len(installed),
        "ever_used": len(used),
        "never_used": len(never),
        "stale": len(stale),
        "total_invocations": sum(r["count"] for r in used),
        "turn_off_candidates": len(never) + len(stale),
        "always_on_tokens": tok(installed),
        "always_on_used": tok([r for r in installed if r["count"] > 0]),
        "always_on_wasted": tok(never),
        "tokens_by_status": tokens_by_status,
        "rec_counts": agg["counts"],
        "rec_token_impact": agg["token_impact"],
        "has_recs": any(r.get("rec_source") == "claude" for r in rows),
    }


# ---------- HTML ----------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Claude Skill Usage</title>
<style>
  :root{ --bg:#0e1116; --panel:#161b22; --line:#272e3a; --ink:#e6edf3; --mut:#8b949e;
         --accent:#4493f8; --good:#3fb950; --warn:#d29922; --bad:#f85149; --gray:#6e7681; --chip:#21262d; }
  *{box-sizing:border-box} html,body{margin:0}
  body{background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:28px 22px 60px}
  h1{font-size:20px;margin:0 0 2px} .sub{color:var(--mut);font-size:13px;margin:0 0 20px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .n{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
  .card .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
  .card.flag .n{color:var(--warn)}
  .recstrip{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
  @media(max-width:680px){.recstrip{grid-template-columns:repeat(2,1fr)}}
  .reccard{background:var(--panel);border:1px solid var(--line);border-left-width:4px;border-radius:10px;padding:12px 14px}
  .reccard .n{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums} .reccard .l{color:var(--mut);font-size:12px;margin-top:2px}
  .reccard.keep{border-left-color:var(--good)} .reccard.modernize{border-left-color:var(--accent)}
  .reccard.turn_off{border-left-color:var(--warn)} .reccard.prune{border-left-color:var(--bad)}
  .board{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
  @media(max-width:760px){.board{grid-template-columns:1fr}}
  .col{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
  .col h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px;display:flex;justify-content:space-between;color:var(--mut)}
  .col.modernize h3{color:var(--accent)} .col.turn_off h3{color:var(--warn)} .col.prune h3{color:var(--bad)}
  .skillcard{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:7px}
  .skillcard .t{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .skillcard .r{font-size:11.5px;color:var(--mut);margin-top:2px}
  .col .more{font-size:11.5px;color:var(--mut);text-align:center;padding-top:4px}
  .viz{display:grid;grid-template-columns:minmax(280px,1fr) minmax(280px,1.4fr);gap:14px;margin-bottom:18px}
  @media(max-width:760px){.viz{grid-template-columns:1fr}}
  .box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
  .box h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 12px}
  .donutwrap{display:flex;gap:18px;align-items:center}
  .donutbox{position:relative;width:150px;height:150px;flex:0 0 auto}
  .donut{position:absolute;inset:0;border-radius:50%;
    -webkit-mask:radial-gradient(circle 46px at 50% 50%, transparent 98%, #000 100%);
            mask:radial-gradient(circle 46px at 50% 50%, transparent 98%, #000 100%);}
  .donutlabel{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;pointer-events:none}
  .donutlabel .b{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}
  .donutlabel .s{font-size:9.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
  .dlegend{font-size:13px;display:flex;flex-direction:column;gap:7px}
  .dlegend .row{display:flex;align-items:center;gap:8px}
  .dot{width:11px;height:11px;border-radius:3px;flex:0 0 auto}
  .dlegend .v{margin-left:auto;color:var(--mut);font-variant-numeric:tabular-nums;padding-left:14px}
  .bars{display:flex;flex-direction:column;gap:8px}
  .bar{display:grid;grid-template-columns:160px 1fr 64px;align-items:center;gap:10px;font-size:12.5px}
  .bar .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar .track{background:var(--chip);border-radius:5px;height:14px;overflow:hidden}
  .bar .fill{height:100%;background:var(--accent);border-radius:5px}
  .bar .val{text-align:right;color:var(--mut);font-variant-numeric:tabular-nums}
  .legend{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:18px;color:var(--mut);font-size:12.5px}
  .legend b{color:var(--ink)}
  .controls{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  input[type=search]{background:var(--panel);border:1px solid var(--line);color:var(--ink);padding:8px 11px;border-radius:8px;min-width:240px;font-size:13px}
  .seg{display:flex;gap:4px;flex-wrap:wrap} .seg button{background:var(--chip);border:1px solid var(--line);color:var(--mut);padding:6px 10px;border-radius:7px;cursor:pointer;font-size:12px}
  .seg button.on{color:var(--ink);border-color:var(--accent)}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
  th.sorted::after{content:" \25be";color:var(--accent)} th.sorted.asc::after{content:" \25b4"}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  td.desc{white-space:normal;color:var(--mut);font-size:12px;max-width:380px}
  tr:hover td{background:#1b212b}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
  .b-ok{background:rgba(63,185,80,.15);color:var(--good)}
  .b-warn{background:rgba(210,153,34,.15);color:var(--warn)}
  .b-bad{background:rgba(248,81,73,.18);color:var(--bad)}
  .b-gray{background:rgba(110,118,129,.20);color:#adbac7}
  .b-info{background:rgba(68,147,248,.15);color:var(--accent)}
  .src{font-size:11px;color:var(--mut)}
  .candidate{color:var(--bad);font-weight:700}
  /* --- polish --- */
  .card,.reccard,.box,.col{box-shadow:0 1px 2px rgba(0,0,0,.25)}
  .card,.reccard,.skillcard,tr,.seg button,th{transition:background .15s ease,border-color .15s ease,transform .15s ease}
  .reccard:hover{transform:translateY(-1px)}
  .skillcard:hover{transform:translateY(-1px);border-color:var(--mut)}
  .skillcard .top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
  .skillcard .tk{color:var(--mut);font-variant-numeric:tabular-nums;font-size:11px;flex:0 0 auto}
  .col .empty{color:var(--mut);font-size:12px;text-align:center;padding:14px 0;opacity:.7}
  .tablewrap{overflow-x:auto}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
  .foot{margin-top:26px;padding-top:16px;border-top:1px solid var(--line);color:var(--mut);font-size:12px;text-align:center}
  @media (prefers-reduced-motion: reduce){*{transition:none !important;animation:none !important}}
  @media (max-width:560px){ body{padding:18px 14px 48px} h1{font-size:18px} .donutwrap{flex-direction:column;align-items:flex-start} }
  /* --- polish v2 (premium pass) --- */
  :root{--mut:#9aa5b2}
  body{background:radial-gradient(1100px 520px at 50% -8%, #141c27 0%, var(--bg) 58%) no-repeat var(--bg)}
  h1{font-size:clamp(20px,2.4vw,26px);font-weight:700;letter-spacing:-.02em}
  .card,.reccard,.box,.col{background:linear-gradient(180deg,rgba(255,255,255,.022),transparent),var(--panel);box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 2px 10px rgba(0,0,0,.28)}
  .card{transition:transform .15s ease,border-color .15s ease}
  .card:hover{transform:translateY(-1px);border-color:#33404f}
  .card .n{font-size:28px;letter-spacing:-.01em}
  .reccard .n{font-size:26px}
  .reccard.keep{background:linear-gradient(180deg,rgba(63,185,80,.07),transparent),var(--panel)}
  .reccard.modernize{background:linear-gradient(180deg,rgba(68,147,248,.07),transparent),var(--panel)}
  .reccard.turn_off{background:linear-gradient(180deg,rgba(210,153,34,.07),transparent),var(--panel)}
  .reccard.prune{background:linear-gradient(180deg,rgba(248,81,73,.07),transparent),var(--panel)}
  .donutlabel .b{font-size:20px}
  .seg button{min-height:32px} .seg button.on{background:rgba(68,147,248,.16);color:var(--ink);border-color:var(--accent)}
  thead th{box-shadow:0 1px 0 var(--line)} th:hover{color:var(--ink)}
  tbody tr:nth-child(even) td{background:rgba(255,255,255,.014)} tr:hover td{background:#1c232e}
  @media(max-width:560px){ .seg button{min-height:38px;padding:9px 13px} .card .n{font-size:24px} }
  /* --- polish v3 (interaction + craft) --- */
  .brand{display:inline-flex;align-items:center;gap:9px}
  .brand svg{width:22px;height:22px;flex:0 0 auto;color:var(--accent)}
  .reccard{cursor:pointer}
  .reccard:hover{border-color:#3a4654}
  th.num{text-align:right} th.sorted{color:var(--ink)}
  .bar .fill{background:linear-gradient(90deg,#3b86f0,#5aa6ff)}
  .bar .fill.unused{background:linear-gradient(90deg,#5b626c,#7d8694)}
  @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .cards,.recstrip,.viz,.board,.tablewrap{animation:rise .35s ease both}
  .recstrip{animation-delay:.04s} .viz{animation-delay:.08s} .board{animation-delay:.12s} .tablewrap{animation-delay:.16s}
  @media (prefers-reduced-motion: reduce){.cards,.recstrip,.viz,.board,.tablewrap{animation:none}}
</style></head><body>
<h1><span class="brand"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="3" y="13" width="4" height="8" rx="1"/><rect x="10" y="8" width="4" height="13" rx="1"/><rect x="17" y="4" width="4" height="17" rx="1"/></svg>Claude Skill Usage</span></h1>
<p class="sub">%%SUBTITLE%%</p>
<div class="cards" id="cards"></div>
<div class="recstrip" id="recstrip"></div>
<div class="viz">
  <div class="box"><h2>Always-on token tax by status</h2><div class="donutwrap"><div class="donutbox"><div class="donut" id="donut"></div><div class="donutlabel" id="donutlabel"></div></div><div class="dlegend" id="dlegend"></div></div></div>
  <div class="box"><h2>Top 10 description-token consumers (always-on)</h2><div class="bars" id="bars"></div></div>
</div>
<div class="board" id="board"></div>
<div class="legend" id="legend"></div>
<div class="controls">
  <input id="q" type="search" placeholder="Filter skills…"/>
  <div class="seg" id="seg">
    <button data-f="all" class="on">All</button>
    <button data-f="used">Used</button>
    <button data-f="candidates">Turn-off candidates</button>
    <button data-f="plugin">Plugins</button>
    <button data-f="global">Global</button>
    <button data-f="prune">Prune</button>
    <button data-f="turn_off">Turn off</button>
    <button data-f="modernize">Modernize</button>
    <button data-f="keep">Keep</button>
  </div>
</div>
<div class="tablewrap"><table id="t"><thead><tr>
  <th data-k="skill">Skill</th>
  <th data-k="source">Source</th>
  <th data-k="count" class="num sorted">Count</th>
  <th data-k="last">Last used</th>
  <th data-k="days_ago" class="num">Days ago</th>
  <th data-k="tok_desc" class="num">Tok (desc)</th>
  <th data-k="tok_body" class="num">Tok (body)</th>
  <th data-k="status">Status</th>
  <th data-k="rec_verdict">Rec</th>
  <th data-k="desc">Description</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<footer class="foot" id="foot"></footer>
<script>
const ROWS = %%DATA%%;
const SUM = %%SUMMARY%%;
const STALE = %%STALE%%;
const STATUS_CLASS = {"Active":"b-ok","Cooling":"b-warn","Stale":"b-bad","Never used":"b-gray","Used":"b-info"};
const STATUS_COLOR = {"Active":"#3fb950","Cooling":"#d29922","Stale":"#f85149","Never used":"#6e7681","Used":"#4493f8"};
const fmt = n => (n==null?"—":n.toLocaleString());
function el(tag, cls, text){ const e=document.createElement(tag); if(cls) e.className=cls; if(text!=null) e.textContent=text; return e; }

// cards
[["installed_total","Installed (enabled)"],["ever_used","Ever used"],["never_used","Never used",true],
 ["always_on_tokens","Always-on tokens / session"],["always_on_wasted","Wasted on never-used",true],
 ["total_invocations","Total invocations"]].forEach(c=>{
  const d=el("div","card"+(c[2]?" flag":"")); d.appendChild(el("div","n",fmt(SUM[c[0]]??0))); d.appendChild(el("div","l",c[1]));
  document.getElementById("cards").appendChild(d);
});

// recommendation action strip
const REC_LABEL = {keep:"Keep", modernize:"Modernize", turn_off:"Turn off", prune:"Prune"};
const REC_BADGE = {keep:"b-ok", modernize:"b-info", turn_off:"b-warn", prune:"b-bad"};
const rc = SUM.rec_counts||{keep:0,modernize:0,turn_off:0,prune:0};
const ri = SUM.rec_token_impact||{turn_off:0,prune:0};
[["keep","current"],["modernize","quality"],["turn_off","reclaim "+fmt(ri.turn_off)+" tok"],["prune","reclaim "+fmt(ri.prune)+" tok"]].forEach(pair=>{
  const k=pair[0], sub=pair[1];
  const c=el("div","reccard "+k); c.appendChild(el("div","n",fmt(rc[k]||0)));
  c.appendChild(el("div","l",REC_LABEL[k]+" · "+sub));
  c.tabIndex=0; c.setAttribute("role","button"); c.setAttribute("aria-label","Filter table to "+REC_LABEL[k]);
  const go=()=>{ applyFilter(k); document.getElementById("t").scrollIntoView({behavior:"smooth"}); };
  c.onclick=go; c.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){e.preventDefault();go();} };
  document.getElementById("recstrip").appendChild(c);
});

// kanban bucket board (actionable buckets only)
const BOARD=[["modernize","Modernize"],["turn_off","Turn off"],["prune","Prune"]];
const CAP=12;
BOARD.forEach(pair=>{
  const k=pair[0], label=pair[1];
  const items=ROWS.filter(r=>r.rec_verdict===k).sort((a,b)=>(b.tok_desc||0)-(a.tok_desc||0));
  const col=el("div","col "+k); const h=el("h3"); h.appendChild(el("span",null,label)); h.appendChild(el("span",null,String(items.length))); col.appendChild(h);
  if(items.length===0) col.appendChild(el("div","empty","Nothing here"));
  items.slice(0,CAP).forEach(r=>{ const sc=el("div","skillcard");
    const top=el("div","top"); top.appendChild(el("div","t",r.skill));
    if(r.tok_desc!=null) top.appendChild(el("div","tk",fmt(r.tok_desc)+" tok"));
    sc.appendChild(top);
    const reason = r.rec_reason || r.rec_note || ((r.lint&&r.lint.length)?r.lint.join(", "):"");
    if(reason) sc.appendChild(el("div","r",reason)); col.appendChild(sc); });
  if(items.length>CAP) col.appendChild(el("div","more","+"+(items.length-CAP)+" more (see table below)"));
  document.getElementById("board").appendChild(col);
});

// donut (conic-gradient) + legend
const order=["Active","Cooling","Stale","Used","Never used"];
const tbs=SUM.tokens_by_status||{};
const segs=order.filter(s=>tbs[s]>0).map(s=>({label:s,val:tbs[s],color:STATUS_COLOR[s]}));
const tot=segs.reduce((a,s)=>a+s.val,0)||1;
let acc=0, stops=[];
segs.forEach(s=>{ const a=acc/tot*100, b=(acc+s.val)/tot*100; stops.push(`${s.color} ${a}% ${b}%`); acc+=s.val; });
document.getElementById("donut").style.background = `conic-gradient(${stops.join(",")})`;
const dl=document.getElementById("dlegend");
segs.forEach(s=>{ const r=el("div","row"); const dot=el("span","dot"); dot.style.background=s.color; r.appendChild(dot);
  r.appendChild(el("span",null,s.label)); r.appendChild(el("span","v",fmt(s.val)+"  ("+Math.round(s.val/tot*100)+"%)")); dl.appendChild(r); });

// donut center = total always-on tokens
const dlab=document.getElementById("donutlabel");
dlab.appendChild(el("div","b",fmt(SUM.always_on_tokens||0)));
dlab.appendChild(el("div","s","always-on tok"));

// top consumers (by always-on description tokens)
const topc=[...ROWS].filter(r=>r.tok_desc!=null).sort((a,b)=>b.tok_desc-a.tok_desc).slice(0,10);
const maxT=topc.length?topc[0].tok_desc:1; const barsEl=document.getElementById("bars");
topc.forEach(r=>{ const b=el("div","bar"); b.appendChild(el("div","nm",r.skill));
  const tr=el("div","track"); const fl=el("div","fill"+(r.count>0?"":" unused")); fl.style.width=(r.tok_desc/maxT*100)+"%";
  tr.appendChild(fl); b.appendChild(tr);
  b.appendChild(el("div","val",fmt(r.tok_desc))); barsEl.appendChild(b); });

// legend text
const lg=document.getElementById("legend");
lg.appendChild(el("b",null,"Two token costs: "));
lg.appendChild(document.createTextNode("(1) Always-on tax (Tok desc) — every enabled skill's name+description sits in context every session, paid even if never used; scales with the number of enabled skills. (2) On-invoke cost (Tok body) — when a skill fires, its full body loads once. Turn-off candidates = Never used (gray, pure always-on waste) and Stale (>"+STALE+"d). Token counts are estimates."));

let sortK="count", asc=false, filter="all", q="";
function isCand(r){return r.status==="Never used"||r.status==="Stale";}
function pass(r){
  if(q && !(r.skill.toLowerCase().includes(q)||(r.desc||"").toLowerCase().includes(q))) return false;
  if(filter==="used") return r.count>0;
  if(filter==="candidates") return isCand(r);
  if(filter==="plugin") return r.source==="plugin";
  if(filter==="global") return r.source==="global";
  if(["keep","modernize","turn_off","prune"].includes(filter)) return r.rec_verdict===filter;
  return true;
}
function render(){
  let rows=ROWS.filter(pass);
  rows.sort((a,b)=>{ let x=a[sortK], y=b[sortK];
    if(x===null||x===undefined) x=(typeof y==="number")?-1:"";
    if(y===null||y===undefined) y=(typeof x==="number")?-1:"";
    if(typeof x==="string") return asc? x.localeCompare(y): y.localeCompare(x);
    return asc? x-y : y-x; });
  const tb=document.getElementById("tb"); tb.replaceChildren();
  rows.forEach(r=>{ const tr=el("tr");
    tr.appendChild(el("td", isCand(r)?"candidate":null, r.skill));
    tr.appendChild(el("td","src", r.source==="plugin" ? ("plugin · "+r.plugin) : r.source));
    tr.appendChild(el("td","num", String(r.count)));
    tr.appendChild(el("td", null, r.last||"—"));
    tr.appendChild(el("td","num", r.days_ago==null?"—":String(r.days_ago)));
    tr.appendChild(el("td","num", fmt(r.tok_desc)));
    tr.appendChild(el("td","num", fmt(r.tok_body)));
    const tds=el("td"); tds.appendChild(el("span","badge "+(STATUS_CLASS[r.status]||"b-info"), r.status)); tr.appendChild(tds);
    const rcell=el("td"); const rb=el("span","badge "+(REC_BADGE[r.rec_verdict]||"b-info"), REC_LABEL[r.rec_verdict]||r.rec_verdict);
    rb.title = r.rec_source==="heuristic" ? "heuristic baseline (run the recommendation pass for a Claude verdict)" : (r.rec_reason||"");
    rcell.appendChild(rb); tr.appendChild(rcell);
    tr.appendChild(el("td","desc", (r.desc||"").slice(0,180)));
    tb.appendChild(tr); });
  document.querySelectorAll("th").forEach(th=>{ th.classList.toggle("sorted", th.dataset.k===sortK); th.classList.toggle("asc", th.dataset.k===sortK && asc); });
}
document.querySelectorAll("th").forEach(th=>{ th.tabIndex=0; th.setAttribute("role","button");
  const doSort=()=>{ const k=th.dataset.k; if(k===sortK) asc=!asc; else {sortK=k; asc=false;} render(); };
  th.onclick=doSort; th.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){e.preventDefault();doSort();} }; });
document.getElementById("q").oninput=e=>{q=e.target.value.toLowerCase();render();};
function applyFilter(f){ filter=f; document.querySelectorAll("#seg button").forEach(x=>x.classList.toggle("on", x.dataset.f===f)); render(); }
document.querySelectorAll("#seg button").forEach(b=>b.onclick=()=>applyFilter(b.dataset.f));

const foot=document.getElementById("foot");
foot.appendChild(document.createTextNode("Generated locally by "));
const fa=el("a",null,"skill-usage"); fa.href="https://github.com/paultaki/claude-skill-usage"; foot.appendChild(fa);
foot.appendChild(document.createTextNode(" — your transcripts never leave your machine."));

render();
</script>
</body></html>"""


def render_html(rows, summary, scan_meta, out=None):
    out = out or HTML_OUT
    if summary.get("demo"):
        subtitle = "Sample data — a live demo of the skill-usage dashboard. Install it to see your own numbers."
    else:
        subtitle = (f"Generated {summary['generated_at']} · {scan_meta['files']} transcripts "
                    f"({scan_meta['parsed']} parsed, {scan_meta['reused']} cached) · tokens: {summary['tokenizer']}")
    html = HTML_TEMPLATE
    html = html.replace("%%DATA%%", json.dumps(rows))
    html = html.replace("%%SUMMARY%%", json.dumps(summary))
    html = html.replace("%%SUBTITLE%%", subtitle)
    html = html.replace("%%STALE%%", str(STALE_DAYS))
    open(out, "w").write(html)


def print_prune(rows, inv):
    cands = [r for r in rows if r["source"] in ("global", "project") and r["count"] == 0]
    cands.sort(key=lambda r: -(r["tok_desc"] or 0))
    total = sum(r["tok_desc"] or 0 for r in cands)
    print(f"\nPRUNE LIST — {len(cands)} never-used global/project skills "
          f"(~{total:,} always-on tokens/session reclaimable)\n")
    print(f"{'tok':>5} {'lines':>5} {'age_d':>5}  {'dup?':<16} skill")
    for r in cands:
        dup = plugin_dup(r["skill"].split(":")[-1], inv)
        print(f"{(r['tok_desc'] or 0):>5} {(r['lines'] or 0):>5} "
              f"{(inv.get(r['skill'],{}).get('age_days') if r['skill'] in inv else '-') or '-':>5}  "
              f"{('dup of '+dup) if dup else '':<16} {r['skill']}")


def print_summary(rows, summary, scan_meta):
    s = summary
    print(f"Scanned {scan_meta['files']} transcripts ({scan_meta['parsed']} parsed, {scan_meta['reused']} cached) | tokens: {s['tokenizer']}")
    print(f"Installed: {s['installed_total']} | Ever used: {s['ever_used']} | Never used: {s['never_used']} "
          f"| Invocations: {s['total_invocations']}")
    print(f"Always-on tokens/session: {s['always_on_tokens']:,}  "
          f"(used {s['always_on_used']:,} / wasted on never-used {s['always_on_wasted']:,})")
    print()
    print(f"{'count':>5} {'days':>5} {'tok_d':>5} {'tok_b':>6}  {'status':<10} skill")
    shown = 0
    for r in rows:
        if r["count"] == 0:
            continue
        print(f"{r['count']:>5} {str(r['days_ago']) if r['days_ago'] is not None else '-':>5} "
              f"{r['tok_desc'] if r['tok_desc'] is not None else '-':>5} "
              f"{r['tok_body'] if r['tok_body'] is not None else '-':>6}  {r['status']:<10} {r['skill']}")
        shown += 1
        if shown >= 20:
            break
    print(f"\nTurn-off candidates: {s['turn_off_candidates']}  (run with --prune for the global list)")
    print(f"Dashboard: {HTML_OUT}")


def open_in_browser(path):
    """Open a file in the default app, cross-platform."""
    import platform
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", path], check=False)
        elif system == "Windows":
            os.startfile(path)  # noqa: type-ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        pass


def demo_data():
    """Synthetic, realistic-but-fake dataset for a public demo dashboard (no transcripts, no private data)."""
    def r(skill, source, count, days, status, td, tb, verdict, reason, note="", plugin="", desc="", lint=None):
        last = (NOW - timedelta(days=days)).strftime("%Y-%m-%d") if days is not None else None
        return {
            "skill": skill, "source": source, "plugin": plugin, "count": count,
            "last": last, "days_ago": days, "status": status,
            "tok_desc": td, "tok_body": tb, "lines": max(1, tb // 6), "desc": desc,
            "lint": lint or [], "baseline": verdict,
            "rec_verdict": verdict, "rec_reason": reason, "rec_note": note,
            "rec_conf": "high", "rec_source": "claude",
        }
    rows = [
        r("pdf-processing", "plugin", 41, 0, "Active", 58, 2650, "keep", "most-used; core document workflow", plugin="docs", desc="Extract text and tables from PDFs."),
        r("git-commit-helper", "global", 27, 0, "Active", 61, 420, "keep", "used constantly; current", desc="Generate commit messages from diffs."),
        r("code-reviewer", "global", 19, 1, "Active", 120, 1400, "keep", "frequent, on-stack", desc="Review diffs for bugs and style."),
        r("api-scaffolder", "global", 14, 2, "Active", 80, 1100, "keep", "used; generates route handlers", desc="Scaffold REST endpoints."),
        r("supabase-helper", "global", 9, 0, "Active", 110, 2100, "keep", "on-stack database helper", desc="Supabase queries, RLS, migrations."),
        r("react-patterns", "plugin", 7, 3, "Active", 90, 1800, "keep", "used; on-stack", plugin="frontend", desc="React component patterns."),
        r("changelog-writer", "global", 6, 4, "Active", 54, 300, "keep", "used on releases", desc="Draft changelogs from commits."),
        r("excel-analyzer", "global", 8, 5, "Active", 70, 3300, "modernize", "used, but the body is 540 lines (>500)", "Split the pivot/chart reference into its own file.", desc="Analyze spreadsheets, pivots, charts.", lint=["body_oversize"]),
        r("slide-maker", "global", 5, 9, "Active", 44, 260, "modernize", "used, but the description has no trigger conditions", "Add 'Use when…' triggers so it's discoverable.", desc="Build slide decks.", lint=["desc_no_trigger"]),
        r("survey-summarizer", "global", 4, 18, "Cooling", 52, 210, "modernize", "used, but cites a stale API", "Refresh the cited endpoint and add triggers.", desc="Summarize survey results."),
        r("invoice-generator", "global", 0, None, "Never used", 96, 420, "turn_off", "situational; enable when you're billing"),
        r("crm-sync", "global", 0, None, "Never used", 88, 360, "turn_off", "only useful if you run a CRM"),
        r("jira-bridge", "plugin", 0, None, "Never used", 74, 300, "turn_off", "enable if you work in Jira", plugin="atlassian"),
        r("calendar-planner", "global", 0, None, "Never used", 60, 180, "turn_off", "situational scheduling helper"),
        r("email-digest", "global", 0, None, "Never used", 70, 240, "turn_off", "enable for inbox triage"),
        r("translation-helper", "global", 0, None, "Never used", 64, 200, "turn_off", "situational i18n helper"),
        r("data-cleaner", "plugin", 0, None, "Never used", 58, 260, "turn_off", "keep for the occasional ad-hoc cleanup", plugin="data"),
        r("jquery-helper", "global", 0, None, "Never used", 40, 520, "prune", "off-stack — you don't use jQuery", lint=["body_oversize"]),
        r("coffeescript-compiler", "global", 0, None, "Never used", 36, 300, "prune", "obsolete language; not your stack"),
        r("flash-exporter", "global", 0, None, "Never used", 34, 280, "prune", "Flash is dead — safe to remove"),
        r("ie6-polyfill", "global", 0, None, "Never used", 30, 210, "prune", "legacy browser shim; obsolete"),
        r("svn-bridge", "global", 0, None, "Never used", 38, 260, "prune", "you use git; redundant"),
        r("perl-formatter", "global", 0, None, "Never used", 33, 240, "prune", "off-stack language"),
        r("gulp-runner", "plugin", 0, None, "Never used", 42, 300, "prune", "superseded by your current bundler", plugin="legacy"),
    ]
    return rows, {"files": 0, "parsed": 0, "reused": 0}


def main():
    args = sys.argv[1:]
    no_scan = "--no-scan" in args
    rescan = "--rescan" in args
    do_open = "--open" in args
    do_prune = "--prune" in args
    do_demo = "--demo" in args

    if do_demo:
        rows, scan_meta = demo_data()
        summary = summarize(rows)
        summary["demo"] = True
        out = os.path.join(OUT_DIR, "demo.html")
        os.makedirs(OUT_DIR, exist_ok=True)
        render_html(rows, summary, scan_meta, out=out)
        print(f"Demo dashboard (sample data): {out}")
        if do_open:
            open_in_browser(out)
        return

    usage, scan_meta = scan_transcripts(use_cache=not rescan, rescan=rescan)
    inv = build_inventory()
    rows = build_rows(usage, inv)
    rows = merge_recommendations(rows, load_recommendations())
    summary = summarize(rows)
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump({"summary": summary, "scan_meta": scan_meta, "rows": rows}, open(JSON_OUT, "w"), indent=2)

    render_html(rows, summary, scan_meta)
    print_summary(rows, summary, scan_meta)
    if do_prune:
        print_prune(rows, inv)
    if do_open:
        open_in_browser(HTML_OUT)


if __name__ == "__main__":
    main()
