# Tableau Workbook Analysis Skill

## Purpose

Analyze any Tableau workbook file (`.twb` or `.twbx`) present in the workspace and extract comprehensive metadata including datasources, columns, calculated fields, parameters, worksheets, dashboards, and relationships. This skill is generic and works with any Tableau workbook regardless of its content or domain.

## When to Use

- User asks to analyze a Tableau workbook
- User wants to extract metadata from a `.twb` or `.twbx` file
- User needs a summary of datasources, fields, calculations, or visualizations in a Tableau file
- User wants to understand the structure of a Tableau workbook before migration
- As a prerequisite step before generating a Power BI migration constitution

## Instructions

### Step 1: Locate Tableau Files

Search the `Data/` folder for Tableau workbooks:
- Use `file_search` with glob patterns `Data/**/*.twb` and `Data/**/*.twbx`
- Workbooks are organized in subfolders under `Data/` (e.g., `Data/Netflix/`, `Data/Loan/`, `Data/Q3 Buyer/`, `Data/Sales and Customer/`)
- If a `.twbx` file is found, note that it is a packaged workbook (ZIP containing a `.twb` and data extracts)
- Do NOT hardcode any specific file name — always discover dynamically
- Data files (CSV, Excel) are co-located with the `.twb` in the same subfolder

### Step 2: Parse the TWB XML Structure

A `.twb` file is XML. Extract the following metadata sections:

#### 2.1 Workbook Metadata
- `version` attribute on `<workbook>` element
- `source-build` and `source-platform` attributes

#### 2.2 Parameters
- Look for `<datasource name='Parameters'>` 
- Extract each `<column>` inside with:
  - `caption` (display name)
  - `datatype` (integer, string, real, date, etc.)
  - `param-domain-type` (range, list, all)
  - Default value from `<calculation formula='...' />`
  - Range constraints from `<range min='' max='' granularity='' />`

#### 2.3 Datasources
- Each `<datasource>` element (excluding the Parameters datasource)
- Extract:
  - `caption` (display name)
  - `name` (internal ID)
  - Connection type from `<connection class='...'>`
  - File paths from `<named-connection>` elements
  - Tables/relations from `<relation>` elements

#### 2.4 Columns & Fields
For each datasource, extract `<column>` elements:
- `caption` (display name)
- `name` (internal field name)
- `datatype` (string, integer, real, boolean, date, datetime)
- `role` (dimension or measure)
- `type` (nominal, ordinal, quantitative)
- `semantic-role` if present (geographic roles like `[State].[Name]`)

#### 2.5 Calculated Fields
Identify columns with nested `<calculation>` elements:
- `caption` (calculated field name)
- `formula` (the Tableau calculation expression — decode XML entities like `&quot;`, `&gt;`, `&lt;`, `&amp;`, `&#13;&#10;`)
- `datatype` and `role`
- Table calculation settings from `<table-calc>` if present (ordering-type, ordering-field)

#### 2.6 Worksheets
- Each `<worksheet name='...'>` element
- Extract the worksheet name

#### 2.7 Dashboards
- Each `<dashboard name='...'>` element
- Extract the dashboard name (decode `&amp;` etc.)

#### 2.8 Relationships / Joins
- Look for `<relation type='join'>` elements inside datasource connections
- Extract join type, left/right tables, and join clauses from `<clause>` elements
- Also check for multi-table `<relation type='collection'>` (logical model)

#### 2.9 Sets (MANDATORY — extract if present)
Tableau sets are `<group>` elements that define a subset of dimension members.
- **Fixed (constant) set**: `<group name='[... Set]'>` containing `<groupfilter function='member' ...>` clauses listing explicit member values
- **Computed (dynamic) set**: `<group name='[... Set]'>` containing `<groupfilter function='filter' ...>` or a top-N condition (e.g. `function='end'` with a `<groupfilter function='top'>` child)
- Extract: set name, source dimension/field, set type (fixed vs computed), and the member list or top-N condition
- If NO `<group>` set elements exist, write `None` — do not invent sets

