# Visual Colors

Guidelines for effective color usage in Power BI reports.

## Color Principles

### Use Theme Colors

Prefer theme colors over hex codes:

```json
// Good - uses theme color
"expr": {"ThemeDataColor": {"ColorId": 1, "Percent": 0}}

// Avoid in visuals - use only in extension measures
"expr": {"Literal": {"Value": "'#118DFF'"}}
```

### Semantic Colors

Use these theme color names in extension measures:

| Color Name | Meaning | Typical Color |
|------------|---------|---------------|
| `"good"` | Positive, on-target | Green |
| `"bad"` | Negative, off-target | Red |
| `"neutral"` | Unchanged, baseline | Gray/Yellow |
| `"minColor"` | Gradient minimum | Red/Orange |
| `"midColor"` | Gradient midpoint | Yellow/White |
| `"maxColor"` | Gradient maximum | Green/Blue |

### Extension Measure Pattern

```dax
// Return theme color names, not hex codes
Color Measure =
IF([Value] >= [Target], "good",
IF([Value] >= [Target] * 0.9, "neutral", "bad"))
```

## Color Contrast

### WCAG 2.1 Requirements

| Element | Minimum Ratio |
|---------|---------------|
| Normal text | 4.5:1 |
| Large text (18pt+) | 3:1 |
| UI components | 3:1 |

### Common Contrast Issues

| Background | Text | Ratio | Status |
|------------|------|-------|--------|
| White (#FFF) | Dark gray (#333) | 12.6:1 | Pass |
| White (#FFF) | Medium gray (#777) | 4.5:1 | Pass (barely) |
| White (#FFF) | Light gray (#AAA) | 2.9:1 | Fail |
| Light blue (#E3F2FD) | Blue (#1976D2) | 4.8:1 | Pass |

## Color Categories

### Data Colors (dataColors)

Primary series colors in theme:

```json
"dataColors": [
  "#118DFF",  // Blue (primary)
  "#12239E",  // Dark blue
  "#E66C37",  // Orange
  "#6B007B",  // Purple
  "#E044A7",  // Pink
  "#744EC2"   // Violet
]
```

### Background Colors

Use muted, light colors:

- White: `#FFFFFF`
- Light gray: `#F5F5F5`, `#FAFAFA`
- Light blue: `#F0F8FF`, `#E3F2FD`

### Accent Colors

For highlights and emphasis:

- Use sparingly
- Reserve bright colors for important data
- Don't use red/orange unless indicating problems

## Conditional Formatting Colors

### Best Practices

1. **Theme tokens over hex** -- CF should use theme sentiment tokens ("good", "bad", "neutral", "minColor", "maxColor") not hardcoded hex. Theme tokens mean changing the theme cascades to all CF across all reports. Use `--theme-colors` to convert existing hex CF to tokens.
2. **Measure-driven preferred** -- Prefer extension measures returning theme tokens over built-in gradient/rules. Measure logic lives in one place; change the measure or theme and it propagates. Use `--to-measure` to convert built-in CF.
3. **Sparingly applied** -- CF should highlight exceptions, not decorate everything. Formatting everything means formatting nothing. Apply to variance/gap columns, not raw values.
4. **Accessible** -- Use blue/orange instead of red/green for colorblind safety. Always pair color with a secondary cue (icon, text, shape).
5. **Theme-first hierarchy** -- Check theme sentiment colors exist before applying CF. Create them if missing by setting `good`, `bad`, and `neutral` sentiment colors in the theme.json file (e.g., good="#00B050", bad="#FF0000", neutral="#FFC000")

### Positive/Negative Pattern

```json
// In extension measure (preferred)
"expression": "IF([Value] >= 0, \"good\", \"bad\")"
```

Theme defines actual colors:

```json
"good": "#00B050",   // Green
"bad": "#FF0000",    // Red
"neutral": "#FFC000" // Yellow/Orange
```

### Gradient Pattern

For continuous scales, use theme tokens not hex:

```
minColor -> bad end of scale (e.g., "minColor" or "bad")
midColor -> neutral midpoint (e.g., "midColor" or "neutral")
maxColor -> good end of scale (e.g., "maxColor" or "good")
```

### Traffic Light Pattern

| Range | Color Name | Meaning |
|-------|------------|---------|
| < 50% | `"bad"` | Critical |
| 50-80% | `"neutral"` | Warning |
| > 80% | `"good"` | On track |

### Data Bars

Data bars provide magnitude scanning in tables/matrices. Apply to primary measure columns. Use muted colors that don't overwhelm the text values.

## Color Don'ts

### Avoid

1. **Too many colors** - Maximum 6-8 distinct colors per visual
2. **Pure black** - Use dark gray (#333) instead
3. **Neon/bright colors** - Cause eye strain
4. **Red for positive** - Confuses users
5. **Color-only meaning** - Always pair with text/icons

### Never Use

- Rainbow gradients
- Clashing color combinations
- Low contrast combinations
- Brand colors on data points (unless intentional)

## Accessibility Tips

### Color Blindness

Test with color blindness simulators:

- Protanopia (red-blind): ~1% of males
- Deuteranopia (green-blind): ~1% of males
- Tritanopia (blue-blind): rare

**Safe combinations:**

- Blue + Orange (instead of Red + Green)
- Blue + Yellow
- Dark + Light variants of same hue

### Alternative Indicators

Pair colors with:

- Icons (up/down arrows)
- Patterns (solid/hatched)
- Text labels
- Shapes (markers)
