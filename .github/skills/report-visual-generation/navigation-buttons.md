# Navigation Buttons & Bookmarks Skill

## Purpose

Migrate Tableau navigation buttons (goto-sheet, toggle/show-hide) to Power BI `actionButton` visuals with page navigation and bookmark actions.

## Tableau Button Types

### 1. Page Navigation (`goto-sheet`)

**Tableau XML**: `<button action='tabdoc:goto-sheet window-id="..."'>`

**Detection**: Zone with `type-v2='dashboard-object'` containing `<button>` with `action` attribute matching `goto-sheet`.

**Power BI equivalent**: `actionButton` with `action.type = "PageNavigation"`

### 2. Toggle Visibility (`toggle-action`)

**Tableau XML**: `<button action=''><toggle-action>tabdoc:toggle-button-click-action ...</toggle-action></button>`

**Detection**: Zone with `<button>` containing a `<toggle-action>` child element. The `action` attribute is empty.

**Power BI equivalent**: `actionButton` with `visualContainerObjects.visualLink.type = "Bookmark"`, wired to an **auto-generated bookmark** that hides/shows the target visuals (see "Bookmark Generation" below — no manual setup required).

---

## Extraction (from TWB XML)

### PowerShell Extraction Script

```powershell
[xml]$twb = Get-Content "Data/{subfolder}/{workbook}.twb" -Raw

$twb.workbook.dashboards.dashboard | ForEach-Object {
    $dname = $_.name
    Write-Host "=== Buttons for: $dname ==="
    
    function Find-Buttons($zones) {
        foreach ($z in $zones) {
            if ($z.button) {
                $action = $z.button.action
                $tooltip = $z.button.'button-visual-state'.'tooltip-text'
                if ($tooltip -is [array]) { $tooltip = $tooltip[0] }
                $toggleAction = $z.button.'toggle-action'
                
                $actionType = if ($toggleAction) { "toggle" }
                              elseif ($action -match 'goto-sheet') { "goto-sheet" }
                              else { "unknown" }
                
                # For goto-sheet, extract window-id to resolve target dashboard
                $target = ""
                if ($action -match 'window-id="([^"]+)"') {
                    $windowId = $Matches[1]
                    $target = ($twb.workbook.windows.window | Where-Object { $_.id -eq $windowId }).name
                }
                
                Write-Host "  type=$actionType tooltip=$tooltip target=$target x=$($z.x) y=$($z.y) w=$($z.w) h=$($z.h)"
            }
            if ($z.zone) { Find-Buttons $z.zone }
        }
    }
    Find-Buttons $_.zones.zone
}
```

### Output Format (save to tableau-visuals-output.md)

```markdown
### Navigation Buttons: {dashboard_name}
| # | Action Type | Tooltip | Target | x | y | w | h |
|---|-------------|---------|--------|---|---|---|---|
| 1 | goto-sheet | Go to Sales Dashboard | SalesDashboard | 120 | 30 | 150 | 40 |
| 2 | goto-sheet | Go to Customer Dashboard | CustomerDashboard | 280 | 30 | 150 | 40 |
| 3 | toggle | Show Dashboard Filters | zones: [6, 7] | 450 | 30 | 40 | 40 |
```

---

## Generation Templates

