# Report Constitution: Visual Layer Standards

## Canvas & Layout

| Property | Value |
|---|---|
| Canvas Size | 1280 × 720 px (16:9) |
| Edge Padding (Top) | 25px |
| Edge Padding (Left/Right) | 25px |
| Inter-Visual Gap | **20px minimum** — visuals MUST NOT overlap |
| Max Visuals Per Page | 8–10 (avoid clutter) |
| Alignment | Grid-aligned, consistent row heights |

> **CRITICAL GAP RULE**: When calculating visual positions, ensure `visual_x + visual_width + 20px ≤ next_visual_x` (horizontal) and `visual_y + visual_height + 20px ≤ next_visual_y` (vertical). Visuals MUST NEVER overlap. Subtract gap from calculated width/height, not add to position.

## Typography

| Property | Value |
|---|---|
| Font Family | Aptos |
| Data Font Size | 10pt |
| Visual Title Size | 12pt bold |
| Page Title Size | 14pt bold |
| Axis Labels | 10pt |
| Data Labels | 10pt |
| Slicer Text | 10pt |
| Tooltip Text | 10pt |

## Theme & Colors

| Property | Value |
|---|---|
| Background | Professional — White (#FFFFFF) canvas |
| Visual Background | Light gray (#F5F5F5) or White |
| Border | **ENABLED** — 1px solid #E0E0E0 on ALL visuals |
| Border Radius | 4px (subtle rounded corners) |
| Shadow | None (clean professional look) |
| Primary Color | #2C3E50 (dark blue-gray) |
| Accent Colors | Professional palette: #3498DB, #2ECC71, #E74C3C, #F39C12, #9B59B6, #1ABC9C |
| Gridlines | Light (#EBEBEB), minimal |
| Title Color | #2C3E50 |
| Data Color | #333333 |

## Table/Matrix Visual Rules

| Data Type | Alignment | Format |
|---|---|---|
| Numbers | Left-aligned | Preserve source format (commas, decimals) |
| Text | Right-aligned | As-is |
| Dates | Left-aligned | Preserve source format (dd/MM/yyyy or as source) |
| Currency | Left-aligned | Source currency symbol + 2 decimal |
| Percentage | Left-aligned | Source format with % |

## Data Format Preservation

- **Principle**: Data format must remain the same as the source Tableau workbook
- Number of decimal places: Same as source
- Currency symbol: Same as source
- Date format pattern: Same as source
- Thousands separator: Same as source
- Do NOT auto-convert formats unless Tableau format is incompatible with Power BI

## Visual Behavior

| Property | Value |
|---|---|
| Responsive | Enabled |
| Data Labels | Show where space permits (≤6 data points) |
| Legend | Show only when multiple series |
| Axis Titles | Show when not self-explanatory |
| Tooltips | Enable with relevant measures |
| Sort | Preserve Tableau sort order |

## Slicer Standards

| Property | Value |
|---|---|
| Style | Dropdown (for >5 items), List (for ≤5) |
| Position | Top row or right panel |
| Multi-select | Enabled by default |
| "Select All" | Show for multi-select slicers |
| Font | Aptos 10pt |
| **Title** | **DISABLED (`show: false`)** — slicers use header only |
| **Header** | **ENABLED** — shows the field name as the slicer label |

> **CRITICAL**: Slicers must NEVER have both title and header active. Use ONLY the slicer header (built-in field label). Set `visualContainerObjects.title.show = false` for all slicers.

## Page Layout Strategy

1. **Title bar** (top 50px): Page title + key slicers
2. **KPI row** (next 100px): Card visuals for key metrics
3. **Main area** (remaining): Charts and tables in grid
4. **If dashboard has >8 visuals**: Split into multiple pages with logical grouping

## Naming Conventions

| Item | Convention |
|---|---|
| Page names | Same as Tableau dashboard name (Title Case) |
| Visual titles | Same as Tableau worksheet name (descriptive) |
| Bookmark names | Action + Context (e.g., "Show Details") |
