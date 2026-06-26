# Architecture

## Goal

One command — *"Migrate the report in `Data/<Folder>`"* — turns any Tableau workbook
into a validated Power BI `.pbip`, with three constraints:

1. **Correctness** — the output must open in Power BI Desktop with no errors.
2. **Speed** — any report in well under 5 minutes.
3. **Low AI cost** — spend tokens only where determinism cannot reach.

## The core idea: deterministic-first, AI only for the gaps

Everything that can be computed from the workbook is computed in Python. The AI is a
**narrow gap-filler**, not the driver. Concretely, the AI is asked for exactly one
artifact — `agent-fragment.json` — and only when the deterministic engine proves it is
needed. It never sees raw `.twb` XML and never writes TMDL or PBIR.

```
.twb / .twbx
   │
   ▼  (Python, deterministic)
 parse → analysis.json (IR)
 pre-translate safe DAX → dax-partial.json
 design single-flat schema (when unambiguous) → schema-easy.json
 classify the remainder → classification.json + agent-todo.json
   │
   ├── no hard gaps ─────────────► merge → emit (TMDL ∥ PBIR) → validate (∥) → .pbip   [ZERO AI]
   │
   └── hard gaps remain
            │
            ▼  (AI, ONE batched call)
        agent-fragment.json   (complex DAX + schema + visual types)
            │
            ▼  (Python, deterministic)
        merge → emit (TMDL ∥ PBIR) → validate (∥) → .pbip
```

A "hard gap" is anything that would make a *valid* emit impossible without judgment:
a measure whose DAX the safe translator refused (LOD `{FIXED/INCLUDE/EXCLUDE}`,
table-calcs `WINDOW_/RUNNING_/INDEX/RANK/LOOKUP`, multi-branch logic), or a
multi-table star schema that needs a fact/dimension/relationship decision. Ambiguous
chart types are *soft* gaps — they default to a table if unfilled, so they never block
a build, but the gap-fill agent resolves them for fidelity.

## Components

| Path | Role |
|---|---|
| `scripts/migrate.py` | **The orchestrator.** Single-command `run` / `finish` / `generate`; gap detection; parallel emit + validate; writes `MIGRATION_RESULT.json`. |
| `scripts/pipeline.py` | Lower-level `prepare` / `merge` / `generate` stages (kept for direct use). |
| `scripts/twb/` | Tableau XML → IR (`analysis.json`): datasources, columns, calc fields, parameters, worksheet marks, dashboard zones, RLS detection. |
| `scripts/dax/` | `map_dax.py` (7-handler fail-closed registry) + `dax_expr.py` (safe recursive Tableau→DAX). Translates the safe subset; refuses the rest. |
| `scripts/classify/` | Binary route every calc + the schema into *deterministic* vs *agent*; emits the self-contained `agent-todo.json`. |
| `scripts/schema/` | `star_det.py` — deterministic single-flat schema; multi-table routes to the agent. |
| `scripts/merge/` | `merge_decisions.py` — assemble fragments into the final `decisions.json` (deterministic; not the AI). |
| `scripts/emit/` | `emit_tmdl.py` (semantic model) + `emit_pbir.py` (report). The only writers of TMDL/PBIR. |
| `scripts/contracts/` | JSON Schemas for the IR, classification, fragment, and decisions artifacts. |
| `scripts/validate_semantics.py`, `emit/validate_bindings.py`, `plugins/pbip/.../validate_pbip.py`, `plugins/pbip/hooks/bin/tmdl-validate-*` | The four validation gates. |
| `scripts/validate/` | **Post-generate validation + single workbook.** Compares the IR against the emitted PBIR/TMDL (visual mapping, measure, filter), maintains the one `Output/<Model>/validation/<Model>_Validation.xlsx` across iterations with embedded side-by-side screenshot evidence (real Tableau thumbnails decoded from the `.twb`; Power BI panes are real Desktop captures dropped in by the user), and enforces early termination on repeated unresolved issues. Run via `migrate.py validate`. |
| `.github/agents/migrate-report.agent.md` | The orchestrator agent Copilot invokes. |
| `.github/agents/gap-fill.agent.md` | The single gap-fill agent (DAX + schema + visuals fused). |
| `.github/copilot-instructions.md` | Routes "migrate this report" to the orchestrator. |
| `.github/skills/`, `plugins/` | Knowledge skills (Tableau→DAX mapping, TMDL/PBIR/M rules) + validators. |
| `.specify/memory/` | The shared, read-only constitution (migration + report rules). |

## Where the parallelism is

The two emitters write to different folders (`*.SemanticModel/` vs `*.Report/`) and
share no state, so `migrate.py` runs them on a thread pool concurrently. The four
validators are read-only and also run concurrently after the artifacts exist. On real
fixtures the whole `finish` stage completes in ~1 second; the 5-minute budget is almost
entirely the single AI gap-fill call, which is itself one batched request rather than a
chain.

## How this improves on the four prototypes

This repo deliberately keeps the strongest piece of each prior attempt and drops the
rest.

- **From `Deterministic-Agentic` / `_New` / `speckit_solution`** — the mature
  deterministic engine (parser, DAX registry, TMDL/PBIR emitters, validators, golden
  tests). **Dropped:** the 14-stage spec-kit pipeline (`specify → clarify → plan →
  tasks → analyze`) and its ~20 agents. Those stages asked the same "what does this
  workbook do?" question in five different LLM calls. They are replaced by **two**
  agents (orchestrate + gap-fill) and **one** batched AI call.
- **From `Agentic-AI-Solutions`** — the idea of a tight, machine-readable feedback
  envelope (here, `MIGRATION_RESULT.json`) and the parse/emit separation. Its
  render-and-compare visual-fidelity loop (.NET PBIP→PNG + SSIM) is a strong optional
  add-on documented as future work below; the core path does not depend on it.
- **Sample-specific hacks removed** — the inherited `fix_*.py` scripts (hard-coded for
  particular Bain dashboards) were pruned; nothing in the core pipeline imported them.

### A concrete fix made here

`merge_decisions.py` previously hard-coded `visualDecisions: []` and
`fieldParameters: []`, silently discarding the agent's chart-type choices — so every
ambiguous worksheet fell back to a plain table. This repo carries those decisions
through to the emitter, so the agent's visual choices (bar / column / line / pie / map
/ scatter / slicer …) actually render. Verified on the Netflix fixture: 9 ambiguous
worksheets now emit 7 distinct visual types instead of all tables, with all four
validators still green.

## Contracts (the only interfaces that matter)

- `analysis.json` — the IR. Output of parsing; input to everything else.
- `agent-todo.json` — the self-contained gap list the AI reads.
- `agent-fragment.json` — the single artifact the AI writes (`fragment_schema.json`).
- `decisions.json` — the merged build spec the emitters consume (`decisions_schema.json`).
- `MIGRATION_RESULT.json` — the orchestrator's status envelope.

Because the fragment and decisions schemas share field names, merging is structural
concatenation, not translation — which is what makes the deterministic/agent handoff
robust.

## Future work (optional, non-blocking)

- **Visual-fidelity loop** — port the `Agentic-AI-Solutions` renderer (PBIP→PNG via
  headless Chromium) + `compare_pngs.py` SSIM diff as a post-generate verification
  stage that feeds pixel diffs back to the gap-fill agent.
- **Batch mode** — `migrate.py` already isolates per-report state under `Output/<Model>/`,
  so multiple workbooks can be migrated in parallel processes.
- **Richer deterministic DAX** — graduate common LOD/table-calc patterns from the agent
  into the `map_dax.py` registry as they prove stable, shrinking AI cost further.
