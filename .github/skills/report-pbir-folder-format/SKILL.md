# Report PBIR Folder Format Skill

## Purpose

Generate the Enhanced PBIR folder structure and its entry-point files (`definition.pbir`, `report.json`, `version.json`, `pages.json`, `page.json`). Single-responsibility companion to the report visual generation pipeline.

## When to Use

- During report visual generation, when scaffolding the `.Report/` folder and writing non-visual entry files
- Per-visual `visual.json` authoring lives in **`report-visual-json`**

## ⚠️ Use Enhanced Folder Format (NOT Legacy)

Use the PBIR Enhanced Folder Format with per-visual JSON files. The legacy flat `report.json` with a `sections`/`visualContainers` array causes "ThemeServiceBase" crashes in modern Power BI Desktop.

## Folder Structure

```
Output/{WorkbookName}/{ProjectName}.Report/
├── definition.pbir              ← Points to semantic model
└── definition/
    ├── report.json              ← Report-level config (NO visuals)
    ├── version.json             ← Schema version
    └── pages/
        ├── pages.json           ← Page ordering array
        ├── ReportSection1/
        │   ├── page.json        ← Page config
        │   └── visuals/
        │       └── {visual_name}/visual.json
        └── ReportSection2/
            └── ...
```

- All report files MUST live inside `Output/{WorkbookName}/`.
- Folder names under `.Report/` MUST NOT contain spaces (pages/visuals won't render).
- Page/visual `name` values MUST match `^[a-zA-Z0-9_][a-zA-Z0-9_-]*$`.

## definition.pbir

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
  "version": "4.0",
  "datasetReference": {"byPath": {"path": "../{ModelName}.SemanticModel"}}
}
```

- Version MUST be `"4.0"`. The `byPath` target directory MUST exist. Do NOT add `"byConnection": null`.

## report.json (root — NO visuals)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json",
  "themeCollection": {},
  "settings": {"useStylableVisualContainerHeader": true}
}
```

- Schema MUST be `3.0.0`. NEVER use `1.0.0`.
- `themeCollection` MUST be `{}` — see `report-load-failure-rules`.
- NEVER add `"datasetBinding"` or `"layoutOptimization"` (not allowed in `3.0.0`; dataset binding lives only in `definition.pbir`).
- Always include `useStylableVisualContainerHeader: true`.
- If `baseTheme` is ever used, `reportVersionAtImport` MUST be an object `{"visual":"1.8.95","report":"2.0.95","page":"1.3.95"}`, NEVER a string.

## version.json

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
  "version": "2.0.0"
}
```

- `$schema` is MANDATORY. Without it PBI Desktop throws "Can't find '$schema' property in 'version.json'".

## pages.json (ordering)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
  "pageOrder": ["ReportSection1", "ReportSection2"]
}
```

- `$schema` MUST use `pagesMetadata` (NOT `pages`).

## page.json (per page)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json",
  "name": "ReportSection1",
  "displayName": "Dashboard Name",
  "displayOption": "FitToPage",
  "height": 720,
  "width": 1280
}
```

- `displayOption` MUST be a string (`"FitToPage"`, `"FitToWidth"`, or `"ActualSize"`), NEVER an integer.
- `width`/`height` should match the Tableau dashboard size when honoring fidelity (see `report-layout-gapping`).

## Coverage Rule

- Create a page for EVERY Tableau dashboard, not just the first. Each dashboard = one page.

## Encoding

- Write ALL files as UTF-8 WITHOUT BOM. In PowerShell 5.1 do NOT use `Set-Content -Encoding UTF8` (adds BOM); use `[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))`, or use the `create_file` tool.

## Anti-Hallucination

- Pages = dashboards; do not add empty or decorative pages.
