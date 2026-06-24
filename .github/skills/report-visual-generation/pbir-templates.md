# PBIR Visual Templates Skill

## Purpose

Provide ready-to-use JSON templates for each Power BI visual type in the Enhanced PBIR Folder Format. Copy these templates and fill in the placeholders.

## Folder Structure

```
{Name}.Report/
├── definition.pbir
└── definition/
    ├── report.json
    ├── version.json
    └── pages/
        ├── pages.json
        ├── {PageName}/
        │   ├── page.json
        │   └── visuals/
        │       ├── {visual_name}/
        │       │   └── visual.json
        │       └── ...
```

## Root Files

### report.json
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json",
  "themeCollection": {
    "baseTheme": {
      "name": "CY24SU06",
      "type": "SharedResources",
      "reportVersionAtImport": { "visual": "1.8.95", "report": "2.0.95", "page": "1.3.95" }
    }
  },
  "settings": {
    "useStylableVisualContainerHeader": true,
    "useEnhancedTooltips": true
  }
}
```

> Schema MUST be `3.0.0`. NEVER add `datasetBinding` or `layoutOptimization`.
> `baseTheme` requires `name` + `type` + `reportVersionAtImport`. NEVER add `resourcePackage`.
> `themeCollection: {}` is also valid (uses PBI default theme).

### version.json
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
  "version": "2.0.0"
}
```

### pages.json
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
  "pageOrder": ["PageName1", "PageName2"],
  "activePageName": "PageName1"
}
```

> Schema URL uses `pagesMetadata` (NOT `pages`).

### page.json
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json",
  "name": "PageName1",
  "displayName": "Human Readable Page Title",
  "displayOption": "FitToPage",
  "height": 720,
  "width": 1280
}
```

> `displayOption` MUST be a string: `"FitToPage"`, `"FitToWidth"`, or `"ActualSize"`.

---

## Visual Templates

### Schema Rules (ALL visuals)

The `visualContainer/2.4.0` schema allows ONLY these top-level properties:
- `$schema`
- `name`
- `position`
- `visual` (or `visualGroup`)

**NEVER add `filters`, `filterConfig`, `sorts`, or any other top-level property.**

Inside `visual`, the allowed properties are: `visualType`, `query`, `objects`, `visualContainerObjects`, `drillFilterOtherVisuals`. **NEVER add `sorts`, `filters`, or `dataTransforms` inside `visual`** — PBI Desktop rejects them with "Property has not been defined and the schema does not allow additional properties".

To control sort order in a table/chart, users must configure it in Desktop UI, or use DAX measures with RANKX to force ordering.

---