> ⚠️ **CRITICAL — the action lives in `visualContainerObjects.visualLink`, NOT `objects.action`.**
> Power BI wires button navigation/bookmark actions through the container-level `visualLink` object.
> Putting the action under `visual.objects.action` makes the button **render but do nothing** — the
> action silently fails. The templates below use `visualLink`. Use `type` = `'PageNavigation'` |
> `'Bookmark'` | `'Back'` | `'Drillthrough'` | `'Url'` | `'WebUrl'` | `'QnA'`.
>
> Replace `{button_fill_color}` with the color extracted from the parent Tableau button-bar zone
> style (fallback to the report theme's primary/background color). Do **not** hardcode a color.

### Page Navigation Button

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "nav_button_{target_page}",
  "position": {
    "x": 25, "y": 25, "z": 1000,
    "height": 40, "width": 120, "tabOrder": 0
  },
  "visual": {
    "visualType": "actionButton",
    "objects": {
      "icon": [{"properties": {
        "shapeType": {"expr": {"Literal": {"Value": "'Arrow'"}}}
      }}],
      "outline": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "false"}}}
      }}],
      "fill": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'{button_fill_color}'"}}}}}
      }}],
      "text": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "text": {"expr": {"Literal": {"Value": "'{button_label}'"}}},
        "fontColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}}}
      }}]
    },
    "visualContainerObjects": {
      "visualLink": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
        "navigationSection": {"expr": {"Literal": {"Value": "'{target_page_name}'"}}}
      }}],
      "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
      "background": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'{button_fill_color}'"}}}}}
      }}],
      "border": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
    }
  }
}
```

### Bookmark Toggle Button

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "toggle_{function}",
  "position": {
    "x": 25, "y": 25, "z": 1000,
    "height": 40, "width": 40, "tabOrder": 0
  },
  "visual": {
    "visualType": "actionButton",
    "objects": {
      "icon": [{"properties": {
        "shapeType": {"expr": {"Literal": {"Value": "'Filter'"}}}
      }}],
      "outline": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "false"}}}
      }}],
      "fill": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'{button_fill_color}'"}}}}}
      }}],
      "text": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "false"}}}
      }}]
    },
    "visualContainerObjects": {
      "visualLink": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "type": {"expr": {"Literal": {"Value": "'Bookmark'"}}},
        "bookmark": {"expr": {"Literal": {"Value": "'{bookmark_id}'"}}}
      }}],
      "title": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
      "background": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'{button_fill_color}'"}}}}}
      }}],
      "border": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
    }
  }
}
```

- `visualLink.bookmark` MUST be the `name` (hex id) of a generated bookmark — see "Bookmark Generation" below. NEVER leave it empty.

---

## Bookmark Generation (replaces manual setup)

A Tableau `toggle-action` shows/hides a set of zones. Reproduce this in Power BI by **auto-generating a bookmark pair** that toggles the visibility (`display.mode`) of the migrated visuals that correspond to the toggled `zone-ids`. No manual creation in Desktop is required.

### Step 1 — Map toggled zones to PBI visual names

From the `<toggle-action>` `zone-ids`, resolve each Tableau zone to the `name` of the generated `visual.json` for that worksheet. Collect them into `targetVisualNames`.

### Step 2 — Create the bookmarks folder

```
Output/{WorkbookName}/{ProjectName}.Report/definition/bookmarks/
├── bookmarks.json
├── {hexid_show}.bookmark.json
└── {hexid_hide}.bookmark.json
```

- Bookmark `name` values MUST be unique tokens matching `^[\w-]+$` (use a 20-char hex id).

### Step 3 — bookmarks.json (order)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json",
  "items": [
    {"name": "{hexid_show}"},
    {"name": "{hexid_hide}"}
  ]
}
```

### Step 4 — "Show" bookmark ({hexid_show}.bookmark.json)

> **⚠️ CRITICAL — `display.mode` has NO `"visible"` value.** The `bookmark/1.4.0` schema
> (`VisualContainerDisplayMode`) allows ONLY `hidden`, `maximize`, `spotlight`, `elevation`.
> `"mode": "visible"` makes Power BI Desktop reject the `.pbip` with *"JSON does not match any schemas
> from 'anyOf' … singleVisual.display.mode"* (neither `tmdl-validate` nor `validate_pbip.py` catches it).
> **To make a visual visible, OMIT it from `visualContainers`.** Each bookmark lists ONLY the visuals it HIDES.

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.4.0/schema.json",
  "displayName": "Show {function}",
  "name": "{hexid_show}",
  "options": {
    "applyOnlyToTargetVisuals": true,
    "targetVisualNames": ["{visual_a}", "{visual_b}", "{show_button}", "{hide_button}"],
    "suppressData": true,
    "suppressActiveSection": true
  },
  "explorationState": {
    "version": "1.3",
    "activeSection": "{page_name}",
    "sections": {
      "{page_name}": {
        "visualContainers": {
          "{show_button}": {"singleVisual": {"display": {"mode": "hidden"}}}
        }
      }
    }
  }
}
```

### Step 5 — "Hide" bookmark ({hexid_hide}.bookmark.json)

