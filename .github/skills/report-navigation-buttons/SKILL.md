# Report Navigation Buttons Skill

## Purpose

Generate Power BI `actionButton` visuals from Tableau navigation and toggle buttons. Single-responsibility companion to the report visual generation pipeline. Covers page-navigation buttons (`goto-sheet`) and bookmark toggle buttons.

## When to Use

- During report visual generation, for EVERY Tableau button extracted from a dashboard
- Whenever `visualType` is `actionButton`

## ⚠️ CRITICAL — Action Goes in `visualContainerObjects.visualLink`

Power BI wires button navigation through the **container-level `visualLink`** object — NOT `objects.action`.

- Set the action via `visualContainerObjects.visualLink` with `type` and `navigationSection` (the target page's `name`).
- Putting the action under `visual.objects.action` (or using a `page` key) makes the button **render but do nothing** — navigation silently fails.
- `type` values: `'PageNavigation'`, `'Bookmark'`, `'Back'`, `'Drillthrough'`, `'Url'`, `'WebUrl'`, `'QnA'`.

> This supersedes any older guidance that placed `action.type`/`action.page` inside `objects.action`. Always use `visualLink`.

> **Color:** replace `{button_fill_color}` with the color extracted from the parent Tableau button-bar
> zone style (fallback to the report theme's primary/background color). Do **not** hardcode a color.

## Page Navigation Button (Tableau `goto-sheet`)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "nav_button_{target_page}",
  "position": {"x": 25, "y": 25, "z": 1000, "height": 40, "width": 120, "tabOrder": 0},
  "visual": {
    "visualType": "actionButton",
    "objects": {
      "icon": [{"properties": {"shapeType": {"expr": {"Literal": {"Value": "'Arrow'"}}}}}],
      "outline": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
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

## Bookmark Toggle Button (Tableau `toggle-action`)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
  "name": "toggle_button_{function}",
  "position": {"x": 25, "y": 25, "z": 1000, "height": 40, "width": 40, "tabOrder": 0},
  "visual": {
    "visualType": "actionButton",
    "objects": {
      "icon": [{"properties": {"shapeType": {"expr": {"Literal": {"Value": "'Filter'"}}}}}],
      "outline": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
      "fill": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "true"}}},
        "fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'{button_fill_color}'"}}}}}
      }}],
      "text": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
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

- `visualLink.bookmark` MUST be the `name` (hex id) of a generated bookmark (see "Bookmark Generation"). NEVER leave it empty.

## Bookmark Generation (Tableau toggle → real Power BI bookmarks)

Reproduce a Tableau `toggle-action` (show/hide zones) by generating a **bookmark pair** that toggles the
visibility of the migrated visuals — no manual setup in Desktop required.

1. **Map** the toggled `zone-ids` to the `name`s of the generated visuals → `targetVisualNames`.
2. **Create** `definition/bookmarks/` with `bookmarks.json` plus one `.bookmark.json` per state.
3. Bookmark `name` values must be unique 20-char hex tokens matching `^[\w-]+$`.

`bookmarks.json`:

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json",
  "items": [{"name": "{hexid_show}"}, {"name": "{hexid_hide}"}]
}
```

> ### ⚠️ CRITICAL — `display.mode` enum: `"visible"` is INVALID
>
> In a `.bookmark.json`, `explorationState.sections.{page}.visualContainers.{visual}.singleVisual.display.mode`
> accepts ONLY these four values (per the official `bookmark/1.4.0` schema → `VisualContainerDisplayMode`):
> **`hidden`**, `maximize`, `spotlight`, `elevation`.
> There is **NO `"visible"` value.** Writing `"mode": "visible"` makes Power BI Desktop reject the whole `.pbip`
> with *"JSON does not match any schemas from 'anyOf' … singleVisual.display.mode"*.
> The `tmdl-validate` and `validate_pbip.py` validators do NOT catch this — only Desktop's deserializer does.
>
> **To make a visual VISIBLE in a bookmark: OMIT its entry from `visualContainers` entirely.** Keep it listed in
> `options.targetVisualNames` (with `applyOnlyToTargetVisuals: true`); any target without a `"hidden"` override is
> shown. So each bookmark's `visualContainers` should contain **only the visuals it HIDES**.

`{hexid_show}.bookmark.json` — the "Show" state reveals the drawer visuals, so they are OMITTED (default visible); only the visuals that must be hidden in this state (e.g. the "Show" button itself) get a `"hidden"` override:

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

The `{hexid_hide}.bookmark.json` "Hide" state is the mirror image — it lists the drawer visuals (`{visual_a}`, `{visual_b}`) and the `{hide_button}` with `"mode": "hidden"`, and OMITS the `{show_button}` so it reappears.

A Power BI bookmark button applies **one** bookmark, so a full toggle uses **two stacked buttons** at the
same position — "Show" wired to `{hexid_show}`, "Hide" wired to `{hexid_hide}` — each hidden by the opposite
bookmark so they swap as the user clicks. For a single button, wire it to the Show bookmark and note the
single-direction limitation in the spec.

## Button Rules

- **No data query**: buttons only have `objects` + `visualContainerObjects` — no `query` property.
- **Position**: scale from Tableau zone coordinates using the same formula as charts (see `report-layout-gapping`).
- **Z-index**: 1000+ so buttons render above chart visuals.
- **Title**: ALWAYS `title.show = false`.
- **Label text**: use the extracted Tableau tooltip (e.g. "Go to Sales Dashboard").
- **Styling**: match the Tableau nav-bar background color (extract from the parent zone style into `{button_fill_color}`) — do NOT hardcode a color.
- **Page target**: `visualLink.navigationSection` MUST match the target page's `name` from its `page.json`.
- **Bookmark toggle**: generate a Show/Hide bookmark pair under `definition/bookmarks/` and set `visualLink.bookmark` to the generated bookmark `name` — never leave it empty (see "Bookmark Generation").
- **Bookmark `display.mode`**: ONLY `hidden` (or `maximize`/`spotlight`/`elevation`) is valid — NEVER `"visible"`. To show a visual, omit it from `visualContainers`. Listing every visual with `"visible"` breaks the `.pbip` load.
- **Icon shapes**: `'Arrow'` for navigation, `'Filter'` for filter toggles, `'Blank'` for custom.
- **Active state**: distinct fill/border for the "current page" button.

## Anti-Hallucination

- Generate a button ONLY for Tableau zones that contain a `<button>` element.
- Resolve `goto-sheet` `window-id` to a real target dashboard; do not invent navigation targets.
