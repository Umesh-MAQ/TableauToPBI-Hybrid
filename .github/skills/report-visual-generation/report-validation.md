# Report Validation Skill

## Purpose

Validate generated PBIR report artifacts for JSON schema compliance, data binding correctness, layout integrity, visual fidelity, and navigation button targets.

---

## Validation Layers

### Layer 1: JSON Schema Compliance

Every JSON/PBIR file must parse without errors AND contain required fields.

| File | Required Fields |
|---|---|
| `report.json` | `$schema` (3.0.0), `themeCollection` |
| `version.json` | `$schema` (1.0.0), `version` |
| `pages.json` | `$schema` (pagesMetadata/1.0.0), `pageOrder`, `activePageName` |
| `page.json` | `$schema` (page/2.0.0), `name`, `displayName`, `displayOption` |
| `visual.json` | `$schema` (visualContainer/2.4.0), `name`, `position`, `visual` or `visualGroup` |
| `definition.pbir` | `$schema`, `version`, `datasetReference` |

#### Forbidden Top-Level Properties in visual.json
- `filters` — REJECTED by PBI Desktop
- `filterConfig` — REJECTED by PBI Desktop
- `dataTransforms` — REJECTED by PBI Desktop

If ANY of these appear at the root level of visual.json, **remove immediately**.

#### Schema URL Validation
```
report.json     → .../report/definition/report/3.0.0/schema.json
version.json    → .../report/definition/versionMetadata/1.0.0/schema.json
pages.json      → .../report/definition/pagesMetadata/1.0.0/schema.json
page.json       → .../report/definition/page/2.0.0/schema.json
visual.json     → .../report/definition/visualContainer/2.4.0/schema.json
definition.pbir → .../report/definitionProperties/2.0.0/schema.json
```

---

### Layer 2: Data Binding Validation

Every `queryRef` in visual.json MUST reference a valid table/column/measure from the TMDL semantic model.

#### Validation Steps

1. **Collect all TMDL entities**: Parse `Output/{Name}/{Name}.SemanticModel/definition/tables/*.tmdl` to extract:
   - Table names
   - Column names per table
   - Measure names per table

2. **Collect all queryRefs**: From each visual.json, extract `queryRef` values from `query.queryState.*.projections[].queryRef`

3. **Format**: `queryRef` follows pattern `{TableName}.{ColumnOrMeasureName}`

4. **Validate**: Every queryRef must resolve:
   - `TableName` exists in TMDL
   - `ColumnOrMeasureName` exists as a column OR measure in that table

5. **Report errors**: List every unresolved queryRef with file path and visual name

#### Common Errors
- Typo in table name (case-sensitive!)
- Column was renamed during star-schema design but visual still uses old name
- Measure referenced by column syntax (`Column:` instead of `Measure:`)

---

### Layer 3: Layout Overlap Detection

Check every pair of visuals on the same page for overlapping bounding boxes.

#### Algorithm
```
For each page:
  visuals = load all visual.json position data
  For each pair (A, B) where A ≠ B:
    overlap_x = A.x < B.x + B.width AND B.x < A.x + A.width
    overlap_y = A.y < B.y + B.height AND B.y < A.y + A.height
    if overlap_x AND overlap_y:
      report "OVERLAP: {A.name} and {B.name} on page {page}"
```

#### Edge Padding Check
```
For each visual:
  if x < 25: report "LEFT PADDING violation: {name}"
  if y < 25: report "TOP PADDING violation: {name}"
  if x + width > canvas_width - 25: report "RIGHT PADDING violation: {name}"
  if y + height > canvas_height - 25: report "BOTTOM PADDING violation: {name}"
```

---

### Layer 4: Visual Fidelity Checks

Compare generated visuals against the Tableau source metadata in `tableau-visuals-output.md`.

| Check | Rule |
|---|---|
| Visual count | PBI page visual count ≥ Tableau dashboard visual count |
| Type mapping | Each Tableau mark type maps to expected PBI visualType (per visual-type-mapping.md) |
| Slicer title | `title.show` MUST be `false` for all slicer visuals |
| Border | All visuals MUST have `border.show = true` (unless navigation button) |
| Title text | Title text should reflect Tableau sheet title or be descriptive |
| Missing visuals | Flag any Tableau worksheet that has no corresponding PBI visual |

---

### Layer 5: Navigation Button Validation

| Check | Rule |
|---|---|
| Action placement | Every button action lives in `visualContainerObjects.visualLink` (never `objects.action`) |
| Page targets exist | Every `visualLink.navigationSection` value must match a `name` in some page.json |
| Button count | Number of nav buttons per page matches Tableau dashboard button count |
| Bookmarks generated | Each toggle button's `visualLink.bookmark` references a real bookmark in `definition/bookmarks/` (never empty); a Show/Hide pair exists with resolved `targetVisualNames` |
| Z-index | All button visuals have `z ≥ 1000` |
| Title disabled | All button visuals have `title.show = false` |

---

### Layer 6: Cross-Page Consistency

| Check | Rule |
|---|---|
| `pageOrder` complete | Every page folder name appears in `pages.json.pageOrder` |
| `activePageName` valid | `activePageName` matches one of the page names |
| No orphan pages | Every page folder in `pages/` has an entry in `pageOrder` |
| No missing pages | Every entry in `pageOrder` has a corresponding folder in `pages/` |
| Definition binding | `definition.pbir` → `datasetReference.byPath.path` points to existing `.SemanticModel` folder |

---

## Terminal Validation Commands

### Quick JSON Parse Check (all report files)
```powershell
Get-ChildItem "Output\{Name}\{Name}.Report" -Recurse -Include "*.json","*.pbir" | ForEach-Object {
  try { Get-Content $_.FullName -Raw | ConvertFrom-Json | Out-Null }
  catch { Write-Error "Invalid JSON: $($_.FullName) — $_" }
}
```

### Full PBIP Validator
```powershell
python "plugins\pbip\skills\pbip\scripts\validate_pbip.py" "Output\{Name}"
```

Exit codes: 0=clean, 1=warnings, 2=errors, 3=usage

### TMDL Syntax Check
```powershell
& "plugins\pbip\hooks\bin\tmdl-validate-windows-x64.exe" "Output\{Name}\{Name}.SemanticModel\definition"
```

---

## Error Priority

Fix errors in this order:
1. **JSON parse failures** — file is broken, nothing else works
2. **Forbidden top-level properties** — PBI Desktop will reject the file immediately
3. **Data binding mismatches** — visuals will show errors in PBI Desktop
4. **Layout overlaps** — visuals obscure each other
5. **Missing visuals** — Tableau content not migrated
6. **Fidelity issues** — wrong chart type or styling

---

## Validation Summary Template

After validation, produce a summary:

```markdown
## Validation Results: {WorkbookName}

### Schema Compliance
- ✅ All JSON files parse correctly
- ✅ All required fields present
- ❌ `visual_chart1/visual.json` has forbidden `filters` property (FIXED)

### Data Bindings
- ✅ 15/15 queryRefs resolve to valid TMDL entities

### Layout
- ✅ No overlaps detected
- ✅ All visuals within canvas bounds

### Visual Fidelity
- ✅ 8/8 visuals mapped to correct types
- ✅ All slicers have title disabled
- ✅ All visuals have border enabled

### Navigation
- ✅ 2 page navigation buttons target valid pages
- ✅ 1 toggle button wired to a generated Show/Hide bookmark pair

### Cross-Page
- ✅ pageOrder matches folder structure
- ✅ definition.pbir binding resolves
```