### Generic Chart Visual (bar, line, area, pie, etc.)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "{unique_name}",
  "position": {
    "x": 25, "y": 25, "z": 0,
    "height": 300, "width": 400, "tabOrder": 0
  },
  "visual": {
    "visualType": "{type}",
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
    "objects": {},
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'{Visual Title}'"}}}
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

### Query Buckets by Visual Type

| visualType | Buckets |
|---|---|
| `clusteredBarChart` / `clusteredColumnChart` | `Category` (axis), `Y` (values) |
| `lineChart` / `areaChart` | `Category` (axis), `Y` (values), `Series` (legend) |
| `pieChart` / `donutChart` | `Category` (slices), `Y` (values) |
| `card` | `Values` only |
| `kpi` | `Indicator` (CY measure), `Goal` (PY measure), `TrendAxis` (date column). **REQUIRES** `objects.status[0].properties.direction` |
| `tableEx` | `Values` (all columns) |
| `pivotTable` | `Rows`, `Columns`, `Values` |
| `treemap` | `Group` (category), `Values` (size) |
| `filledMap` / `map` | `Category` (location), `Y` (values) |
| `scatterChart` | `Category`, `X`, `Y`, `Size` |
| `slicer` | `Values` only |

### Card Visual

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "card_{measure_name}",
  "position": {
    "x": 25, "y": 25, "z": 0,
    "height": 100, "width": 200, "tabOrder": 0
  },
  "visual": {
    "visualType": "card",
    "query": {
      "queryState": {
        "Values": {
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
    "objects": {},
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'{Title}'"}}}
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

### KPI Visual (with sparkline trend)

**CRITICAL**: The KPI visual requires `objects.status[0].properties.direction` to render. Without this, the visual appears blank/empty with no data. Use `"'Positive'"` when higher-is-better (sales, profit, customers), `"'Negative'"` when lower-is-better (costs, churn).

Query buckets: `Indicator` (CY value), `Goal` (PY value for comparison), `TrendAxis` (date field for sparkline).

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "kpi_{measure_name}",
  "position": {
    "x": 25, "y": 25, "z": 0,
    "height": 130, "width": 280, "tabOrder": 0
  },
  "visual": {
    "visualType": "kpi",
    "query": {
      "queryState": {
        "Indicator": {
          "projections": [{
            "field": {
              "Measure": {
                "Expression": {"SourceRef": {"Entity": "{TableName}"}},
                "Property": "{CY_MeasureName}"
              }
            },
            "queryRef": "{TableName}.{CY_MeasureName}",
            "active": true
          }]
        },
        "TrendAxis": {
          "projections": [{
            "field": {
              "Column": {
                "Expression": {"SourceRef": {"Entity": "DimDate"}},
                "Property": "Date"
              }
            },
            "queryRef": "DimDate.Date",
            "active": true
          }]
        },
        "Goal": {
          "projections": [{
            "field": {
              "Measure": {
                "Expression": {"SourceRef": {"Entity": "{TableName}"}},
                "Property": "{PY_MeasureName}"
              }
            },
            "queryRef": "{TableName}.{PY_MeasureName}",
            "active": true
          }]
        }
      }
    },
    "objects": {
      "status": [{"properties": {
        "direction": {"expr": {"Literal": {"Value": "'Positive'"}}}
      }}]
    },
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'{Title}'"}}}
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

### Histogram / Distribution Chart

For Tableau "Customer Distribution by Nr of Orders"-style histograms, use a `clusteredColumnChart` with a **calculated column** as the X-axis category:

1. **Semantic model**: Add a calculated column to the dimension table (e.g., `DimCustomer[Orders Per Customer] = CALCULATE(DISTINCTCOUNT(FactTable[OrderID]))`)
2. **Visual**: Use that column as `Category` and a count measure (e.g., DISTINCTCOUNT of Customer ID) as `Y`

Do NOT map histogram-style visuals to simple bar charts with entity names on the axis. The key distinction: if Tableau shows a **distribution** (binned counts), the PBI equivalent needs a computed bin column.

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "histogram_{dimension}",
  "position": {
    "x": 25, "y": 238, "z": 0,
    "height": 520, "width": 440, "tabOrder": 0
  },
  "visual": {
    "visualType": "clusteredColumnChart",
    "query": {
      "queryState": {
        "Category": {
          "projections": [{
            "field": {
              "Column": {
                "Expression": {"SourceRef": {"Entity": "{DimTable}"}},
                "Property": "{BinColumn}"
              }
            },
            "queryRef": "{DimTable}.{BinColumn}",
            "active": true
          }]
        },
        "Y": {
          "projections": [{
            "field": {
              "Measure": {
                "Expression": {"SourceRef": {"Entity": "{FactTable}"}},
                "Property": "{CountMeasure}"
              }
            },
            "queryRef": "{FactTable}.{CountMeasure}",
            "active": true
          }]
        }
      }
    },
    "objects": {},
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'{Title}'"}}}
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

### Slicer Visual

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

> **CRITICAL**: Slicer `title.show` MUST be `"false"`. Slicers use their built-in header (field name) as label. Setting title to true creates duplicate labels.

Slicer modes: `'Basic'` (list), `'Dropdown'` (compact), `'Slider'` (range).

### Table Visual (`tableEx`)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "table_{name}",
  "position": {
    "x": 25, "y": 25, "z": 0,
    "height": 300, "width": 600, "tabOrder": 0
  },
  "visual": {
    "visualType": "tableEx",
    "query": {
      "queryState": {
        "Values": {
          "projections": [
            {
              "field": {"Column": {"Expression": {"SourceRef": {"Entity": "{Table}"}}, "Property": "{Col1}"}},
              "queryRef": "{Table}.{Col1}", "active": true
            },
            {
              "field": {"Column": {"Expression": {"SourceRef": {"Entity": "{Table}"}}, "Property": "{Col2}"}},
              "queryRef": "{Table}.{Col2}", "active": true
            }
          ]
        }
      }
    },
    "objects": {},
    "visualContainerObjects": {
      "title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'{Title}'"}}}
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

---

## PBIP Entry Point Files

### .pbip
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
  "version": "1.0",
  "artifacts": [{"report": {"path": "{Name}.Report"}}],
  "settings": {"enableAutoRecovery": true}
}
```

> NEVER add `"dataset"` artifact. Semantic model reference lives in `definition.pbir`.

### definition.pbir
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
  "version": "4.0",
  "datasetReference": {"byPath": {"path": "../{Name}.SemanticModel"}}
}
```

### definition.pbism
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
  "version": "4.2",
  "settings": {}
}
```

---

## Encoding Notes

- ALL files MUST be UTF-8 WITHOUT BOM
- PowerShell: use `[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))`
- NEVER use `Set-Content -Encoding UTF8` (adds BOM in PowerShell 5.1)
- Or use the `create_file` tool which writes without BOM
