# Report Visual Generation Skill

## Purpose

Generate Power BI report visuals in PBIR Enhanced Folder Format from extracted Tableau visual metadata and report constitution rules.

## Sub-Skills (READ before generating)

| Sub-Skill | File | Purpose |
|-----------|------|---------|
| Visual Type Mapping | `visual-type-mapping.md` | Tableau mark → PBI visualType mapping, orientation, ambiguous cases |
| PBIR Templates | `pbir-templates.md` | Ready-to-use JSON templates for each visual type |
| Navigation Buttons | `navigation-buttons.md` | Button extraction, page nav + bookmark toggle templates |
| Layout Calculation | `layout-calculation.md` | Coordinate scaling, gap enforcement, minimum heights |
| Report Validation | `report-validation.md` | Schema, binding, layout, fidelity, navigation checks |

All sub-skills are in `.github/skills/report-visual-generation/`.

## When to Use

- After `tableau-visual-extraction` has produced `.specify/memory/{WorkbookName}/tableau-visuals-output.md`
- After `report-constitution.md` has been read (universal rules)
- When generating actual Power BI report visuals from the migration pipeline

## Instructions

### Step 1: Read Context

1. Read `.specify/memory/{WorkbookName}/tableau-visuals-output.md` — visual inventory, positions, chart types
2. Read `.specify/memory/report-constitution.md` — universal layout rules
3. Read TMDL files from `Output/{WorkbookName}/{ModelName}.SemanticModel/definition/` for table/column/measure names
4. Read `.specify/memory/{WorkbookName}/theme-overrides.md` if it exists

### Step 2: Apply Report Constitution

Read `.specify/memory/report-constitution.md` for:
- Layout rules (edge padding, inter-visual gap, canvas size)
- Typography (font, size)
- Theme & colors
- Table alignment (numbers LEFT, text RIGHT, dates LEFT)
- Number formatting (preserve from Tableau)

### Step 3: Map Visuals to Power BI

Read `visual-type-mapping.md` for type resolution, then use templates from `pbir-templates.md`.

**Critical Schema Rule**: `visualContainer/2.4.0` allows ONLY: `$schema`, `name`, `position`, `visual`/`visualGroup`. **NEVER** add `filters`, `filterConfig` at root.

**Key distinctions:**
- `visual.objects` = visual-type-specific formatting (data colors, axis, labels)
- `visual.visualContainerObjects` = container formatting (title, background, border) — same for ALL types
- NEVER put `title`/`background`/`border` inside `objects`
- ALL visuals MUST include `visualContainerObjects.border` with `show: true`
- SLICERS: Set `visualContainerObjects.title.show = false`

### Step 4: Calculate Positions

Read `layout-calculation.md` for:
- Coordinate scaling formula (Tableau → PBI)
- No-overlap validation
- Minimum height constraints
- Row alignment rules

### Step 5: Generate Navigation Buttons

Read `navigation-buttons.md` for:
- Page Navigation button template (Tableau `goto-sheet`)
- Bookmark Toggle button template (Tableau `toggle-action`)
- Z-index, styling, and action rules

### Step 6: Generate Report Files

Use folder structure from `pbir-templates.md`:
```
Output/{WorkbookName}/{ProjectName}.Report/
├── definition.pbir
└── definition/
    ├── report.json, version.json
    └── pages/
        ├── pages.json
        └── {PageName}/
            ├── page.json
            └── visuals/{visual_name}/visual.json
```

### Step 7: Validate

Read `report-validation.md` and run all 6 validation layers.

## Critical Rules — Report Load Failures Prevention

### 1. themeCollection — safe patterns only
`themeCollection: {}` is always safe (uses PBI default theme). If using `baseTheme`, include `name` + `type` + `reportVersionAtImport` (object, not string). NEVER add `resourcePackage` or any other property — PBI Desktop rejects them.

**Valid baseTheme example:**
```json
"themeCollection": {
  "baseTheme": {
    "name": "CY24SU06",
    "type": "SharedResources",
    "reportVersionAtImport": { "visual": "1.8.95", "report": "2.0.95", "page": "1.3.95" }
  }
}
```

### 2. Use Enhanced Folder Format (PBIR), NOT Legacy Flat Format
Legacy `sections` array causes ThemeServiceBase crash in modern PBI Desktop.

### 3. Visual Type Must Match Tableau Source Exactly
Read `visual-type-mapping.md` — faithfulness rules section.

### 4. ALL Dashboards → ALL Pages
Each Tableau dashboard = one Power BI report page. Never skip dashboards.

### 5. Ask User for Ambiguous Visual Types
Use `vscode_askQuestions` — never silently default to bar/column charts.

### 6. objects vs visualContainerObjects
- `objects` = visual-specific (dataPoint, categoryAxis, valueAxis, legend, data)
- `visualContainerObjects` = container (title, background, border, padding)

### 7. Slicer title MUST be disabled
Slicers use built-in header. Title creates duplicate label.

### 8. File encoding — UTF-8 WITHOUT BOM
Use `create_file` tool or `[System.IO.File]::WriteAllText()` with `UTF8Encoding($false)`.

### 9. PBIP project file schemas
Read `pbir-templates.md` → "PBIP Entry Point Files" section for `.pbip`, `definition.pbir`, `definition.pbism` schemas.
