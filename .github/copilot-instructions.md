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
   parallel**, write the final `MIGRATION_RESULT.json`. `finish` also runs one
   validation iteration automatically.
4. `python scripts/migrate.py validate "<folder>"` — build/update the **single**
   `Output/<Model>/validation/<Model>_Validation.xlsx` (Summary, Visual Mapping,
   Measure Validation, Filter Validation with embedded side-by-side screenshots,
   Iterations). Re-run it each cycle: it appends an Iterations row and stops early
   (`status: stopped_early`) when the same unresolved issues repeat twice.
   **Screenshots are REAL captures, never synthetic.** The Tableau pane is the
   genuine worksheet/dashboard thumbnail Tableau Desktop embedded in the `.twb`
   (decoded with stdlib base64/zipfile and written as `<key>_tableau.png`). Note
   Tableau only stores thumbnails for a subset of sheets (dashboards + active
   sheets), so some visuals get a real Tableau image and others show a placeholder.
   The Power BI pane is a **real Power BI Desktop capture**: `validate` launches
   the produced `.pbip` in Power BI Desktop and screenshots each report page with
   the Win32 `PrintWindow` API (`PW_RENDERFULLCONTENT`), writing
   `page_<page>_powerbi.png` and assigning each page's capture to every visual/
   filter on it (`<key>_powerbi.png`). Page selection is deterministic — the
   emitter's `activePageName` in `pages.json` is set to each page in turn and the
   report is relaunched, since Power BI Desktop has no reliable keyboard/CLI page
   switch. Already-captured `page_*_powerbi.png` files are reused on the next run
   (delete them to force a fresh Desktop capture); set `PBI_CAPTURE=0` to skip
   capture entirely. User-supplied PNGs (either side) are never overwritten, so
   real full-res captures override the auto-captures. Keep the workbook **closed
   in Excel** while running `validate`; if it is open/locked the run writes a
   timestamped copy instead of updating in place.

## Hard rules

- **Never read raw `.twb` XML into context.** The parser already extracted everything
  into `analysis.json`. Read that.
- **Never hand-edit TMDL/PBIR files.** The emitters own those. If a visual or measure
  is wrong, fix the `agent-fragment.json` and re-run `finish`.
- The AI authors exactly **one** file per migration: `agent-fragment.json`
  (schema `scripts/contracts/fragment_schema.json`).
- A migration is **done** only when `MIGRATION_RESULT.json` has `status: "complete"`
  and every validator reports `0 error(s)`.
- **One validation workbook per model.** It is created once and updated in place every
  iteration — never start a second one. Stop iterating when `validate` reports
  `stopped_early`; document the root cause + manual action rather than looping.

## Setup (once)

```powershell
python -m pip install -r requirements.txt    # stdlib-only engine; this is optional tooling
```

The constitution files in `.specify/memory/` are the shared rulebook — read them,
never regenerate them.
