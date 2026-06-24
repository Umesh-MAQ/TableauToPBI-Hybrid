# Tableau Visual Extraction Skill (Router)

## Purpose

Extract visualization metadata from a Tableau workbook (.twb) — chart types, field placements, encodings, formatting, filters, layout positions, and navigation buttons — needed to recreate equivalent Power BI visuals. This is a **thin router** — each rule area lives in its own focused skill so nothing is skipped. Read the focused skill for the step you are on.

## When to Use

- After `tableau-analysis` has run and the analysis output exists
- When migrating Tableau visuals/dashboards to the Power BI report layer
- Before generating Power BI report visuals

## ⚠️ Parse the Actual TWB XML

The high-level analysis output only has datasource metadata. Mark types, shelves, encodings, and zone positions MUST come from parsing the real `.twb` XML. Locate it via `file_search` with `Data/**/*.twb`.

## What to Read for Each Task (focused skills)

| Task | Read this skill |
|------|-----------------|
| Map a Tableau mark to a Power BI visualType (canonical, incl. Automatic inference) | **`.github/skills/tableau-mark-mapping/SKILL.md`** |
| Extract per-worksheet encodings (mark, rows/cols, color/size/text, combo, reference lines) | **`.github/skills/tableau-worksheet-extraction/SKILL.md`** |
| Extract dashboard layout (size, zones, navigation buttons, containers, title) | **`.github/skills/tableau-dashboard-extraction/SKILL.md`** |
| Translate format strings + write the extraction output document | **`.github/skills/tableau-format-translation/SKILL.md`** |

## Extraction Order

1. Locate the `.twb` and read the analysis output for worksheet/dashboard names.
2. For each worksheet: extract encodings -> `tableau-worksheet-extraction`; resolve chart type -> `tableau-mark-mapping`.
3. For each dashboard: extract layout + buttons -> `tableau-dashboard-extraction`.
4. Capture formats + write `.specify/memory/{WorkbookName}/tableau-visuals-output.md` -> `tableau-format-translation`.

## Completeness Gate (before handoff)

- Mark type for EVERY worksheet; color/size/text encodings where present; rows/cols captured.
- Dashboard zone positions for ALL dashboards; navigation buttons for ALL dashboards with `<button>`.
- Dual-axis/combo and reference/trend lines recorded per worksheet (value or `None`).
- Output file exists with the Visual Inventory table.

If any worksheet is missing mark/encoding data, **re-parse the TWB** before proceeding.

## Anti-Hallucination

- Extract only what the XML contains; write `None`/`Default` for empty categories.
- Copy format strings verbatim (after entity decoding); never rewrite or guess. Resolve `goto-sheet` targets to real dashboards.
