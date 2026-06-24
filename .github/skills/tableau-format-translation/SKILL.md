# Tableau Format Translation Skill

## Purpose

Translate Tableau field format strings into Power BI `formatString` values, and define the extraction output document structure. Single-responsibility companion to the Tableau visual extraction pipeline.

## When to Use

- During visual extraction, when capturing field formats and writing `tableau-visuals-output.md`
- When applying source formats to Power BI visual fields/measures

## Format String Translation (Tableau → Power BI)

Only include rows for formats actually found in the workbook; otherwise write `None`. Do NOT guess formats.

| Tableau Format | Power BI formatString | Notes |
|---|---|---|
| `$#,##0` / `$#,##0.00` | `\$#,##0` / `\$#,##0.00` | Currency (escape `$`) |
| `0%` / `0.0%` | `0%` / `0.0%` | Percentage |
| `#,##0` / `#,##0.00` | `#,##0` / `#,##0.00` | Thousands separator |
| `0.0"K"` / `0.0,"M"` | `#,0.0,"K"` / `#,0.0,,"M"` | Scaled abbreviations |
| `mmmm yyyy` | `mmmm yyyy` | Month-Year |
| `mm/dd/yyyy` | `mm/dd/yyyy` | Date |
| `[h]:mm:ss` | `h:mm:ss` | Duration (Power BI has no elapsed `[h]`) |
| (no format) | `Default` | Leave model default |

- Where no format was captured for a field, leave the model default — do not invent one.

## Extraction Output Document

Save extracted visualization metadata to `.specify/memory/{WorkbookName}/tableau-visuals-output.md`:

```markdown
# Tableau Visual Extraction: {workbook_name}

## Dashboard Layout
- **Name**: {dashboard_name}
- **Size**: {width} × {height} px
- **Layout Type**: Tiled / Floating / Mixed

## Visual Inventory

### Visual 1: {worksheet_name}
- **Chart Type**: {Tableau mark} → {Power BI visual type}
- **Position**: x={x}, y={y}, w={w}, h={h}
- **X-Axis (Columns)**: {field} ({aggregation})
- **Y-Axis (Rows)**: {field} ({aggregation})
- **Color**: {field}
- **Size**: {field}
- **Labels**: {field}
- **Secondary Axis / Combo**: {second measure + mark type, or None}
- **Analytics Lines**: {constant/average/trend line details, or None}
- **Filters**: {filter_description}
- **Format**: {number_format}
- **Sort**: {sort_field} {direction}

## Filters / Slicers
| Filter Name | Type | Field | Position | Style |
|---|---|---|---|---|

## Parameter Controls
| Parameter | Control Type | Position |
|---|---|---|

## Navigation Buttons
### {dashboard_name}
| # | Action Type | Tooltip | Target | x | y | w | h |
|---|-------------|---------|--------|---|---|---|---|

## Color Palette
| Field Value | Color (Hex) |
|---|---|

## Formatting Summary
| Property | Value |
|---|---|
| Title Font | {font} {size} |
| Body Font | {font} {size} |
| Number Format | {format} |
| Date Format | {format} |
```

## Completeness Gate (before handoff)

- [ ] Mark type for EVERY worksheet (from `tableau-worksheet-extraction`)
- [ ] Dashboard zone positions for ALL dashboards (from `tableau-dashboard-extraction`)
- [ ] Color/Size/Text encodings where present
- [ ] Rows/cols captured
- [ ] Navigation buttons for ALL dashboards with `<button>` elements
- [ ] Dual-axis/combo recorded per worksheet (value or `None`)
- [ ] Reference/trend lines recorded per worksheet (value or `None`)
- [ ] Output file exists and contains the Visual Inventory table

## Anti-Hallucination

- Copy format strings verbatim (after XML entity decoding); never rewrite or guess them.
- Write `None`/`Default` for empty categories instead of fabricating entries.
