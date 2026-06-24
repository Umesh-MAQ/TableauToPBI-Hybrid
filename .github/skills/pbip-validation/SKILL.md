# PBIP Validation Skill

## Purpose

The validation rules for a generated PBIP semantic model — schema correctness, model integrity, and M safety. Single-responsibility companion to the PBIP generation pipeline. Read this as the final gate before handing off the model.

## When to Use

- After PBIP semantic model generation, before handoff/validation stages
- When a generated `.pbip` fails to open or load in Power BI Desktop

## PBIP Schema Rules (prevents "schema does not allow additional properties")

1. `.pbip` artifacts array: ONLY objects with `"report"` — NEVER `"dataset"`/`"semanticModel"`.
2. `.pbism`: ONLY `"version"` (= `"4.2"`) + `"settings": {}` — NEVER `"datasetReference"`/`"compatibilityLevel"`, nothing inside `settings`.
3. `.pbir`: version MUST be `"4.0"` — uses `"datasetReference"` with `"byPath"`.
4. `.pbi/editorSettings.json` (TMSL only): ONLY `"version": "1.0"`.
5. `report.json`: REQUIRED in the Report folder or Desktop won't load.

## Entry-Point Version Pins (verify by reading each file)

- `definition.pbism` → `"version": "4.2"` + semanticModel `definitionProperties/1.0.0` schema. If `"1.0"`, Desktop looks for `model.bim` and fails — fix immediately.
- `definition.pbir` → `"version": "4.0"` + report `definitionProperties/2.0.0` schema.
- `.pbip` → has the `pbip/pbipProperties/1.0.0` `$schema`.

## Model Integrity Rules

6. Every table in relationships MUST exist in the tables set.
7. Every column referenced in relationships MUST exist in that table.
8. All measure expressions MUST reference valid table/column names.
9. No duplicate table names; no duplicate column names within a table.
10. (TMSL) `model.bim` MUST be valid JSON (escape DAX quotes).
11. `compatibilityLevel` MUST be ≥ 1567.
12. Each table MUST have at least one partition.
13. MUST create BOTH `Report/` and `SemanticModel/` folders.
14. Relationships: do NOT specify `fromCardinality`/`toCardinality`/`isActive`.
15. `GENERATESERIES` tables produce a column named `Value` — use `"sourceColumn": "Value"` (no brackets).
16. Relationship "to" columns MUST contain unique values (keys, not repeated attributes).

## RLS Role Rules (only when analysis detected RLS)

16a. A `definition/roles/` folder exists ONLY when the analysis reported RLS `Detected: Yes`. If `Detected: No`, NO `roles/` folder may exist.
16b. Each `definition/roles/{RoleName}.tmdl` starts with `role {Name}` (no `createRole`/TMSL syntax).
16c. Every role has `modelPermission: read` (never `none` for a security role).
16d. Each `tablePermission {Table} = <DAX>` references a table/column that exists in the model; the DAX is a row-level boolean predicate and does NOT reference measures.
16e. Dynamic RLS: the mapping table (e.g. `User_Access`) has its own `tables/{MappingTable}.tmdl` partition, a `ref table` line in `model.tmdl`, and a relationship to the secured/fact table. Set `crossFilteringBehavior: bothDirections` when the user filter must reach the fact rows.

## M Query Rules (prevents data-loading errors)

17. NEVER use `Table.TransformColumns` with row field access (`[col]`) — use `Table.AddColumn`.
18. NEVER reference other query/table names in M — each partition is self-contained.
19. ALWAYS use absolute file paths in `File.Contents()`.
20. ALWAYS use `QuoteStyle.Csv` for CSV.
21. ALWAYS null-check columns before applying text functions.

(Full M templates + safety detail in **`pbip-m-queries`**.)

## TMDL Structural Checks

- Indentation uses tabs (one per nesting level).
- Columns/measures nested in table; measure DAX on the SAME line as `measure 'Name' =`.
- Names quoted only when containing spaces/dots/equals/colons or starting with a digit.
- `database.tmdl` starts with just `database`.
- Every `ref table` has a matching `tables/{Name}.tmdl`.

(Full TMDL rules in **`pbip-tmdl-syntax`**.)

## Plugin Validators (run in terminal)

```powershell
# TMDL structural syntax
& "plugins\pbip\hooks\bin\tmdl-validate-windows-x64.exe" "Output\{WorkbookName}\{ModelName}.SemanticModel\definition"

# Cross-cutting PBIP project validator
python "plugins\pbip\skills\pbip\scripts\validate_pbip.py" "Output\{WorkbookName}"
```

Fix any reported errors before proceeding.

## Output Checklist

- [ ] `{Name}.pbip`
- [ ] `{Name}.Report/definition.pbir` + `report.json`
- [ ] `{Name}.SemanticModel/definition.pbism`
- [ ] `{Name}.SemanticModel/definition/` (database/model/relationships + tables) — or `model.bim` for TMSL
- [ ] `definition/roles/*.tmdl` (ONLY if analysis detected RLS — else no `roles/` folder)
- [ ] `diagramLayout.json`
