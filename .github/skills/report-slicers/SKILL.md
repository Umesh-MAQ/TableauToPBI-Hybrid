# Report Slicers Skill

## Purpose

Generate Power BI slicer visuals from Tableau filter and parameter controls. Single-responsibility companion to the report visual generation pipeline. Enforces the slicer-specific template and the critical "title disabled" rule.

## When to Use

- During report visual generation, for EVERY Tableau filter control or parameter control
- Whenever `visualType` is `slicer`

## Slicer Template (use INSTEAD of the generic visual template)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "slicer_{field_name}",
  "position": {
    "x": 25, "y": 25, "z": 0,
    "height": 55, "width": 300, "tabOrder": 0
  },
  "visual": {
    "visualType": "slicer",
    "query": {
      "queryState": {
        "Values": {
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
        }
      }
    },
    "objects": {
      "data": [{"properties": {"mode": {"expr": {"Literal": {"Value": "'Dropdown'"}}}}}]
    },
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "false"}}}
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

## ⚠️ CRITICAL — `title.show` MUST be `"false"`

- Slicers have a BUILT-IN **header** that automatically displays the field name.
- Setting `title.show = true` creates a DUPLICATE label (header text + title text stacked). This is the #1 most common slicer mistake.
- This rule OVERRIDES the generic visual template — do NOT copy `title.show = true` from chart templates.
- **WRONG**: `"show": {"expr": {"Literal": {"Value": "true"}}}`
- **CORRECT**: `"show": {"expr": {"Literal": {"Value": "false"}}}`
- The slicer header color/font can be styled via `objects.header[].properties.fontColor`.

## Slicer Modes

Set via `objects.data[].properties.mode`:

- `'Basic'` — list
- `'Dropdown'` — compact (default for migrations)
- `'Slider'` — numeric range

## Sizing

- Minimum height ≥ 55px (per layout rules in `report-layout-gapping`).
- Border still required (per `report-borders-titles`) even though the title is off.

## Anti-Hallucination

- Generate a slicer ONLY for filter/parameter controls that exist in the Tableau dashboard.
- Field binding (`Entity`/`Property`) MUST match a real table/column in the semantic model TMDL.
