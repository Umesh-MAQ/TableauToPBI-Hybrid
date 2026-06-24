# Tableau Mark Mapping Skill

## Purpose

The canonical decision table for mapping a Tableau mark type to a Power BI `visualType`, including Automatic-mark inference. Single-responsibility companion to the Tableau visual extraction pipeline. This is the authority other skills defer to for chart-type selection.

## When to Use

- During worksheet extraction, to resolve each `<mark class='...'>` to a Power BI visual type
- Whenever a generated visual's type must match the Tableau source

## Primary Visual Type Mapping (use FIRST match)

| Tableau Mark | Power BI Visual | When to Use |
|---|---|---|
| Text (single measure) | `card` | Single KPI number only |
| Text (multiple rows/cols) | `tableEx` | Flat data table (no subtotals) |
| Text (rows + cols as dimensions with measures) | `pivotTable` | Cross-tab / matrix with subtotals |
| Bar (horizontal) | `clusteredBarChart` | Categorical comparison |
| Bar (vertical / columns shelved) | `clusteredColumnChart` | Categorical comparison |
| Line | `lineChart` | Trend over time (no fill) |
| Line + color on dimension | `lineChart` with Legend | Trend with series/highlight |
| Area | `areaChart` | Trend over time (filled) |
| Circle / Shape | `scatterChart` | Two-measure comparison |
| Pie | `pieChart` or `donutChart` | Proportions (few categories) |
| Map | `map` or `filledMap` | Geographic data |
| Gantt | `decompositionTree` | Approximate (no direct equivalent) |
| Square (single dimension + size/color on measure) | `treemap` | Hierarchical proportions |
| Square (dimensions on BOTH rows AND cols + color on measure) | `pivotTable` with conditional formatting | Heatmap / cross-tab |
| Polygon | `shape` | Custom shapes (approximate) |
| Automatic | **Infer** (see below) | Context-dependent |

## Automatic Mark Inference (mark class = "Automatic")

0. **`[:Measure Names]` on `<cols>` or `<rows>` ⇒ TEXT TABLE (HIGHEST PRIORITY).** This is the single
   most reliable table signal and overrides rules 1–8 below. A `Measure Names` field means multiple
   measures are displayed as columns — the worksheet is a crosstab, NOT a chart.
   - ONE dimension on the other shelf → `tableEx`.
   - Dimensions on BOTH rows and cols → `pivotTable`.
   - Include EVERY measure from the Measure Values card as a column (enumerate from the dashboard
     screenshot / `<datasource>` measure list — do not drop Style Count, % of Total, etc.).
   - **NEVER** turn a `Measure Names` worksheet into a single-measure bar/column chart. This exact
     mistake (Automatic mark + Measure Names rendered as a bar) silently loses columns.
1. **Color + Size on SAME measure + text/label on dimension** → `treemap`
2. **Date/time on cols + measure on rows** → `lineChart` or `areaChart`
3. **Only dimensions on rows + measures on text** → `tableEx`
4. **Dimensions on BOTH rows AND cols + measures** → `pivotTable`
5. **Single measure, no dimensions** → `card`
6. **Geographic field present** → `map`
7. **Hierarchy on rows (multiple dims with `/`) + measure on cols** → `clusteredBarChart` with drill (or `matrix` if text encoding present)
8. **Dimension on one axis + measure on other (and NO `Measure Names`)** → `clusteredBarChart` or `clusteredColumnChart`

> **Priority**: Rule 0 wins over everything — `Measure Names` on a shelf ⇒ table/matrix. Then Rule 1 —
> BOTH color AND size on a measure ⇒ `treemap`. A single measure with NO `Measure Names` is the ONLY case
> that becomes a bar/column chart.

## Square Mark Disambiguation

- Color on measure + dims on BOTH rows AND cols → `matrix` (heatmap), NOT treemap.
- Only rows + size/color on measure → `treemap`.
- Single dimension + size → `treemap`.

## Text Mark: Table vs Matrix vs Card

- ONLY `<rows>` has dimensions, measures on `<text>` → `tableEx`.
- BOTH `<rows>` AND `<cols>` have dimensions → `pivotTable`.
- Single aggregate, no dimensions → `card`.
- **NEVER** map a text table to a bar/column/line chart.

## Orientation Rule (preserve from Tableau)

- `clusteredColumnChart` = VERTICAL bars (categories on X).
- `clusteredBarChart` = HORIZONTAL bars (categories on Y).
- Bar mark with dimension on rows → horizontal (`clusteredBarChart`).
- Bar mark with dimension on cols → vertical (`clusteredColumnChart`).

## When No Direct Equivalent Exists

- **ASK THE USER** via `vscode_askQuestions`; present 2–3 closest alternatives with descriptions; record the choice in the extraction output. Do NOT silently default to bar/column.

## Anti-Hallucination

- Resolve the type from the actual mark + shelves/encodings — never assume bar/column.
- A treemap stays a treemap, a heatmap becomes a matrix, a pie stays a pie.
