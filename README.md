# Tableau → Power BI — Hybrid Migrator

Convert a Tableau workbook (`.twb` / `.twbx`) into a ready-to-open Power BI project
(`.pbip` = TMDL semantic model + PBIR report) from inside GitHub Copilot Chat.

> **Drop your report in a folder, tell Copilot "Migrate this report", and get a
> validated `.pbip` back — usually in seconds, with most workbooks costing little or
> no AI tokens.**

This repo is a **hybrid** of the best parts of four earlier prototypes: a proven,
deterministic Python engine does ~all the structural work, and the AI is called once
(if at all) to fill only the genuinely hard gaps. See [ARCHITECTURE.md](ARCHITECTURE.md)
for why it is built this way and how it compares to the prior solutions.

---

## How to use it (the 30-second version)

1. Put your workbook and its data files in a folder under `Data/`, e.g.:
   ```
   Data/
     Netflix/
       Netfix Workbook.twb
       netflix_titles.csv
   ```
2. Open this folder in VS Code with **GitHub Copilot** (agent mode).
3. In Copilot Chat, say:
   ```
   Migrate the report in Data/Netflix
   ```
4. Copilot runs the pipeline and tells you when the project is ready. Open it in
   **Power BI Desktop → Open → `Output/<Model>/<Model>.pbip`**.

That's it. Copilot uses the [`migrate-report`](.github/agents/migrate-report.agent.md)
agent, which orchestrates everything below.

---

## How to use it (the command line)

The agent just calls these — you can too.

```powershell
# 1) Deterministic pass: parse, pre-translate safe DAX, design schema, classify.
python scripts/migrate.py run "Data/Netflix"
```

This writes `Output/<Model>/MIGRATION_RESULT.json`:

- `status: "complete"` → a valid `.pbip` was produced **with zero AI cost**. Done.
- `status: "needs_agent"` → real gaps remain (complex DAX, a multi-table star schema,
  ambiguous chart types, or a measure the deterministic DAX sanitizer can't make
  Power-BI-safe). The engine wrote a self-contained `agent-todo.json`.

```powershell
# 2) The gap-fill agent reads agent-todo.json + analysis.json and writes ONE file,
#    Output/<Model>/agent-fragment.json. (In Copilot this is automatic.)

# 3) Finish: merge the fragment, emit model + report in parallel, validate in parallel.
python scripts/migrate.py finish "Data/Netflix"
```

A migration is **done** only when `MIGRATION_RESULT.json` shows `status: "complete"`
and every validator reports `0 error(s)`.

---

## What you get

```
Output/<Model>/
  <Model>.pbip                         ← open this in Power BI Desktop
  <Model>.SemanticModel/definition/    ← TMDL: tables, measures, relationships, M queries
  <Model>.Report/definition/           ← PBIR: pages + visuals
  analysis.json                        ← extracted IR (no raw XML needed downstream)
  decisions.json                       ← the merged build spec
  MIGRATION_RESULT.json                ← machine-readable status + validation report
```

Every project is checked by four validators before it is called complete:
`tmdl-validate` (TMDL syntax), `validate_bindings` (every visual field binds to a real
column/measure), `validate_pbip` (project structure), and `validate_semantics`
(reference/type/relationship integrity — the "won't open in Desktop" class of errors).

The static validators can't see one class of bug: DAX that is syntactically valid but
that Power BI Desktop rejects at **runtime** (e.g. a measure reference inside a
`CALCULATE` boolean filter — the yellow-warning measures). A deterministic DAX-safety
sanitizer auto-fixes the shapes it can (hoisting such refs into a `VAR`); anything it
can't fix is **escalated to the agent** rather than silently emitted. See *Why hybrid*
below.

---

## Setup

- **Python 3.9+** (the engine is standard-library only).
- **Power BI Desktop** (June 2024 or later) to open the `.pbip`.
- Optional: `pip install -r requirements.txt` for `jsonschema` (stricter contract
  validation) — the engine degrades gracefully without it.

The `tmdl-validate` binaries for Windows/macOS/Linux ship in
`plugins/pbip/hooks/bin/`; the pipeline picks the right one automatically.

Run the regression suite any time:

```powershell
python -m unittest discover -s scripts/tests
```

### Version control

The generated `Output/` is reproducible from the source `.twb`, so it is **git-ignored**
to keep pushes small. The one exception is `agent-fragment.json` (the agent-authored
gap-fill — measures, visual decisions); it is **kept under version control** because it
can't be reproduced without re-running the LLM. A fresh clone can therefore regenerate
every `.pbip` deterministically with `python scripts/migrate.py finish "Data/<Report>"`.

---

## Why "hybrid" and why it's fast / cheap

| Work | Who does it | Cost |
|---|---|---|
| Parse `.twb` XML → structured IR | Python | free |
| Translate safe DAX (SUM, ratios, IF/CASE, ATTR, passthrough) | Python | free |
| Single-flat schema design | Python | free |
| Emit TMDL + PBIR (in parallel) | Python | free |
| Validate (4 checks, in parallel) | Python | free |
| Complex DAX (LOD, table-calcs), star-schema design, ambiguous chart types | **AI, one batched call** | small |

Many workbooks finish on the deterministic path with **no AI call at all**. When the
AI is needed, it authors exactly one file (`agent-fragment.json`) — never raw XML,
TMDL, or PBIR. That is what keeps cost low and runtime well under the 5-minute target.

**Result first — escalate, don't guess.** The priority order is correctness → speed →
AI cost. Wherever the deterministic path can only *guess* (an unresolvable chart type,
a binding it can't place, a DAX shape it can't make Power-BI-safe), the work is routed
to the agent instead of shipping a degraded output. The escalation is enforced at a
single source-agnostic choke point, so it catches both deterministic **and**
agent-authored output before anything is emitted.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.
