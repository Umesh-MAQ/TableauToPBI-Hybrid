# Tableau Dashboard Extraction Skill

## Purpose

Extract dashboard-level layout from the TWB XML: dashboard size, zone positions, navigation/toggle buttons, layout containers, and title. Single-responsibility companion to the Tableau visual extraction pipeline.

## When to Use

- During visual extraction, for EVERY `<dashboard>` element
- BEFORE positioning Power BI visuals (this feeds coordinate scaling + page sizing)

## ⚠️ Extract ALL Dashboards

Extract EVERY dashboard in the workbook — each becomes a separate Power BI report page. Maintain visual order and relative positioning per dashboard.

## Dashboard Size

- `<size maxheight maxwidth minheight minwidth>` → dashboard pixel dimensions (Tableau default often 1000×800 or 1366×768).
- Use these for page sizing and coordinate scaling (see `report-layout-gapping`).

## Zone Layout (Visual Positions)

Each `<zone>` is a container:

- `type='text'` → title/text box
- `type='viz'` → embedded worksheet
- `type='filter'` → filter control / slicer
- `type='paramctrl'` → parameter control
- `type='bitmap'` → image
- `type-v2='dashboard-object'` with `<button>` child → navigation/toggle button

Extract per zone: `x`, `y`, `w`, `h`; `name` (embedded worksheet); `param` (controlled parameter). Tableau zone values may be pixels OR in a 0–100000 space — convert with `px = zone_value / 100000 * dashboard_dimension` when needed.

## Navigation Buttons (MANDATORY)

For each `<zone type-v2='dashboard-object'>` containing a `<button>`:

1. **Action type**:
   - `action='tabdoc:goto-sheet window-id="..."'` → page navigation
   - `<toggle-action>tabdoc:toggle-button-click-action...</toggle-action>` → toggle visibility
   - Empty `action=''` with `<toggle-action>` child → toggle button
2. **Visual states** (`<button-visual-state>`): `<tooltip-text>` (label), `<image-path>` (icon, reference only).
3. **Position**: `x`, `y`, `w`, `h` from the zone.
4. **Target resolution**:
   - `goto-sheet`: extract `window-id`, match `<window class='dashboard' name='...'>` to get the target dashboard name.
   - toggle: extract `zone-ids` from `<toggle-action>` to identify shown/hidden zones.
5. **Container**: parent `<zone friendly-name='...'>` + `layout-strategy-id` (e.g. "distribute-evenly") defines button group layout.

Output per dashboard:
```markdown
### Navigation Buttons: {dashboard_name}
| # | Action Type | Tooltip | Target | x | y | w | h |
|---|-------------|---------|--------|---|---|---|---|
| 1 | goto-sheet | Go to Sales Dashboard | Sales Dashboard | {x} | {y} | {w} | {h} |
| 2 | toggle | Show/Close Filters | zones: [6] | {x} | {y} | {w} | {h} |
```

**Power BI mapping** (informs `report-navigation-buttons`):
- `goto-sheet` → `actionButton` with `visualLink.type = 'PageNavigation'` and `navigationSection` = target page name.
- `toggle` → `actionButton` with `visualLink.type = 'Bookmark'`, wired to an auto-generated Show/Hide bookmark pair under `definition/bookmarks/` (see `report-navigation-buttons` — no manual Desktop setup).

## Layout Containers

- `<layout-component>`: `layout-flow: tb` (vertical stack), `layout-flow: lr` (horizontal stack), `layout-basic` (tiled/floating).

## Title & Subtitle

- Dashboard `<formatted-text>` for title; capture font name, size, color, alignment.

## Validation Gate (per dashboard)

- [ ] Dashboard size extracted
- [ ] Zone positions (x, y, w, h) for ALL visuals
- [ ] Navigation buttons extracted for ALL dashboards that contain `<button>` elements

## Anti-Hallucination

- Extract only zones/buttons present in the XML; write `None` when a category is empty.
- Resolve `goto-sheet` targets to real dashboards — never invent navigation targets.
