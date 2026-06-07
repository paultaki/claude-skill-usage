# skill-usage v2 — Design Spec

**Date:** 2026-06-06
**Status:** Approved (design), pre-implementation
**Owner:** Paul Takisaki
**Scope:** Evolve `skill-usage` from a usage/cost dashboard into a shareable **decision dashboard** with a recommendation engine that classifies every skill into Keep / Modernize / Turn off / Prune, including a check against Anthropic's current skill-authoring docs.

---

## 1. Problem & Goal

v1 answers *"how often is each skill used and what does it cost?"* (usage counts, last-used, days-ago, always-on token tax, never-used flags). It does **not** tell the user **what to do** about each skill.

v2 adds a recommendation layer that, per skill, says **Keep / Modernize / Turn off / Prune** with a one-line reason — and surfaces it in a clean, intuitive dashboard so the user can then tell Claude *"modernize these, prune those, turn off the rest."* It must:

- judge off-stack skills generically (*"you don't even do that type of code"*) by reading **the running user's own** `CLAUDE.md` / `~/.claude/rules`, not hardcoded assumptions;
- flag skills **behind current best practice** by checking against Anthropic's **latest** skill-authoring docs;
- persist its verdicts so they're shareable and support before/after comparison;
- ship as a self-contained, shareable skill folder.

## 2. Non-Goals (YAGNI)

- No live hook / daemon. The per-transcript cache already makes reruns near-instant.
- No auto-execution of destructive actions. Decision surface only; Claude executes on explicit instruction.
- No multi-user server / hosted dashboard. Local single-file HTML.
- No attempt to tokenize exactly for Claude. Estimates (tiktoken if present, else chars/4), clearly labeled.
- No grading of plugin *quality* we don't own — plugin skills get usage/turn-off treatment, but "Modernize" edits apply only to the user's own global/project skills (we don't edit files inside plugin caches).

## 3. Architecture — three isolated layers

```
┌─ Layer 1: Deterministic collector  (skill-usage.py, no LLM) ──────────────┐
│  • usage (transcripts) + tokens + inventory            [v1, done]          │
│  • mechanical LINT vs best-practices checklist          [new]              │
│  • cheap HEURISTIC baseline verdict per skill           [new]              │
│  → writes data/skill-usage.json                                            │
└───────────────────────────────────────────────────────────────────────────┘
            │ data/skill-usage.json
            v
┌─ Layer 2: Recommendation pass  (Claude, via SKILL.md workflow) ───────────┐
│  • WebFetch current Anthropic skill-authoring docs → distill checklist     │
│    → cache data/best-practices.md (with fetched-on date)                   │
│  • read skill-usage.json + each candidate SKILL.md                         │
│    + the USER'S CLAUDE.md / ~/.claude/rules + usage profile                │
│  • emit FINAL verdict per skill                                            │
│  → writes data/recommendations.json                                        │
└───────────────────────────────────────────────────────────────────────────┘
            │ data/recommendations.json
            v
┌─ Layer 3: Dashboard  (dashboard.html, read-only) ─────────────────────────┐
│  • merges skill-usage.json + recommendations.json                          │
│  • action strip + bucket board + Rec column/filter + existing usage view   │
└───────────────────────────────────────────────────────────────────────────┘
```

**Why this split:** counting/sizing/linting is deterministic and belongs in the script (fast, reproducible, offline). Judgment ("off-stack", "outdated garbage", "behind the docs") requires reasoning over the user's environment + live docs — that's Claude's job, run only when the user invokes the skill. The two meet through two JSON files, so each layer is independently testable and the dashboard renders whatever is present (usage-only if the recs pass hasn't run yet).

## 4. Data contracts

### 4.1 `data/skill-usage.json` (extend v1)
Each row gains:
- `lint`: `{ flags: string[], desc_ok: bool, body_ok: bool }` — mechanical findings (see §5).
- `baseline`: `"keep" | "modernize" | "turn_off" | "prune"` — cheap heuristic starting verdict.

### 4.2 `data/recommendations.json` (new — the persistent verdict store)
```json
{
  "evaluated_at": "2026-06-06T23:40:00Z",
  "docs_version": "claude-code/skills fetched 2026-06-06",
  "model": "claude-opus-4-8",
  "skills": {
    "<skill-id>": {
      "verdict": "keep | modernize | turn_off | prune",
      "reason": "one line, decision-enabling",
      "modernize_note": "what to fix (only for modernize)",
      "confidence": "high | medium | low",
      "source": "claude | heuristic"
    }
  }
}
```
Rows with no entry fall back to `baseline` (tagged `source:"heuristic"`) so the dashboard is never empty.

### 4.3 `data/best-practices.md` (new — cached doc checklist)
Distilled bullet checklist of current skill-authoring guidance + a `fetched: <date>` line. Refreshed by the Claude pass; used by both the deterministic lint (mechanical subset) and the judgment pass.

## 5. Deterministic lint (Layer 1, mechanical only)

