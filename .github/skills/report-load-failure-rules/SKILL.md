# Report Load-Failure Prevention Skill

## Purpose

The hard rules that prevent Power BI Desktop from crashing with "Failed to load the report". Single-responsibility companion to the report visual generation pipeline. Read this LAST as a final checklist before writing report files.

## When to Use

- During report visual generation, as a final validation gate before writing/handing off the report
- Whenever a generated report fails to open in Power BI Desktop

## Rule 1 — `themeCollection` MUST be empty `{}`

```json
// CORRECT (in definition/report.json)
"themeCollection": {}

// WRONG — TypeError: Cannot read properties of undefined (reading 'visual')
"themeCollection": {"baseTheme": {"name": "CY24SU06", "type": 2}}
```

**Why**: `ThemeServiceBase.getInheritParentColors` looks up visual styles in the referenced theme. If the theme can't be fully resolved, the lookup returns `undefined` and accessing `.visual` crashes.

## Rule 2 — Use Enhanced Folder Format (PBIR), NOT Legacy Flat

```
// CORRECT — per-visual folders
{ProjectName}.Report/definition/pages/ReportSection1/visuals/{name}/visual.json

// WRONG — legacy sections array → ThemeServiceBase crash
report.json with "sections": [{ "visualContainers": [...] }]
```

See **`report-pbir-folder-format`** for the full structure.

## Rule 3 — Visual Type Must Match Tableau Source Exactly

| Tableau Feature | CORRECT Power BI Visual | WRONG (never use) |
|---|---|---|
| Text table (rows only) | `tableEx` | clusteredBarChart |
| Cross-tab (rows + cols) | `pivotTable` | clusteredColumnChart |
| Area mark | `areaChart` | lineChart |
| Single KPI number | `card` | multiRowCard |
| Filter control | `slicer` | — |

The canonical mapping (with Automatic-mark inference) is in **`tableau-mark-mapping`**.

## Rule 4 — ALL Dashboards → ALL Pages

- Generate visuals for EVERY dashboard in the workbook. Each dashboard = one page. Do NOT skip dashboards or generate only the first page.

## Rule 5 — Strict visual.json Top-Level Properties

- `visual.json` root allows ONLY `$schema`, `name`, `position`, `visual`/`visualGroup`. No `filters`/`filterConfig`/`config`. See **`report-visual-json`**.

## Rule 6 — Schema Versions & `$schema` Presence

- `report.json` schema = `3.0.0`; `version.json` version = `2.0.0` (and MUST have `$schema`); `definition.pbir` version = `4.0`; `page.json` `displayOption` is a string; `pages.json` uses `pagesMetadata`.
- Every JSON entry file MUST contain `$schema`.

## Rule 7 — Encoding

- All files UTF-8 WITHOUT BOM (see `report-pbir-folder-format`).

## Rule 8 — Bookmark `display.mode` Enum — NEVER `"visible"`

- In `definition/bookmarks/{id}.bookmark.json`, the path
  `explorationState.sections.{page}.visualContainers.{visual}.singleVisual.display.mode`
  accepts ONLY `hidden`, `maximize`, `spotlight`, `elevation` (per `bookmark/1.4.0` → `VisualContainerDisplayMode`).
- `"mode": "visible"` → Desktop rejects the `.pbip`: *"JSON does not match any schemas from 'anyOf' … singleVisual.display.mode"*.
- To SHOW a visual in a bookmark, **OMIT its entry** from `visualContainers`. Each bookmark lists ONLY the visuals it HIDES.
- ⚠️ `tmdl-validate` and `validate_pbip.py` do NOT catch this — verify bookmark JSON manually. See `report-navigation-buttons`.

## Rule 9 — Projection `active` flag — NEVER `false` for Displayed Fields

- A projection with `"active": false` is dropped from the visual's query → the field does NOT render. The visual loads but shows BLANK data (title only).
- This silently breaks multi-field cards (value + % diff) and multi-column tables (name + measures) when only the first projection is `active: true`.
- RULE: set `"active": true` on EVERY displayed value/column projection in cards and tables. See **`report-visual-json`**.
- ⚠️ Validators do NOT catch this — only Power BI Desktop reveals the blank visual.

## Final Pre-Write Checklist

- [ ] `themeCollection` is `{}`
- [ ] Enhanced folder format (per-visual `visual.json`), no `sections` array
- [ ] Every Tableau dashboard has a page; counts match
- [ ] Every visual type matches its Tableau mark
- [ ] No extra top-level properties in any `visual.json`
- [ ] All `$schema` values present and correct versions
- [ ] All slicers have `title.show = false` (see `report-slicers`)
- [ ] All visuals have a border (see `report-borders-titles`)
- [ ] No visual overlaps; edge padding respected (see `report-layout-gapping`)
- [ ] No bookmark uses `display.mode: "visible"` — visible visuals are OMITTED (see Rule 8)
- [ ] Every displayed projection is `active: true` (no blank cards/tables — see Rule 9)
- [ ] All files UTF-8 without BOM
