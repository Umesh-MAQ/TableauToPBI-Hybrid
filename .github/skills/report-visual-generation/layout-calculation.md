# Layout Calculation & Coordinate Scaling Skill

## Purpose

Convert Tableau dashboard zone coordinates to Power BI visual positions while enforcing gap rules, padding, and minimum height constraints.

## Coordinate Scaling Formula

```
scale_x = pbi_canvas_width / tableau_dashboard_width
scale_y = pbi_canvas_height / tableau_dashboard_height

pbi_x = (tableau_zone_x * scale_x) + edge_padding
pbi_y = (tableau_zone_y * scale_y) + edge_padding
pbi_width = (tableau_zone_w * scale_x) - inter_visual_gap
pbi_height = (tableau_zone_h * scale_y) - inter_visual_gap
```

### Default Values
- PBI canvas: 1280 × 720 px (use actual Tableau dashboard size for scaling source)
- Edge padding: 25px from all edges
- Inter-visual gap: 20px minimum

### Custom Canvas Size
If Tableau dashboard specifies `maxwidth` and `maxheight`, use those as the PBI page dimensions instead of 1280×720. Set `width` and `height` in `page.json` accordingly.

---

## Gap Enforcement Rules

### No-Overlap Rule (MANDATORY)

After calculating ALL positions, validate every pair of visuals:

**Horizontal adjacency** (visuals in same row, y ranges overlap):
```
visual_A.x + visual_A.width + 20 ≤ visual_B.x
```

**Vertical adjacency** (visuals in same column, x ranges overlap):
```
visual_A.y + visual_A.height + 20 ≤ visual_B.y
```

**If ANY overlap is detected**: Shrink width/height by the gap amount (20px) to create space.

### Edge Constraints
```
visual.x ≥ 25                          (left padding)
visual.y ≥ 25                          (top padding)
visual.x + visual.width ≤ canvas_width - 25   (right padding)
visual.y + visual.height ≤ canvas_height - 25  (bottom padding)
```

### Row Alignment Rule
All visuals in the same logical row MUST have:
- Identical `y` values
- Identical `height` values

---

## Minimum Height Constraints

| Visual Type | Minimum Height |
|---|---|
| Slicer | 55px |
| Card | 80px |
| Chart (bar, line, area, pie, etc.) | 130px |
| Table / Matrix | 150px |
| Navigation Button | 35px |

If calculated height is below minimum, clamp to minimum and recalculate remaining layout.

---

## Layout Strategy

### Step 1: Identify Rows

Group visuals by similar `y` position (within 10px tolerance = same row):
```
Row 1: visuals with y ≈ 25-80 (slicers/nav buttons)
Row 2: visuals with y ≈ 100-180 (KPI cards)
Row 3: visuals with y ≈ 200+ (main charts)
```

### Step 2: Calculate Per-Row

For each row:
1. Count visuals (N)
2. Available width = `canvas_width - 2*edge_padding - (N-1)*gap`
3. Each visual width = `available_width / N` (or use proportional Tableau widths)
4. Set all heights to max(calculated_height, minimum_height)
5. Set all y values identical within the row

### Step 3: Stack Rows

```
row_1_y = edge_padding (25)
row_2_y = row_1_y + row_1_height + gap (20)
row_3_y = row_2_y + row_2_height + gap (20)
...
```

Verify: `last_row_y + last_row_height + edge_padding ≤ canvas_height`

### Step 4: Handle Overflow

If total height exceeds canvas:
1. Reduce chart heights (keep above minimum)
2. If still overflows, reduce gaps to 15px
3. If still overflows, split into multiple pages

---

## Tableau Zone Coordinate Space

Tableau zones may use:
- **Pixel coordinates**: direct `x`, `y`, `w`, `h` in pixels (most common)
- **Normalized coordinates** (0–100000 range): convert with `px = zone_value / 100000 * dashboard_dimension`

Detect which system by checking if values exceed dashboard dimensions. If `x` or `y` > `maxwidth`/`maxheight`, it's normalized.

---

## Example Calculation

**Input**: Tableau dashboard 1366×768, visual zone at x=100, y=200, w=600, h=400

```
scale_x = 1280 / 1366 = 0.937
scale_y = 720 / 768 = 0.938

pbi_x = (100 * 0.937) + 25 = 119
pbi_y = (200 * 0.938) + 25 = 213
pbi_width = (600 * 0.937) - 20 = 542
pbi_height = (400 * 0.938) - 20 = 355
```

---

## Validation Checklist

- [ ] No visual overlaps (20px gap minimum between all adjacent visuals)
- [ ] All visuals within canvas bounds (25px edge padding)
- [ ] All heights meet minimums (slicers ≥55, cards ≥80, charts ≥130)
- [ ] Visuals in same row have identical y and height
- [ ] Total layout fits within canvas (no overflow beyond bottom edge)
- [ ] Navigation buttons grouped at consistent y with equal spacing
