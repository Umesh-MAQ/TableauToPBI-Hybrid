# Report Visual JSON Skill

## Purpose

Author the per-visual `visual.json` structure (schema, position, data bindings) and the Tableau-mark → Power BI `visualType` mapping. Single-responsibility companion to the report visual generation pipeline.

## When to Use

- During report visual generation, for EVERY chart/table/card `visual.json`
- Whenever picking the correct `visualType` for a Tableau mark

## visual.json Structure

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "unique_visual_name",
  "position": {
    "x": 25, "y": 25, "z": 0,
    "height": 300, "width": 400, "tabOrder": 0
  },
  "visual": {
    "visualType": "tableEx",
    "query": {
      "queryState": {
        "Category": {
          "projections": [{
            "field": {
              "Column": {
                "Expression": {"SourceRef": {"Entity": "{TableName}"}},
                "Property": "{ColumnName}"
              }
            },
            "queryRef": "{TableName}.{ColumnName}",
            "active": true
          }]
        },
        "Y": {
          "projections": [{
            "field": {
              "Measure": {
                "Expression": {"SourceRef": {"Entity": "{TableName}"}},
                "Property": "{MeasureName}"
              }
            },
            "queryRef": "{TableName}.{MeasureName}",
            "active": true
          }]
        }
      }
    },
    "objects": {
      "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"Literal": {"Value": "'#4e79a7'"}}}}}}}]
    },
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'Visual Title'"}}}
      }}],
      "border": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#E0E0E0'"}}}}},
        "radius": {"expr": {"Literal": {"Value": "4D"}}}
      }}]
    }
  }
}
```

## ⚠️ CRITICAL — Allowed Top-Level Properties ONLY

The `visualContainer/2.4.0` schema permits ONLY: `$schema`, `name`, `position`, and `visual` (or `visualGroup`).

- **NEVER** add `filters`, `filterConfig`, `config`, or ANY other property at the root of `visual.json`. Power BI Desktop rejects it with "Property has not been defined and the schema does not allow additional properties".
- For Top N / measure-based filtering, rely on DAX logic (e.g. `Top N Filter = IF([State Rank] <= SELECTEDVALUE(...), 1, 0)`) and let users add visual filters in the Desktop UI.
- ⚠️ **NEVER hand-author a `VisualTopN` filterConfig.** The PBIR `filterConfig` schema is NOT the semantic-query format: `VisualTopN` does NOT accept `Expression`/`Count`/`OrderBy`/`IsAscending` — those crash the whole report on open with *"Property 'X' has not been defined and the schema does not allow additional properties"* + *"Required properties are missing: ItemCount"*. Do Top-N via a DAX rank measure instead; do not emit a TopN filterConfig at all.
- Page-level filters go in `page.json`; report-level filters go in `report.json`.

## Query Roles

- Use the correct role bucket per visual type: `Category` (axis/rows), `Y`/`Values` (measures), `Series`/`Legend` (color split), `Size` (bubble/treemap).
- `Column` → dimensions; `Measure` → measures. Match `Entity`/`Property` to real TMDL names.
- `queryRef` format: `{TableName}.{ColumnOrMeasureName}` (e.g. `FactLoan.Total Loan Amount`).

## ⚠️ CRITICAL — Projection `active` flag (prevents BLANK visuals)

- EVERY projection that should be DISPLAYED must be `"active": true`. **NEVER set `"active": false` on a field you want shown.**
- A projection marked `"active": false` is excluded from the visual's query, so it does not render. If you mark a card's second value or a table's grouping column `active: false`, the visual renders BLANK (title only, no data).
- This is the #1 cause of "card shows title but no number" and "table is empty": multi-field cards (value + % diff) and multi-column tables (name + measures) where only the first projection was `active: true`.
- RULE: in multi-row cards, KPI cards, and tables, set `"active": true` on ALL displayed value/column projections. Reserve `active: false` only for fields intentionally hidden (e.g. field-parameter alternates) — which is rare in migrations.
- Validators do NOT catch this — only opening in Power BI Desktop reveals the blank visual.

## Visual Type Mapping

| Power BI visualType | Usage |
|---|---|
| `clusteredBarChart` | Horizontal bars (categories on Y) |
| `clusteredColumnChart` | Vertical bars (categories on X) |
| `stackedBarChart` / `stackedColumnChart` | Stacked horizontal / vertical |
| `lineChart` | Time series / trends |
| `areaChart` | Filled area |
| `lineClusteredColumnComboChart` / `lineStackedColumnComboChart` | Combo (bar + line) |
| `pieChart` / `donutChart` | Proportions (few categories) |
| `card` / `multiRowCard` | Single KPI / multiple KPIs |
| `tableEx` | Flat table |
| `pivotTable` | Matrix / cross-tab |
| `map` / `filledMap` | Bubble map / choropleth |
| `treemap` | Hierarchical proportions |
| `scatterChart` | Scatter / bubble |
| `slicer` | Filter control (see `report-slicers`) |
| `actionButton` | Navigation / bookmark (see `report-navigation-buttons`) |
| `textbox` / `shape` | Text / background shape |
| `kpi` / `gauge` / `waterfallChart` / `funnel` | KPI / radial / waterfall / funnel |

> The canonical Tableau-mark → visualType decision table (including Automatic-mark inference) lives in the **`tableau-mark-mapping`** skill. This skill lists the available `visualType` identifiers; pick the one that the mark-mapping skill resolved.

## Notes

- Visual folder name = visual `name`, no spaces (underscores or camelCase).
- Use `tableEx` for flat tables, `pivotTable` for cross-tabs, `card` for single KPIs, `areaChart` for area marks (NOT `lineChart`).
- Navigation buttons do NOT have a `query` — see `report-navigation-buttons`.

## Anti-Hallucination

- Every field binding MUST reference a real table/column/measure from the semantic model TMDL.
- Generate only visuals that exist in the source dashboard; match counts exactly.
