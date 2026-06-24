# Report Theme & Colors Skill

## Purpose

Decide and apply the report color theme for migrated Power BI visuals: copy the Tableau theme when one exists, otherwise fall back to the standard professional theme. Single-responsibility companion to the report visual generation pipeline.

## When to Use

- During report visual generation, BEFORE writing any `visual.json`
- Whenever a visual needs background, data-point, title, axis, or legend colors

## Theme Decision Rule (MANDATORY)

1. **If the Tableau workbook has a distinctive theme** (dark mode, brand colors, custom mark palette), **copy it**:
   - Extract ALL `style-rule` entries from the TWB `<style>` element: `background-color`, mark color (`style-rule element='mark'`), `font-color`, `title-color`, axis/legend colors
   - Save extracted colors to `.specify/memory/{WorkbookName}/theme-overrides.md`
   - Apply those colors to every visual (see mapping below)
2. **If the workbook has NO distinctive theme**, use the **standard professional theme**:
   - Page background: white
   - Visual background: white or light gray
   - Border: subtle light gray `#E0E0E0`
   - Data points: default professional palette (e.g. `#4e79a7`, `#f28e2b`, `#e15759`, `#76b7b2`, `#59a14f`)
3. **NEVER overwrite the universal `.specify/memory/report-constitution.md`** — workbook theme overrides are layered on top, saved per workbook.

## Color Application Mapping

Apply the chosen palette to these specific properties on EVERY applicable visual:

| Target | Property path |
|--------|---------------|
| Page background | `page.json` background / wallpaper |
| Visual background | `visualContainerObjects.background[].properties.color` |
| Visual border | `visualContainerObjects.border[].properties.color` |
| Data point fill | `visual.objects.dataPoint[].properties.fill` |
| Title font color | `visual.objects.title[].properties.fontColor` |
| Category axis labels | `visual.objects.categoryAxis[].properties.labelColor` |
| Value axis labels | `visual.objects.valueAxis[].properties.labelColor` |
| Legend text | `visual.objects.legend[].properties.labelColor` |

## Color Value Format (PBIR)

```json
{"solid": {"color": {"expr": {"Literal": {"Value": "'#RRGGBB'"}}}}}
```

- Hex must be single-quoted INSIDE the `Value` string.
- For charts with a legend (multiple series), map each series value to a palette entry; reuse TWB color-encoding entries when present.

## Data Color Consistency

- Extract mark colors from TWB `style-rule element='mark'` and apply to `visual.objects.dataPoint[].properties.fill`.
- For series charts, build a value→color map from the TWB color-encoding palette.
- Example (Netflix standard, or extract from TWB): Movie = `#d3293d`, TV Show = `#ffbeb2`.

## Border vs Theme Note

The mandatory border rule (every visual gets a border) lives in the **`report-borders-titles`** skill. This skill only decides the COLOR of that border based on the theme (`#E0E0E0` for light themes, `#333333` for dark themes).

## Anti-Hallucination

- Copy hex values verbatim from the TWB — never invent brand colors.
- If no theme colors are found, use the standard palette above — do NOT guess workbook-specific colors.
- Record the decision (copied vs standard) in `theme-overrides.md` when colors were extracted.
