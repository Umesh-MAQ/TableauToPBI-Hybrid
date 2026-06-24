# Report Layout & Gapping Skill

## Purpose

Position Power BI report visuals so they never overlap and maintain consistent spacing. Single-responsibility companion to the report visual generation pipeline. Covers canvas sizing, edge padding, inter-visual gap, coordinate scaling, and overlap validation.

## When to Use

- During report visual generation, when computing each visual's `position` (x, y, width, height)
- Before writing `visual.json` files — positions MUST pass the no-overlap check first

## Canvas & Page Size

- **Default canvas**: 1280 × 720 px (Power BI 16:9).
- **Fidelity override**: if the Tableau dashboard `<size maxwidth maxheight>` differs, set `page.json` `width`/`height` to those EXACT pixel values instead of defaulting to 1280×720.
- All visuals MUST fit within the canvas: `x + width ≤ canvas_width - 25` and `y + height ≤ canvas_height - 25`.

## Spacing Rules (MANDATORY)

- **Edge padding**: 25px from top and sides.
- **Inter-visual gap**: 20px between visuals — visuals must NEVER overlap.
- **Minimum heights**: slicers ≥ 55px, cards ≥ 80px, charts ≥ 130px.
- **Same-row consistency**: all visuals in the same logical row MUST share identical `y` and `height` values.

## Coordinate Scaling (Tableau → Power BI)

Tableau zone coordinates may be pixel values or in a 0–100000 space. Detect which:

- **Pixel zones**: scale to canvas —
  ```
  scale_x = canvas_width  / tableau_dashboard_width
  scale_y = canvas_height / tableau_dashboard_height
  pbi_x      = (tableau_x * scale_x) + edge_padding
  pbi_y      = (tableau_y * scale_y) + edge_padding
  pbi_width  = (tableau_w * scale_x) - inter_visual_gap
  pbi_height = (tableau_h * scale_y) - inter_visual_gap
  ```
- **0–100000 zones**: convert to pixels first —
  ```
  px = zone_value / 100000 * dashboard_dimension
  ```
  then apply the scaling above.

Subtracting the full 20px gap from width/height guarantees minimum spacing regardless of source coordinates. Apply constitution padding/gap rules after scaling.

## No-Overlap Validation (MANDATORY)

After computing all positions, validate EVERY pair of visuals:

- **Horizontally adjacent** (y-ranges overlap): `left.x + left.width + 20 ≤ right.x`
- **Vertically adjacent** (x-ranges overlap): `top.y + top.height + 20 ≤ bottom.y`
- **Edge constraints**: `x + width ≤ canvas_width - 25`; `y + height ≤ canvas_height - 25`
- **Row check**: `previous_row_bottom + 20 ≤ current_row_y`

If ANY overlap is detected, shrink the offending width/height by 20px (or reflow) and re-validate BEFORE writing files.

## Anti-Hallucination

- Derive every position from extracted Tableau zone coordinates — never invent layouts to fill space.
- Number of visuals per page = that dashboard's zone count; do not add decorative visuals.
