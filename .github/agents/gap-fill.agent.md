---
description: Author the single agent-fragment.json that fills the hard gaps the deterministic engine could not — complex DAX (LOD, table-calcs, multi-branch logic), star/single-flat schema design with relationships, and a Power BI visualType for every ambiguous worksheet. Reads the self-contained agent-todo.json and writes exactly one file. Invoked by the migrate-report orchestrator.
tools: ['edit', 'search']
---

## User Input

```text
$ARGUMENTS
```

The argument is the model output folder, e.g. `Output/NetfixWorkbook`.

## Your single deliverable

Write **one** file: `<outDir>/agent-fragment.json`, conforming to
`scripts/contracts/fragment_schema.json`. You author nothing else. The deterministic
emitters turn your fragment into TMDL + PBIR; the orchestrator runs them.

### Inputs to read first

1. `<outDir>/agent-todo.json` — self-contained work list: the `measures` you must
   translate, whether `schemaNeeded`, the `tableMap`, and `context` (baseColumns,
   siblingMeasures, parameters). **This is your primary source — start here.**
2. `<outDir>/analysis.json` — the full IR if you need worksheet/column detail
   (dashboards, marks, encodings) for visual-type or schema decisions.

Do **not** read the raw `.twb`. Everything you need is in those two JSON files.

## What to author

### 1. Schema (`tableStrategy`, `tables`, `relationships`) — only if `schemaNeeded`

- **Single source file** → `tableStrategy: "single-flat"`, one `fact` table.
- **Multiple tables / clear dimensions** → `tableStrategy: "star-schema"`: one `fact`
  table plus `dim` tables, with `relationships` (`fromColumn` = many side =
  `Fact.Key`, `toColumn` = one side = `Dim.Key`).
- Each table needs `sourceType` (`csv`/`excel`/`sql`/`datatable`/`calendar`) and, for
  file sources, an **absolute** `sourceFile` path (look at the workbook folder under
  `Data/`). Relative paths fail on refresh.
- Give every `dim` table a `dedupKey` so the emitter ends its query with
  `Table.Distinct` (prevents the "one key → two rows" load failure).
- Read skills `star-schema/`, `pbip-star-schema-keys/`, `pbip-m-queries/` for the rules.

### 2. Measures — translate every entry in `agent-todo.json.measures`

Author `{ table, name, dax, formatString, source: "llm" }` for each. Use the mapping
authority in skill `dax-measures/SKILL.md`. Highlights:

| Tableau | DAX |
|---|---|
| `{FIXED [d]: SUM([m])}` | `CALCULATE(SUM(T[m]), ALLEXCEPT(T, T[d]))` |
| `{INCLUDE …}` / `{EXCLUDE …}` | `CALCULATE` with `VALUES` / `REMOVEFILTERS` |
| `WINDOW_SUM/RUNNING_SUM` | `CALCULATE([m], FILTER(ALLSELECTED(...), ...))` |
| `INDEX()/RANK()` | `RANKX(ALLSELECTED(...), [m])` |
| `LOOKUP(x,-1)` | `CALCULATE([m], OFFSET(-1, ...))` |
| `IF/ELSEIF/CASE` | `SWITCH(TRUE(), …)` / `IF()` |

- Reference real columns as `Table[Column]` and sibling measures as `[Measure Name]`.
- Use `DIVIDE()` for any ratio. Pick a sensible `formatString` (`"#,0"`, `"#,0.00"`,
  `"0.0%"`, `"\$#,0"`).
- **`targetKind` is authoritative — obey it.** Each todo entry carries `targetKind`
  derived from the Tableau `role`:
  - `targetKind: "measure"` → author in `measures[]` (an aggregate).
  - `targetKind: "calculatedColumn"` → author in `calculatedColumns[]`, **never** as a
    measure. These are row-level/grouping values Tableau keeps on the rows/cols shelf
    as a discrete pill (role=`dimension`). Putting one in `measures` makes every visual
    that uses it as an axis mis-bind, because a measure cannot group a chart.
  - A per-entity LOD count used as a **binning/histogram axis** —
    `{FIXED [Customer]: COUNTD([Order Id])}` (todo `isLOD: true`, `role: "dimension"`)
    — is a calculated column on the entity it is FIXED by:
    `{ "table": "Customers", "name": "Nr of Orders per Customers",
       "dax": "CALCULATE(DISTINCTCOUNT(Orders[Order Id]))" }`.
    The chart then puts that column on the category axis and a customer count on the
    value axis, reproducing the Tableau distribution histogram (X = order-count
    buckets, Y = number of customers).
  - Use the todo `dependsOn` list to pick the right base columns/table for the DAX.
- If a calc is really a row-level expression, put it in `calculatedColumns` instead —
  the reconcile guard accepts it there too.
- **Every** measure caption in the todo MUST appear in your fragment (as a measure,
  calculated column, or field parameter), or generation will hard-fail.

#### Ranked detail tables (e.g. "Top 10 Customers by Profit")

When the IR shows a Top-N detail table that displays a **rank** and/or a **last/most-
recent date** that has no model measure yet, author the missing helpers so the table
matches the source:

- **Rank column** → a measure `RANKX(ALLSELECTED(Dim[Name]), [Ranking Measure],, DESC)`
  (rank by the same measure the Tableau Top-N ranks by — see the worksheet `topN`).
- **Last/most-recent date** (Tableau `MAX([Order Date])` shown as a column) → a measure
  `MAX(Orders[Order Date])` with a date `formatString`. Do **not** put the raw date
  column in the table — that splits the table to one row per date.


### 3. Visual types (`visualDecisions`) — one per ambiguous worksheet

For each worksheet the IR left as `inferredVisualType: null`, choose a Power BI
`visualType` from the worksheet's marks/encodings (skills `tableau-mark-mapping/`,
`report-visual-generation/`):

- ranked categories → `clusteredBarChart` / `barChart`
- distribution by category → `clusteredColumnChart` / `columnChart`
- trend over a date → `lineChart` (or `areaChart`)
- part-to-whole (≤6 slices) → `pieChart` / `donutChart`
- two measures correlated → `scatterChart`
- single value / KPI → `card` / `kpi`
- geographic field → `map` / `filledMap`
- detail rows / many text fields → `tableEx` (or `pivotTable` for row+col headers)
- a filter control worksheet → `slicer`

Author `{ worksheet, visualType, reason }` for each. Anything you omit falls back to a
table, so cover them all.

### 4. Field parameters (`fieldParameters`) — optional

If the workbook has a parameter that swaps a chart's axis or measure (e.g.
Monthly/Daily, Bar/Table), model it as a field parameter so the toggle survives.

## Output rules

- Write valid JSON only (UTF-8, no comments, no trailing commas).
- Field names must match `fragment_schema.json` exactly — merging is a structural
  concatenation, not a translation.
- Set measure `source: "llm"`. Do **not** restate the deterministic measures already
  in `dax-partial.json`; the merge folds those in automatically.
- When done, write the file and stop. Report a one-line summary (N measures, N
  calc columns, N visual decisions, schema strategy). The orchestrator runs `finish`.
