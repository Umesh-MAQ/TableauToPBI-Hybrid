# Report Borders & Titles Skill

## Purpose

Apply the mandatory border and title formatting to every Power BI report visual. Single-responsibility companion to the report visual generation pipeline. Enforces the "all visuals have a border" rule and the correct placement of title formatting.

## When to Use

- During report visual generation, for EVERY `visual.json` written
- Whenever deciding where title / border / background formatting goes

## Border Rule (MANDATORY — ALL visuals)

Every visual MUST include `visualContainerObjects.border` with `show: true`:

```json
"border": [{"properties": {
  "show": {"expr": {"Literal": {"Value": "true"}}},
  "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#E0E0E0'"}}}}},
  "radius": {"expr": {"Literal": {"Value": "4D"}}}
}}]
```

- Default color: `#E0E0E0` (light themes) or `#333333` (dark themes — see `report-theme-colors`).
- Radius: `4D` (4px rounded corners). Note the `D` suffix is required.
- This applies to charts, tables, cards, AND slicers.

## Title Rule

```json
"title": [{"properties": {
  "show": {"expr": {"Literal": {"Value": "true"}}},
  "text": {"expr": {"Literal": {"Value": "'Visual Title'"}}}
}}]
```

- **Title text MUST equal the Tableau worksheet name exactly** — do NOT paraphrase, abbreviate, or "improve" it. Example: "Total Movies and TV Shows By Years" stays verbatim.
- For title COLOR, use `visual.objects.title[].properties.fontColor` (visual-type formatting), NOT `visualContainerObjects`.
- **Slicers**: set `title.show = false` — the slicer's built-in header already shows the field name. See the **`report-slicers`** skill.

## `objects` vs `visualContainerObjects` (CRITICAL)

- `visualContainerObjects` = container-level chrome (title text/show, background, border). Same keys for ALL visual types.
- `objects` = visual-type-specific formatting (data colors, axis, labels, title fontColor). Keys vary by visual type.
- **NEVER** put `title` (text/show), `background`, or `border` inside `objects`.
- **NEVER** put a `"general"` block with `title` inside `objects` — invalid, causes crashes.
- `title.text`/`title.show` → `visualContainerObjects`; `title.fontColor` → `objects`.

## Allowed Title Properties

`visualContainerObjects.title` only allows: `show`, `text`. Do NOT add `color` or `fontSize` here (those go in `objects.title`).

## Anti-Hallucination

- Title text is copied from the Tableau worksheet name — never fabricated.
- Apply the border to every visual without exception; do not skip it for "minor" visuals.
