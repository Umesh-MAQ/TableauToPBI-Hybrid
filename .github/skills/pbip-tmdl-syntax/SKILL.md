# PBIP TMDL Syntax Skill

## Purpose

The TMDL authoring rules for the semantic model definition files (`database.tmdl`, `model.tmdl`, `expressions.tmdl`, table `.tmdl`, `relationships.tmdl`). Single-responsibility companion to the PBIP generation pipeline. Indentation and measure-line rules here are the most common failure points.

## When to Use

- During PBIP generation, when writing any `.tmdl` file
- When a TMDL parse error occurs ("syntax for 'displayFolder' is incorrect", missing artifact, etc.)

## database.tmdl (MANDATORY)

```tmdl
database
	compatibilityLevel: 1567
```

> Must exist. Starts with just `database` (no name, no `createOrReplace`). Without it the model cannot be parsed.

## model.tmdl (MANDATORY — includes ref table declarations)

```tmdl
model Model
	culture: en-US
	defaultPowerBIDataSourceVersion: powerBI_V3
	sourceQueryCulture: en-US
	dataAccessOptions
		legacyRedirects
		returnErrorValuesAsNull

annotation PBI_QueryOrder = ["{TableList}"]

annotation __PBI_TimeIntelligenceEnabled = 1

annotation PBI_ProTooling = ["DevMode"]

ref table {FactTableName}
ref table {DimTable1Name}
```

> **CRITICAL**:
> - `dataAccessOptions` with `legacyRedirects` + `returnErrorValuesAsNull` is REQUIRED.
> - EVERY table file in `tables/` MUST have a matching `ref table` line.
> - `PBI_QueryOrder` MUST list ALL table names as a JSON array string.
> - Do NOT include `discourageImplicitMeasures: true` (blocks measure creation in Desktop).
> - Do NOT include `queryGroup` declarations unless an expression references them.

## expressions.tmdl (shared M expressions)

```tmdl
expression {ExpressionName} =
		let
			Source = Csv.Document(...)
		in
			Result
	lineageTag: {guid}
```

> **CRITICAL INDENTATION**:
> - M body (`let`/`in` + content) at **2 tabs**; body content (assignments) at **3 tabs**; TMDL properties (`lineageTag:`) at **1 tab**.
> - If `lineageTag` is at the same indent as `let`/`in`, the parser treats it as M code → syntax error.
> - Do NOT use `queryGroup:` unless the group is defined in `model.tmdl` ("Property QueryGroup refers to an object which cannot be found").

## Table .tmdl files (in `tables/`)

```tmdl
table {TableName}
	lineageTag: {guid}

	column {ColumnName}
		dataType: string
		lineageTag: {guid}
		summarizeBy: none
		sourceColumn: {SourceColumnName}

	measure {MeasureName} = {ENTIRE DAX ON SAME LINE}
		displayFolder: {FolderName}
		formatString: #,##0
		lineageTag: {guid}

	partition {TableName} = m
		mode: import
		source =
			let
				Source = {M Expression}
			in
				Result
```

### ⚠️ Measure Rule (most common error)

The ENTIRE DAX expression MUST be on the SAME LINE as `measure 'Name' =`. Properties go on the next lines at 2-tab indent.

**Correct:**
```tmdl
	measure 'Total Sales' = SUM(FactOrders[Sales])
		formatString: $#,##0
		displayFolder: Sales
		lineageTag: {guid}
```

**WRONG (DAX on next line → "syntax for 'displayFolder' is incorrect"):**
```tmdl
	measure 'Total Sales' =
		SUM(FactOrders[Sales])
		formatString: $#,##0
```

For VAR/RETURN, keep it ALL on one line:
```tmdl
	measure 'CY Sales' = VAR _Year = [Selected Year] RETURN CALCULATE(SUM(FactOrders[Sales]), DimDate[Year] = _Year)
		formatString: $#,##0
		displayFolder: Sales
		lineageTag: {guid}
```

### Partition Indentation
- `partition` at 1 tab, `mode:`/`source =` at 2 tabs, M body at 3 tabs, M content at 4 tabs.

## relationships.tmdl

```tmdl
relationship {guid}
	fromColumn: {FactTable}.{FKColumn}
	toColumn: {DimTable}.{PKColumn}
	crossFilteringBehavior: oneDirection
```

> Do NOT include `fromCardinality`, `toCardinality`, or `isActive` — Power BI infers these.
> NEVER put a `///` description on a `relationship` — relationships have no `description` property and Power BI Desktop fails to load with "Property 'description' is unknown and is not expected in the situation it appears."
> Do NOT use `//` comments either — TMDL has no `//` comment syntax; a `//` line fails to load with "Unexpected line type: Other!". Leave relationships.tmdl with declarations only (no comment lines of any kind).
> The `tmdl-validate` linter catches NEITHER of these — they are TOM/TMDL-parser rules.

## roles/{RoleName}.tmdl (ONLY when RLS detected)

Generate one file per security role under `definition/roles/`, ONLY when the Tableau analysis reported RLS `Detected: Yes`. If RLS is `None`, do NOT create a `roles/` folder.

**Dynamic RLS** (preferred — filter a user-mapping table by the signed-in user):
```tmdl
role {RoleName}
	modelPermission: read

	tablePermission 'User_Access' = 'User_Access'[Username] = USERPRINCIPALNAME()
```

**Static per-value RLS** (one role per distinct entitlement value):
```tmdl
role India
	modelPermission: read

	tablePermission Netflix = 'Netflix'[country] = "India"
```

> **CRITICAL role rules**:
> - `role` is a root-level declaration (indent 0); `modelPermission:` and `tablePermission` are at 1 tab.
> - Take the role name directly — NO `createRole`/TMSL syntax.
> - `modelPermission: read` is REQUIRED (never `none` for a security role).
> - `tablePermission {Table} = <DAX>` — the DAX is a per-row boolean predicate on `{Table}`; it must NOT reference measures.
> - Reference columns as `'Table'[Column]` (single-quote the table name, bracket the column).
> - Use `USERPRINCIPALNAME()` (signed-in UPN/email) to match Tableau's `USERNAME()`. Use `USERNAME()` only for the older `DOMAIN\user` form.
> - For dynamic RLS to filter the fact: the mapping table needs its own `tables/{MappingTable}.tmdl` partition, a `ref table` line in `model.tmdl`, and a relationship in `relationships.tmdl` to the secured/fact table — set `crossFilteringBehavior: bothDirections` when the filter must flow from the mapping table to the fact rows.

## Naming / Quoting

- Quote names only when they contain spaces, dots, equals, colons, or start with a digit.
- `///` (triple-slash Description) must immediately precede a declaration (no blank line between).

## Anti-Hallucination

- Every `ref table` must map to a real table file; every relationship column must exist in its table.
