# Visual Type Mapping Skill

## Purpose

Map Tableau mark types and encodings to the correct Power BI `visualType`. This is the single source of truth for chart type decisions during visual migration.

## Primary Mapping Table

| Tableau Mark | Condition | Power BI `visualType` |
|---|---|---|
| Text | Single measure, no dimensions | `card` |
| Text | Rows only (flat list) | `tableEx` |
| Text | Rows AND cols (cross-tab) | `pivotTable` |
| Bar | Dimension on rows (horizontal) | `clusteredBarChart` |
| Bar | Dimension on cols (vertical) | `clusteredColumnChart` |
| Line | Any | `lineChart` |
| Line | + color on dimension | `lineChart` with Legend |
| Area | Any | `areaChart` (NEVER lineChart) |
| Pie | Any | `pieChart` or `donutChart` |
| Circle / Shape | Two measures | `scatterChart` |
| Square | Single dimension + size/color on measure | `treemap` |
| Square | Dimensions on BOTH rows AND cols + color on measure | `pivotTable` (heatmap) |
| Map | Geographic fields | `map` or `filledMap` |
| Gantt | Approximate | `decompositionTree` |
| Polygon | Approximate | `shape` |

## Automatic Mark Inference Rules

When `mark class='Automatic'`, infer from encodings (apply in ORDER — first match wins):

1. **Color + Size encodings on SAME measure + text/label on dimension** → `treemap`
2. **Date/time on columns + measure on rows** → `lineChart` or `areaChart`
3. **Only dimensions on rows + measures on text encoding** → `tableEx`
4. **Dimensions on BOTH rows AND columns + measures** → `pivotTable`
5. **Single measure, no dimensions** → `card`
6. **KPI-style (single big number + trend)** → `kpi` (requires `objects.status.direction`)
7. **Geographic field present** → `map`
8. **Hierarchy on rows (fields joined by `/`) + measure on cols** → `clusteredBarChart`
9. **Dimension on one axis + measure on other** → `clusteredBarChart` or `clusteredColumnChart`
10. **Distribution/histogram (count of entities binned by computed value)** → `clusteredColumnChart` with calculated column

> **KPI vs Card**: If the Tableau sheet has a single big number WITH a trend/sparkline or % change indicator, use `kpi`. If it's just a standalone number with no trend context, use `card`.

> **Histogram Rule**: If a Tableau chart shows a DISTRIBUTION (e.g., "how many customers have N orders"), do NOT map it to a bar chart with entity names on the axis. Instead, add a calculated column for the bin value (e.g., `DimCustomer[Orders Per Customer]`) and use it as the X-axis category with a count measure as Y.

> **Priority Rule**: If a visual has BOTH color AND size encodings on a measure, it is a `treemap` regardless of other factors.

## Square Mark Decision Tree

```
Square mark?
├── Color on measure + dimensions on BOTH rows AND cols → pivotTable (heatmap)
├── Only rows + size/color on measure → treemap
└── Single dimension + size → treemap
```

## Text Mark Decision Tree

```
Text mark?
├── Single aggregate value, no dimensions → card
├── ONLY <rows> has dimensions, measures on <text> encoding → tableEx
├── BOTH <rows> AND <cols> have dimensions → pivotTable
└── NEVER map text to bar/column/line chart
```

## Orientation Rules

- Tableau "Bar" mark with **dimension on rows** → `clusteredBarChart` (horizontal bars)
- Tableau "Bar" mark with **dimension on cols** → `clusteredColumnChart` (vertical bars)
- Tableau field on rows = Power BI Y-axis
- Tableau field on cols = Power BI X-axis

## Chart Type Faithfulness Rules

1. Generate ONLY visuals that exist in the original Tableau dashboard — do NOT invent new visuals
2. Match the EXACT chart type from Tableau — a treemap MUST stay a treemap
3. If the original has a color/legend encoding, the PBI visual MUST include a Legend field
4. Preserve data labels if the original shows them (text encoding → data labels ON)
5. If Tableau dashboard has N worksheets, PBI page must have exactly N chart visuals (plus slicers)

## Ambiguous Cases — Ask the User

When NO rule matches clearly:
- Use `vscode_askQuestions` to present 2-3 Power BI alternatives with descriptions
- Record the user's choice
- NEVER silently default to bar/column charts

## Complete visualType Reference

| `visualType` | Description |
|---|---|
| `clusteredBarChart` | Horizontal bars |
| `clusteredColumnChart` | Vertical bars |
| `stackedBarChart` | Stacked horizontal |
| `stackedColumnChart` | Stacked vertical |
| `lineChart` | Line/trend |
| `areaChart` | Filled area |
| `lineClusteredColumnComboChart` | Combo |
| `pieChart` | Pie |
| `donutChart` | Donut |
| `card` | Single KPI |
| `multiRowCard` | Multiple KPIs |
| `tableEx` | Flat table |
| `pivotTable` | Matrix/cross-tab |
| `map` | Bubble map |
| `filledMap` | Choropleth |
| `treemap` | Treemap |
| `slicer` | Filter control |
| `textbox` | Text/title |
| `shape` | Shape |
| `kpi` | KPI with trend |
| `waterfallChart` | Waterfall |
| `funnel` | Funnel |
| `scatterChart` | Scatter/bubble |
| `gauge` | Gauge |
| `actionButton` | Navigation / bookmark button |
