# Tableau Worksheet Extraction Skill

## Purpose

Extract per-worksheet visual encodings from the TWB XML: mark type, field shelves, encodings, aggregation, filters, formatting, dual-axis/combo, and reference/trend lines. Single-responsibility companion to the Tableau visual extraction pipeline.

## When to Use

- During visual extraction, for EVERY `<worksheet>` element
- BEFORE generating any Power BI visual (this feeds chart-type + binding decisions)

## ⚠️ Parse the Actual TWB XML

The high-level `tableau-analysis-output.md` only has datasource metadata — it does NOT contain mark types, shelves, or zone positions. You MUST parse the real `.twb` XML.

## Extract Per Worksheet

For each `<worksheet name='...'>`, read `<table><panes><pane>`:

### Mark Type
- `<mark class='...'>` → resolve via the **`tableau-mark-mapping`** skill (Automatic ⇒ infer).

### Field Shelves
- `<rows>` → Y-axis fields (may contain hierarchy joined by `/`).
- `<cols>` → X-axis fields.

### Encodings (`<pane><encodings>`)
- `<color column='...'>` → legend/color series
- `<size column='...'>` → size (treemap indicator)
- `<text column='...'>` → data labels
- `<wedge-size column='...'>` → pie measure
- `<detail column='...'>` → tooltip detail
- `<shape column='...'>` / `<lod column='...'>` → shape / level of detail

### Aggregation & Derivation
- Column-instance `derivation` (Sum, Avg, Count, Min, Max, None, User).
- `type` (quantitative, nominal, ordinal). Example: `[sum:loan_amount:qk]` = SUM, quantitative.

### Filters
- `<filter>` elements: categorical member include/exclude, range min/max, relative date.

### Formatting
- `<format>` in `<style-rule>`: `text-format` (number/%/currency), `font-size`, `text-align`, `width`/`height`.
- `<customized-label>`; color palettes from `<encoding attr='color'>`.

### Table Calculation Context
- `<table-calc>`: `ordering-type` (Field/Rows/Columns), `ordering-field` — affects how the DAX equivalent should be visualized.

### Dual-Axis / Combo (detect; write `None` if absent)
- TWO measure pills on one axis, multiple `<encodings>` blocks, or a `<dual-axis>` marker.
- Identify each measure's mark (e.g. Bar + Line).
- Map: Bar/Column + Line → `lineClusteredColumnComboChart`/`lineStackedColumnComboChart`; two lines independent scale → `lineChart` + secondary value axis; two bars → `clusteredColumnChart` shared scale.
- Record both measures, both marks, and which is the secondary axis.

### Reference / Trend / Constant Lines (detect; write `None` if absent)
- `<reference-line>`, `<reference-line-aggregation>`, `<trend-lines>`.
- Extract line type (constant/average/median/total/min-max/trend), value/aggregation, scope (per-cell/pane/table), label.
- Map: constant/average/min/max → analytics-pane lines; trend → analytics-pane trend line (on `lineChart`/`scatterChart`).

## Validation Gate (per worksheet)

- [ ] Mark type extracted for EVERY worksheet
- [ ] Color/Size/Text encodings captured where present (crucial for treemap/legend)
- [ ] Rows/cols captured (axis orientation)
- [ ] Dual-axis/combo recorded (value or `None`)
- [ ] Reference/trend lines recorded (value or `None`)

If any worksheet is missing mark/encoding data, **re-parse the TWB** before proceeding.

## PowerShell Reference

```powershell
[xml]$twb = Get-Content "Data/{subfolder}/{workbook}.twb" -Raw
$twb.workbook.worksheets.worksheet | ForEach-Object {
    $name = $_.name
    $pane = $_.table.panes.pane
    if ($pane -is [array]) { $pane = $pane[0] }
    $mark  = if ($pane.mark -is [array]) { $pane.mark[0].class } else { $pane.mark.class }
    $color = $pane.encodings.color.column
    $size  = $pane.encodings.size.column
    $text  = $pane.encodings.text.column
    $wedge = $pane.encodings.'wedge-size'.column
    $rows  = $_.table.rows
    $cols  = $_.table.cols
    Write-Host "$name | mark=$mark | rows=$rows | cols=$cols | color=$color | size=$size | text=$text | wedge=$wedge"
}
```

## Anti-Hallucination

- Extract only what the XML contains; write `None` for empty categories.
- Do not invent encodings, filters, combo measures, or reference lines.