#### 2.10 Groups (MANDATORY — extract if present)
Tableau groups merge dimension members under an alias.
- Look for `<group name='[... (group)]'>` elements OR `<calculation class='categorical-bin'>` group columns
- Extract: group field name, source dimension, and each alias → list of member values it maps
- If NO group elements exist, write `None` — do not invent groups

#### 2.11 Bins (MANDATORY — extract if present)
- Look for `<column>` elements with `<calculation class='bin' ... />`
- Extract: bin field name, source numeric field, and bin size (`decimal-bin-size` / `bin-size` attribute)
- If NO bin elements exist, write `None` — do not invent bins

#### 2.12 Data Blending (MANDATORY — extract if present)
Blending occurs when a workbook has MORE THAN ONE real datasource and worksheets reference fields across them on common dimensions.
- Count real `<datasource>` elements (excluding the `Parameters` datasource)
- If more than one, inspect worksheets for cross-datasource field references and look for `<datasource-dependencies>` blocks naming a secondary datasource
- Extract: primary datasource, secondary datasource(s), and the linking field(s) (common dimension names)
- If only ONE real datasource exists, write `Single datasource — no blending` — do not invent a blend

#### 2.13 Field Formatting (MANDATORY — extract if present)
- For columns and calculated fields, capture any `<format>` / `default-format` / `aggregation` formatting attributes that define a display format (currency, percentage, decimals, date pattern)
- Record the raw Tableau format string verbatim (e.g. `$#,##0`, `0.0%`, `[h]:mm:ss`, `mmmm yyyy`)
- These map to Power BI `formatString` values in the DAX/PBIP stages
- If a field has no explicit format, write `Default` — do not invent a format

#### 2.14 Row-Level Security (RLS) (MANDATORY — detect if present)

Tableau enforces row-level security through user functions inside calculated fields or filters and through user-mapping (entitlement) tables. Detect ANY of the following signals:

- **User functions** in a `<calculation>` formula or filter: `USERNAME()`, `FULLNAME()`, `ISMEMBEROF('Group')`, `ISUSERNAME(...)`, `ISFULLNAME(...)`
- **User-filter / data-source filter** entries that compare a user function to a column (e.g. `[Username] = USERNAME()`)
- **User-mapping table**: a datasource/relation whose rows pair a user identifier with an entitlement value (e.g. `User_Access` with `Username` + `Country`) joined/blended to a secured table on the entitlement column
- **Hardcoded per-user predicates** (e.g. `USERNAME() = "x" AND [Region] = "India"`)

For each detected signal, classify the intended Power BI pattern:

| Tableau construct | Power BI equivalent |
|-------------------|---------------------|
| `USERNAME()` in a calc | `USERPRINCIPALNAME()` (UPN/email) |
| `FULLNAME()` | `USERNAME()` (DOMAIN\\user) — rarely needed |
| `ISMEMBEROF('Group')` | Assign AD/Entra group to the role in the Power BI Service |
| User-mapping CSV (Username→entitlement) joined to data | **Dynamic RLS**: filter the mapping table by the current user |
| Hardcoded `USERNAME() = "x" AND value = "India"` | **Static RLS**: one role per value |

Record: detection flag (`Yes`/`No`), the RLS type (Dynamic mapping-table / Static per-value / Group-based), the secured table + entitlement column, the mapping table + user column (if any), and a suggested role name per role.

If NO RLS signals exist, write `Detected: No` — do not invent roles or mapping tables.

### Step 3: Output Format

Present the extracted metadata in a structured markdown format:

