# Tableau Theme Extraction Skill

## Purpose
Extract visual theme (colors, fonts, backgrounds) from a Tableau workbook (.twb/.twbx) and produce a Power BI report theme JSON that replicates the same look and feel.

## When to Use
- Before generating Power BI report visuals from a Tableau migration
- When the Tableau workbook has a custom dark/light theme
- To ensure the migrated report matches the original Tableau visual identity

## Extraction Steps

### 1. Parse TWB XML — Extract Style Rules

Look for these elements in the TWB XML:

```xml
<!-- Background color -->
<style-rule element="table">
  <format attr="background-color" value="#000000" />
</style-rule>

<!-- Mark/chart color -->
<style-rule element="mark">
  <format attr="mark-color" value="#aa0000" />
</style-rule>

<!-- Color encoding (series colors) -->
<style-rule element="mark">
  <encoding attr="color" field="[field]" type="palette">
    <map to="#d3293d"><bucket>"Movie"</bucket></map>
    <map to="#ffbeb2"><bucket>"TV Show"</bucket></map>
  </encoding>
</style-rule>

<!-- Map style -->
<style-rule element="map">
  <format attr="map-style" value="dark" />
</style-rule>
```

### 2. Extract Font Colors

```xml
<format attr="color" value="#ffffff" />   <!-- body text -->
<format attr="color" value="#ff0000" />   <!-- titles (red in Netflix) -->
<format attr="color" value="#c0c0c0" />   <!-- secondary text -->
```

### 3. Extract Dashboard Size

```xml
<dashboard name="...">
  <size maxwidth="1700" maxheight="800" minwidth="1700" minheight="800" />
</dashboard>
```

### 4. Map to Power BI Theme JSON

The extracted colors map to Power BI's theme structure:

| Tableau Element | Power BI Theme Property |
|----------------|------------------------|
| `table.background-color` | `visualStyles.page.*.background.color` |
| `mark-color` | `dataColors[0]` |
| Color encoding maps | `dataColors` array |
| Title `color` | `visualStyles.*.title.fontColor` |
| Text `color` | `foreground` |
| Dashboard size | Page `width` and `height` in page.json |

### 5. Generate Power BI Theme JSON

Output a theme JSON file that can be embedded in `report.json` under `themeCollection.customTheme` or used as a standalone `.json` theme file:

```json
{
  "name": "Tableau Migrated Theme",
  "dataColors": ["#d3293d", "#ffbeb2", "#aa0000", "#ff6600", "#ffc000"],
  "background": "#000000",
  "foreground": "#ffffff",
  "tableAccent": "#d3293d",
  "visualStyles": {
    "*": {
      "*": {
        "background": [{"color": {"solid": {"color": "#000000"}}}],
        "title": [{"fontColor": {"solid": {"color": "#ff0000"}}, "fontSize": 12, "fontFamily": "Segoe UI"}],
        "labels": [{"color": {"solid": {"color": "#ffffff"}}}]
      }
    }
  }
}
```

### 6. Apply to Report Visuals

When generating visual.json files, apply the theme through `objects` properties:

- **Visual background**: Set `visualContainerObjects.background` to match page bg
- **Title color**: Set `objects.title.properties.fontColor` to the title color
- **Axis labels**: Set `objects.categoryAxis/valueAxis.properties.labelColor`
- **Border**: Add `visualContainerObjects.border` with `show: true` and color matching accent

## Color Extraction Priority

1. **Dashboard-level `style-rule element="table"` background** → Page background
2. **`mark-color`** → Primary data color
3. **Color encoding `<map to="...">` buckets** → Data colors array (in order)
4. **Title format `color`** → Title font color (look for non-white, non-black values)
5. **Font `color` in general formats** → Foreground/label color
6. **Filter/slicer `background-color`** → Slicer styling

## Rules

1. ALWAYS preserve the original Tableau color palette — do NOT substitute with Power BI defaults
2. If Tableau uses a dark background (#000000), Power BI page background MUST also be dark
3. Visual titles MUST use the same color as Tableau (typically red/accent on dark themes)
4. Data labels and axis labels MUST use the same color as Tableau body text (typically white on dark)
5. Match the dashboard size exactly (set page width/height to Tableau's maxwidth/maxheight)
6. Borders around visuals should match Tableau's zone borders (typically subtle or none on dark themes)