Checks that need no judgment, derived from the best-practices checklist:
- `desc_missing` — no frontmatter `description`.
- `desc_short` / `desc_long` — outside a sane length band.
- `desc_no_trigger` — description lacks trigger phrasing ("use when", "when the user", etc.).
- `body_oversize` — SKILL.md body over a line/token threshold (candidate for progressive-disclosure split).
- `frontmatter_missing_field` — missing `name`/`description`.
- `deprecated_pattern` — regex hits for known-outdated conventions (list maintained in the script, seeded from docs).

Lint flags feed the heuristic baseline and give the Claude pass a head start; they are **signals, not verdicts**.

## 6. Heuristic baseline verdict (Layer 1)

Cheap rules so the dashboard is useful even before the LLM pass:
- used recently + few lint flags → `keep`
- used + (lint flags OR oversize) → `modernize`
- never used + situational/keep-ish → `turn_off`
- never used + (off-stack keyword OR strong lint) → `prune`

Off-stack keyword list is small and **overridable**; the authoritative off-stack call is made by Claude reading the user's CLAUDE.md.

## 7. Recommendation pass (Layer 2, Claude)

Workflow encoded in `SKILL.md`:
1. Run `skill-usage.py` (produces/refreshes `skill-usage.json`).
2. Fetch the **current** Anthropic skill-authoring docs (search if the URL moved; do not hardcode a stale link). Distill → `data/best-practices.md`.
3. Read the user's `~/.claude/CLAUDE.md` + `~/.claude/rules/**` (stack, workflow, do-not lists) and the used-skill profile from `skill-usage.json`.
4. For each skill (prioritize never-used + lint-flagged; Keep+current can be confirmed in bulk), assign `verdict` + `reason` (+ `modernize_note` for modernize) + `confidence`. Off-stack → `prune`; situational/maybe-later → `turn_off`; behind-docs-but-valuable → `modernize`; good+current → `keep`.
5. Write `data/recommendations.json`.
6. Re-render the dashboard (`skill-usage.py` merges recs on every render).

Low-confidence/uncertain skills stay `keep` with `confidence:"low"` and a "review" note rather than getting a confident cut — conservative by default.

## 8. Bucket taxonomy

| Verdict | Meaning | Action when user says go |
|---|---|---|
| **Keep** | used/valuable + current | none |
| **Modernize** | worth keeping, behind best-practice (weak desc, bloated body, stale refs) | Claude edits the SKILL.md per `modernize_note` (own skills only) |
| **Turn off** | situational, rarely/never used, may need later | disable: global → archive-move; plugin → `/plugin` steps |
| **Prune** | off-stack or outdated/low-quality with a better alternative | archive-move to `~/.claude/skills-archive/` (reversible) / delete |

## 9. Dashboard (Layer 3) — chosen layout

**Action strip + bucket board + Rec column** (keeps all v1 visuals):
- **Action strip:** four cards — Keep / Modernize / Turn off / Prune — each with count and token impact (e.g., "Prune 72 · reclaim ~5.0k tok"; Modernize shows "quality" not reclaim).
- **Bucket board:** kanban-style columns for the **actionable** buckets (Modernize / Turn off / Prune); each card = skill + one-line reason; Keep is collapsed by default. Capped/scrollable per column with a "+N more" affordance so 100+ skills don't explode the view.
- **Existing usage view retained:** stat cards, status donut, top token consumers.
- **Table:** gains a sortable **Rec** column (color-coded badge) + a Rec filter segment; reason shown inline/on the row; `modernize_note` visible on Modernize rows.
- All DOM built with `createElement`/`textContent` (no `innerHTML`); dependency-free; offline.

## 10. Execution model

Dashboard is read-only. The user reviews, then instructs Claude:
- *"modernize the Modernize bucket"* → Claude opens each own-skill, applies doc-aligned edits, re-runs the pass.
- *"prune"* → archive-move to `~/.claude/skills-archive/` (reversible).
- *"turn off"* → global archive-move; plugin → printed `/plugin` disable steps.

Nothing executes without an explicit instruction. Archive (not delete) is the default for reversibility.

## 11. Shareability / packaging

- Self-contained: `~/.claude/skills/skill-usage/` = `SKILL.md`, `scripts/skill-usage.py`, `DESIGN.md`, `README.md`, `data/` (gitignored runtime outputs).
- No Paul-specific hardcoding; all environment-specific judgment comes from the running user's `CLAUDE.md`/rules + live docs.
- `recommendations.json` persists so a teammate sees verdicts without re-running the LLM (and supports before/after screenshots).
- README documents: run command, flags (`--open`, `--prune`, `--rescan`), the two-step (script → Claude recs pass → re-render), and the tokenizer note.

## 12. Verification plan

- Script: `node --check`-equivalent isn't enough for the dashboard JS → **load `dashboard.html` in a real browser (chrome-devtools MCP), confirm zero console errors + screenshot** (this caught the `const top` global-collision bug in v1; browser-load is mandatory before "done").
- Recs pass: spot-check 5–10 verdicts against reality (off-stack calls match CLAUDE.md; modernize_notes are actionable).
- Idempotence: re-running with no transcript changes reuses cache and produces stable output.
- Empty-state: dashboard renders correctly when `recommendations.json` is absent (usage-only).

## 13. Open questions

None blocking. Future: optional `tiktoken` install for closer token estimates; optional feed of usage data into `skill-stocktake`'s quality pass.