```markdown
# Tableau Workbook Analysis: {workbook_name}

## Workbook Info
- **Version**: ...
- **Source Build**: ...
- **Platform**: ...

## Parameters
| Name | Data Type | Domain Type | Default | Range/Values |
|------|-----------|-------------|---------|--------------|

## Datasources
### {Datasource Caption}
- **Connection Type**: ...
- **Source File(s)**: ...
- **Tables**: ...

#### Dimensions
| Display Name | Field Name | Data Type | Semantic Role |
|--------------|------------|-----------|---------------|

#### Measures
| Display Name | Field Name | Data Type |
|--------------|------------|-----------|

## Calculated Fields
| Name | Formula | Data Type | Type | Table Calc |
|------|---------|-----------|------|------------|

## Worksheets
1. ...

## Dashboards
1. ...

## Relationships
| Left Table | Right Table | Join Type | Condition |
|------------|-------------|-----------|-----------|

## Sets
| Set Name | Source Field | Type (Fixed/Computed) | Members / Condition |
|----------|--------------|-----------------------|---------------------|

## Groups
| Group Field | Source Dimension | Alias | Member Values |
|-------------|------------------|-------|---------------|

## Bins
| Bin Field | Source Field | Bin Size |
|-----------|--------------|----------|

## Data Blending
| Primary Datasource | Secondary Datasource | Linking Field(s) |
|--------------------|----------------------|------------------|

## Field Formatting
| Field | Tableau Format String | Kind (Currency/Percent/Date/Number) |
|-------|-----------------------|-------------------------------------|

## Row-Level Security (RLS)
- **Detected**: Yes / No
- **Type**: Dynamic (mapping table) / Static (per-value) / Group-based / None
- **Secured Table.Column**: {table}.{entitlement column}
- **Mapping Table.User Column**: {mapping table}.{user column} (only for Dynamic)

| Suggested Role | RLS Type | Secured Table | Entitlement Column | Mapping Table | User Column | Power BI Filter (DAX intent) |
|----------------|----------|---------------|--------------------|---------------|-------------|------------------------------|
```

> **NOTE**: Include the Sets, Groups, Bins, Data Blending, Field Formatting, and Row-Level Security (RLS) sections in EVERY analysis output. If a category has no items in the workbook, keep the section heading and write a single row stating `None` (or `Single datasource — no blending`, or `Detected: No` for RLS). Never omit a section and never fabricate rows.

### Step 4: Save Analysis Output

**MANDATORY**: After extracting metadata, save the full structured output to `.specify/memory/tableau-analysis-output.md`. This file serves as the input context for the automatic constitution generation step.

### Step 5: Hand Off to Migration Pipeline

After saving, **automatically** call the `migration-constitution` agent which handles the rest:
1. **Constitution** — Power BI best practices (star schema, DAX, naming, relationships, performance, parameters, semantic layer, traceability)
2. **Specify** — Detailed migration spec (tables, columns, measures, relationships, parameters)
3. **Clarify** — Resolve ambiguities (measure vs column, unclear joins, table calcs, data categories)

### Step 6: Additional Analysis (if requested)
- Identify unused fields and overly complex calculations (deeply nested table calcs) for cleanup recommendations
- Flag any Tableau feature with no documented Power BI mapping (e.g. `SCRIPT_*()` R/Python calls, forecasting, trend lines) so a human can decide

## Anti-Hallucination Rules (MANDATORY)

These rules keep extraction grounded in the actual `.twb` XML and prevent scope inflation:

1. **Extract only what exists.** Every table, column, calculated field, parameter, set, group, bin, relationship, and format MUST be traceable to a concrete XML element in the workbook. Never invent names, fields, or values.
2. **Use `None` for absent categories.** If a section (Sets, Groups, Bins, Data Blending, etc.) has no source elements, write `None` — do not fabricate plausible-sounding entries.
3. **Quote, don't paraphrase, formulas and format strings.** Copy Tableau calculation formulas and format strings verbatim (after decoding XML entities). Do not "improve" or guess them.
4. **One pass, fixed scope.** Extract the sections defined in Step 2 and Step 3 only. Do not add extra analyses, speculative measures, or design recommendations beyond what is requested.
5. **Mark uncertainty explicitly.** If an element is ambiguous or unparseable, label it `UNVERIFIED` rather than guessing a value. Do not silently fill gaps.
6. **No downstream design here.** This skill EXTRACTS metadata only. Do not generate DAX, star-schema designs, or visuals — those are separate pipeline stages.

## Notes

- GENERIC — never hardcode file names, discover dynamically via file search
- Decode XML entities in formulas and names
- For `.twbx` (ZIP), extract the `.twb` inside
- Bracket notation: `[field_name]`, internal calcs: `[Calculation_XXXX]`
- Cross-datasource: `[datasource_name].[field_name]`
- Pipeline (constitution → specify → clarify) is AUTOMATIC — no confirmation needed
- Ref: https://learn.microsoft.com/en-us/power-bi/guidance/
