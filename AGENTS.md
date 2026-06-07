# AGENTS.md

Context for any AI coding agent working in this repo. (Claude Code reads `CLAUDE.md`, which imports this file.)

## What this is

A Claude Code plugin that reports **how often each Claude skill is actually used**, **what it costs in tokens**, and **what to do about it** (Keep / Modernize / Turn off / Prune). It reads the user's local `~/.claude` transcripts — nothing is uploaded.

## Architecture (hybrid — deterministic core + Claude judgment)

1. **Collector** — `skills/skill-usage/scripts/skill-usage.py`. Pure Python, no LLM. Parses transcripts for `Skill` tool-calls, sizes each skill, lints it against a best-practices checklist, assigns a cheap heuristic baseline verdict. Writes `skill-usage.json` + `dashboard.html`.
2. **Recommendation pass** — driven by `skills/skill-usage/SKILL.md`. Claude fetches Anthropic's current skill docs + reads the user's own `CLAUDE.md`, then writes real verdicts to `recommendations.json`.
3. **Dashboard** — generated `dashboard.html` (single file, vanilla JS). Merges the two; renders an action strip + kanban bucket board + sortable table.

## Layout

```
.claude-plugin/{plugin.json,marketplace.json}   manifests (this repo is plugin + marketplace)
skills/skill-usage/
  SKILL.md            the skill + the recommendation-pass workflow
  DESIGN.md           full design spec
  scripts/
    skill-usage.py    collector + dashboard renderer
    selftest.py       dependency-free assert tests for the pure functions
```

## Run / test

```bash
python3 skills/skill-usage/scripts/skill-usage.py --open   # build + open dashboard
python3 skills/skill-usage/scripts/selftest.py             # must print OK
```

## Hard rules (don't break these)

- **Outputs go to `~/.claude/skill-usage/`, never next to the script.** A plugin's own dir is ephemeral (wiped on update). Output location is `OUT_DIR` (env `SKILL_USAGE_OUT` overrides). Do not revert to writing beside `__file__`.
- **Dashboard JS uses `createElement`/`textContent`, never `innerHTML`.** It embeds JSON the user's skill descriptions flow into; `innerHTML` would be an injection risk and trips security hooks.
- **Always load `dashboard.html` in a real browser and confirm zero console errors before claiming it works.** `python -c`/`node --check` will NOT catch browser-runtime bugs (e.g. a `const top` collision with `window.top`). This is the required verification gate.
- **Pure functions (`lint_skill`, `heuristic_baseline`, `merge_recommendations`, `rec_aggregates`) are covered by `selftest.py`.** Add a case when you change their behavior.
- **stdlib only** (optional `tiktoken` if present; otherwise a `chars/4` estimate). Don't add dependencies.
- **Token counts are estimates** — never present them as exact.
- **Recommendations are advisory.** The tool never disables or deletes anything on its own; archive (not delete) is the default when a user acts.