The mirror image of the Show bookmark: same `targetVisualNames`, its own `name` and
`"displayName": "Hide {function}"`, and `visualContainers` listing the drawer visuals
(`{visual_a}`, `{visual_b}`) plus `{hide_button}` each with `"display": {"mode": "hidden"}`.
The `{show_button}` is OMITTED so it reappears.

### Step 6 — Wire the button(s)

A single Tableau toggle alternates two states, but a Power BI bookmark button applies **one** bookmark.
Reproduce the toggle with **two stacked buttons** occupying the same position — each wired to one bookmark
and itself hidden by the opposite bookmark:

- "Show" button → `visualLink.bookmark = {hexid_show}`; hidden by the Show bookmark, shown by the Hide bookmark.
- "Hide" button → `visualLink.bookmark = {hexid_hide}`; hidden by the Hide bookmark, shown by the Show bookmark.

Add each button's `name` to the bookmarks' `targetVisualNames`. A button is HIDDEN in a bookmark by listing
it with `"display": {"mode": "hidden"}`, and SHOWN by omitting it — so the two buttons swap as the user
toggles. If only one button is desired, wire it to the Show bookmark and record the limitation
(single-direction) in the spec.

---

## Rules

### Navigation Buttons
- Set the action via `visualContainerObjects.visualLink` (`type` + `navigationSection`) — NEVER `objects.action`
- `visualLink.navigationSection` MUST match the `name` field from the target page's `page.json`
- Button label (`text.text`) = Tableau tooltip text (e.g., "Go to Sales Dashboard")
- Use z-index 1000+ so buttons render above chart visuals
- Set `title.show = false` — buttons don't need container titles
- Style background/fill from the Tableau button-bar zone style (`{button_fill_color}`) — do NOT hardcode a color
- Group navigation buttons at the same y-position with consistent spacing
- Icon shape: `'Arrow'` for navigation

### Toggle/Bookmark Buttons
- Set the action via `visualContainerObjects.visualLink` with `type` = `'Bookmark'` — NEVER `objects.action`
- `visualLink.bookmark` MUST reference a generated bookmark `name` (hex id) — NEVER leave it empty
- Auto-generate the Show/Hide bookmark pair under `definition/bookmarks/` (see "Bookmark Generation")
- Map the Tableau `toggle-action` `zone-ids` to the corresponding PBI visual `name`s for `targetVisualNames`
- Icon shape: `'Filter'` for filter toggles, `'Blank'` for custom
- Tooltip text from Tableau drives the bookmark `displayName` (e.g., "Show Filters" / "Hide Filters")

### Active State Indicator
- For "current page" button: use a distinct fill color or visible border to indicate active state
- Can use conditional formatting with `isCurrentPage` if supported

### Available Icon Shapes
- `'Arrow'` — navigation
- `'Filter'` — filter toggle
- `'Blank'` — no icon (text only)
- `'Back'` — back navigation
- `'Bookmark'` — bookmark action
- `'Reset'` — reset filters
- `'QnA'` — Q&A

---

## Validation Checklist

- [ ] Every Tableau `goto-sheet` button has a corresponding `actionButton` with `visualLink.type = "PageNavigation"`
- [ ] Every Tableau `toggle` button has a corresponding `actionButton` with `visualLink.type = "Bookmark"`
- [ ] NO button uses the deprecated `objects.action` pattern — all actions live in `visualContainerObjects.visualLink`
- [ ] `visualLink.navigationSection` values match actual page `name` values in page.json files
- [ ] `visualLink.bookmark` references a real bookmark `name` in `definition/bookmarks/` (never empty)
- [ ] A Show/Hide bookmark pair exists for every toggle button, with `targetVisualNames` resolved to real visuals
- [ ] `definition/bookmarks/bookmarks.json` lists every generated bookmark `name`
- [ ] Bookmark file/`name` values satisfy `^[\w-]+$`
- [ ] Button fill/background uses `{button_fill_color}` extracted from the source — no hardcoded color
- [ ] Button positions match extracted zone coordinates (scaled to PBI canvas)
- [ ] All navigation buttons have `title.show = false`
- [ ] Button count matches: if Tableau has N buttons per dashboard, PBI page has N button visuals
