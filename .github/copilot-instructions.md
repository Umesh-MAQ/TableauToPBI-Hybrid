# Tableau → Power BI — Hybrid Migration (Copilot Instructions)

This workspace migrates Tableau workbooks (`.twb` / `.twbx`) into ready-to-open
Power BI projects (`.pbip` = TMDL semantic model + PBIR report) using a **hybrid**
pipeline: a deterministic Python engine does the heavy lifting, and the AI only
fills the genuinely hard gaps. This keeps AI cost low and runtime under ~5 minutes.

## The one trigger you need

When the user says something like **"Migrate this report"**, **"Migrate the report
in `Data/<Folder>`"**, or **"Convert `<folder>` to Power BI"**, hand off to the
**`migrate-report`** agent. Do not do the migration inline — the orchestration,
parallelism, and validation all live in the agent + the Python engine.

```
runSubagent(migrate-report, "<folder the user pointed at, e.g. Data/Netflix>")
```

If the user did not name a folder, look under `Data/` for a folder containing a
`.twb`/`.twbx` and confirm which one they mean.

## How the pipeline works (so you can reason about it)

1. `python scripts/migrate.py run "<folder>"` — deterministic: parse the workbook,
   pre-translate the safe DAX, design the single-flat schema, classify the rest.
   It writes `Output/<Model>/MIGRATION_RESULT.json`:
   - `status: "complete"` → a valid `.pbip` was produced with **zero AI cost**. Done.
   - `status: "needs_agent"` → real gaps remain (complex DAX, multi-table star schema).
2. On `needs_agent`, the **`gap-fill`** agent reads the self-contained
   `Output/<Model>/agent-todo.json` (+ `analysis.json`) and writes **one**
   `agent-fragment.json`. This is the ONLY artifact the AI authors.
3. `python scripts/migrate.py finish "<folder>"` — deterministic: merge the fragment,
   emit the TMDL model and PBIR report **in parallel**, run all validators **in
   parallel**, write the final `MIGRATION_RESULT.json`.

## Hard rules

- **Never read raw `.twb` XML into context.** The parser already extracted everything
  into `analysis.json`. Read that.
- **Never hand-edit TMDL/PBIR files.** The emitters own those. If a visual or measure
  is wrong, fix the `agent-fragment.json` and re-run `finish`.
- The AI authors exactly **one** file per migration: `agent-fragment.json`
  (schema `scripts/contracts/fragment_schema.json`).
- A migration is **done** only when `MIGRATION_RESULT.json` has `status: "complete"`
  and every validator reports `0 error(s)`.

## Setup (once)

```powershell
python -m pip install -r requirements.txt    # stdlib-only engine; this is optional tooling
```

The constitution files in `.specify/memory/` are the shared rulebook — read them,
never regenerate them.
