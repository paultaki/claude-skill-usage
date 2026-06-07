---
name: skill-usage
description: Count how often each Claude skill is used AND recommend Keep / Modernize / Turn off / Prune for each. Parses ~/.claude transcripts for Skill-tool invocations and builds a sortable HTML decision dashboard (usage, token cost, action strip, kanban bucket board) across global + project + plugin skills; a Claude pass checks each skill against the user's CLAUDE.md and Anthropic's latest skill docs. Use when asked how often skills are used, which skills to keep, disable, delete, or modernize, skill usage or token cost, or to refresh the skill dashboard.
---

# skill-usage

Answers: **how often is each skill used, what does it cost, and should I keep / modernize / turn off / prune it?**

## How it works
The real record of skill use is the `Skill` tool_use events in `~/.claude/projects/**/*.jsonl`. The bundled script parses those (no LLM) for usage + token cost + a heuristic baseline verdict. A Claude recommendation pass then adds judgment. Everything renders to one local HTML dashboard. Nothing is uploaded.

## Two token costs (what "turn off" saves)
1. **Always-on tax** — every *enabled* skill's name+description sits in context every session, paid even if never invoked. Scales with the *number* of enabled skills.
2. **On-invoke cost** — when a skill fires, its full body loads once.

So the strongest turn-off candidates are skills you never use.

## The script
The collector is bundled with this skill at `scripts/skill-usage.py`. Resolve its absolute path from this skill's directory:
- installed as a plugin: `"$CLAUDE_PLUGIN_ROOT/skills/skill-usage/scripts/skill-usage.py"`
- dropped into `~/.claude/skills/` manually: `~/.claude/skills/skill-usage/scripts/skill-usage.py`

Outputs always go to `~/.claude/skill-usage/` (never next to the script), so they survive plugin updates.

## Run it (usage only — fast, deterministic)
```bash
python3 "<script-path>" --open
```
Flags: `--open` open the dashboard · `--prune` print the never-used prune list · `--rescan` ignore cache · `--demo` build a sample-data dashboard (no transcripts; writes `demo.html`). First run parses all transcripts; later runs only re-parse new/changed files. Token counts use `tiktoken` if installed, else a `chars/4` estimate (`pip install tiktoken` for a closer number).

This produces usage + token cost + a **heuristic baseline** verdict. For real recommendations, run the full pass below.

## Full recommendation pass (Claude)
Hybrid engine: the script collects deterministic data; this pass adds judgment and writes the persistent verdict store.
1. Run `python3 "<script-path>"` — writes `~/.claude/skill-usage/data/skill-usage.json` + `dashboard.html`.
2. Fetch the **current** Anthropic skill-authoring docs (search if the URL moved; do not hardcode a stale link). Distill a best-practice checklist → write `~/.claude/skill-usage/data/best-practices.md` with a `fetched: <date>` line.
3. Read `~/.claude/skill-usage/data/skill-usage.json`, the user's `~/.claude/CLAUDE.md` + `~/.claude/rules/**`, and each candidate `SKILL.md` (prioritize never-used + lint-flagged; confirm Keep+current in bulk).
4. Assign per skill `{verdict (keep|modernize|turn_off|prune), reason, modernize_note (modernize only), confidence}`:
   - off-stack vs the user's stack → **prune**
   - situational / maybe-later → **turn_off**
   - valuable but behind docs (weak description, bloated body, stale refs) → **modernize**
   - good + current → **keep**
   - genuinely uncertain → **keep / low-confidence** (never a confident cut)
5. Write `~/.claude/skill-usage/data/recommendations.json` (schema in `DESIGN.md` §4.2).
6. Run `python3 "<script-path>" --open` — re-renders, merging recommendations, and opens the dashboard.

## Acting on it
The dashboard is a decision surface. When the user says go, Claude executes — always confirm first, never auto-run, archive (not delete) by default:
- **modernize** → edit the user's own `SKILL.md` per `modernize_note` (never edit files inside plugin caches), then re-run the pass.
- **prune** → archive-move to `~/.claude/skills-archive/` (reversible).
- **turn off** → global: archive-move; plugin: print the `/plugin` disable steps.

## Outputs (all under ~/.claude/skill-usage/)
- `dashboard.html` — action strip (Keep/Modernize/Turn off/Prune + token reclaim) + kanban bucket board + sortable table
- `data/skill-usage.json` — usage + tokens + lint + baseline
- `data/recommendations.json` — persistent Claude verdicts (shareable; supports before/after)
- `data/best-practices.md` — snapshot of the Anthropic skill-doc checklist used
