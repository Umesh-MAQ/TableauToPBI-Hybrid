# PBIP Structure Skill

## Purpose

Define the Power BI Project (.pbip) folder structure and all entry-point manifest files for the semantic model. Single-responsibility companion to the PBIP generation pipeline. TMDL syntax lives in **`pbip-tmdl-syntax`**; M queries in **`pbip-m-queries`**.

## When to Use

- During PBIP generation, when scaffolding the `Output/{WorkbookName}/` folders and writing `.pbip`, `.pbir`, `.pbism`, `report.json`

## Reference

- https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-dataset

## Output Location

All PBIP artifacts MUST live in `Output/{WorkbookName}/` (one subfolder per workbook). Never place PBIP files in the workspace root. Data files are read from `Data/{subfolder}/`.

## Folder Structure (TMDL — PREFERRED)

```
Output/{WorkbookName}/{ModelName}.pbip
Output/{WorkbookName}/{ModelName}.Report/
├── definition.pbir
└── report.json
Output/{WorkbookName}/{ModelName}.SemanticModel/
├── definition.pbism
├── diagramLayout.json              # may be {}
└── definition/
    ├── database.tmdl
    ├── model.tmdl
    ├── expressions.tmdl            # if shared M expressions exist
    ├── relationships.tmdl
    ├── roles/                      # ONLY if analysis detected RLS (one file per role)
    │   └── {RoleName}.tmdl
    └── tables/
        ├── FactTable.tmdl
        └── DimTable1.tmdl
```

> Use TMDL by default. Only use TMSL (`model.bim` JSON) if specifically requested.
> The `roles/` folder is created ONLY when the Tableau analysis reports RLS `Detected: Yes`. See `pbip-tmdl-syntax` for role file syntax.

## .pbip

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
  "version": "1.0",
  "artifacts": [{"report": {"path": "{ModelName}.Report"}}],
  "settings": {"enableAutoRecovery": true}
}
```

> **CRITICAL**: `artifacts` MUST contain ONLY a `"report"` entry — NEVER `"dataset"` or `"semanticModel"`. The semantic model is referenced from `definition.pbir`. `path` is relative to the `.pbip` location.

## definition.pbir (in `.Report/`)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
  "version": "4.0",
  "datasetReference": {"byPath": {"path": "../{ModelName}.SemanticModel"}}
}
```

> Version MUST be `"4.0"` (NOT `"1.0"`). Do NOT include `"byConnection": null`.

## definition.pbism (in `.SemanticModel/`)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
  "version": "4.2",
  "settings": {}
}
```

> Version MUST be `"4.2"` (NOT `"1.0"` — `"1.0"` makes Desktop look for `model.bim` and fail). Do NOT include `"datasetReference"`. Keep `settings` empty.

## report.json (minimal, in `.Report/`)

> For the SEMANTIC MODEL pipeline this minimal report is a placeholder. The full report layer (Enhanced PBIR with visuals) is produced separately — see the report skills. If only the model is being generated, a minimal report.json keeps the project openable.

When using PBIR-Legacy minimal placeholder:
```json
{
  "config": "{\"version\":\"5.53\",\"themeCollection\":{},\"activeSectionIndex\":0,\"linguisticSchemaSyncVersion\":2}",
  "layoutOptimization": 0,
  "publicCustomVisuals": [],
  "sections": [{"name": "ReportSection1", "displayName": "Page 1", "displayOption": 1, "width": 1280, "height": 720, "visualContainers": []}]
}
```

> `themeCollection` MUST be `{}`. Top-level `config` is a stringified JSON string (no top-level `$schema`). Each section needs `displayOption: 1`, `width: 1280`, `height: 720`.

## Required Both Folders

Power BI Desktop REQUIRES both `.Report/` and `.SemanticModel/`. The `.pbip` references the report, which references the model.

## Encoding

- All files UTF-8 WITHOUT BOM. In PowerShell 5.1 use `[System.IO.File]::WriteAllText(path, content, [System.Text.UTF8Encoding]::new($false))` or the `create_file` tool.

## Anti-Hallucination

- Use only the source connection details from the analysis output; never invent file paths or servers.
