# SVG Patterns for Card and Slicer Visuals

Card (`cardVisual`) and Slicer (`advancedSlicerVisual`) visuals support SVG measures through specific binding patterns. The classic card does NOT support SVG -- only the new card visual works.

## Card Visual (cardVisual)

### Binding

Card visuals render SVG via `callout.imageFX`. Bind the SVG measure to the card's `calloutValue` field, then configure `imageFX`:

```json
{
  "objects": {
    "callout": [{
      "properties": {
        "imageFX": {"expr": {"Literal": {"Value": "true"}}},
        "imageHeight": {"expr": {"Literal": {"Value": "40D"}}},
        "imageWidth": {"expr": {"Literal": {"Value": "100D"}}}
      }
    }]
  }
}
```

### Pattern: Arrow Indicator

Compact directional indicator for KPI cards.

```dax
Arrow Indicator =
VAR _Growth = [Growth %]
VAR _Up = _Growth >= 0
VAR _Path = IF(_Up, "M 10,15 L 5,10 L 15,10 Z", "M 10,5 L 5,10 L 15,10 Z")
VAR _Color = IF(_Up, "#4CAF50", "#F44336")

RETURN
"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'>" &
"<path d='" & _Path & "' fill='" & _Color & "'/></svg>"
```

### Pattern: Mini Gauge

Semi-circular gauge for progress or performance.

```dax
Mini Gauge =
VAR _Pct = DIVIDE([Value], [Target], 0)
VAR _Angle = (_Pct * 180) - 90
VAR _R = 40
VAR _CX = 50
VAR _CY = 50
VAR _Rad = _Angle * PI() / 180
VAR _NX = _CX + (_R * 0.8 * COS(_Rad))
VAR _NY = _CY + (_R * 0.8 * SIN(_Rad))

RETURN
"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 60'>" &
"<path d='M 10 50 A 40 40 0 0 1 90 50' fill='none' stroke='#E0E0E0' stroke-width='8'/>" &
"<line x1='" & _CX & "' y1='" & _CY & "' x2='" & _NX & "' y2='" & _NY & "' stroke='#333' stroke-width='2'/>" &
"<circle cx='" & _CX & "' cy='" & _CY & "' r='3' fill='#333'/></svg>"
```

### Pattern: Mini Donut

Percentage completion as a donut ring.

```dax
Mini Donut =
VAR _Pct = [Percentage]
VAR _Angle = _Pct * 360
VAR _LargeArc = IF(_Angle > 180, 1, 0)
VAR _R = 40
VAR _CX = 50
VAR _CY = 50
VAR _Rad = (_Angle - 90) * PI() / 180
VAR _EndX = _CX + (_R * COS(_Rad))
VAR _EndY = _CY + (_R * SIN(_Rad))

RETURN
"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>" &
"<circle cx='" & _CX & "' cy='" & _CY & "' r='" & _R & "' fill='none' stroke='#E0E0E0' stroke-width='8'/>" &
"<path d='M " & _CX & " " & (_CY - _R) & " A " & _R & " " & _R & " 0 " & _LargeArc & " 1 " & _EndX & " " & _EndY & "' fill='none' stroke='#2196F3' stroke-width='8'/></svg>"
```

### Pattern: Progress Bar

Horizontal bar with label, sized for card visuals.

```dax
Progress Bar =
VAR _Pct = [Completion %]
VAR _W = 100
VAR _H = 20
VAR _FillW = _Pct * _W
VAR _Label = FORMAT(_Pct, "0%")
VAR _Color = SWITCH(TRUE(), _Pct < 0.5, "#F44336", _Pct < 0.8, "#FF9800", "#4CAF50")

RETURN
"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 " & _W & " " & _H & "'>" &
"<rect width='" & _W & "' height='" & _H & "' fill='#E0E0E0' rx='" & (_H / 2) & "'/>" &
"<rect width='" & _FillW & "' height='" & _H & "' fill='" & _Color & "' rx='" & (_H / 2) & "'/>" &
"<text x='" & (_W / 2) & "' y='" & (_H / 2 + 5) & "' font-size='11' text-anchor='middle' fill='white' font-weight='bold'>" & _Label & "</text></svg>"
```

---

## Slicer Visual (advancedSlicerVisual)

Slicers can display SVG in header images and custom slicer items. This is less common but useful for branded slicer headers.

### Binding

Set SVG in slicer header via `header.image`:

```json
{
  "objects": {
    "header": [{
      "properties": {
        "image": {
          "expr": {
            "Measure": {
              "Expression": {"SourceRef": {"Schema": "extension", "Entity": "Table"}},
              "Property": "Header SVG"
            }
          }
        }
      }
    }]
  }
}
```

---

## Design Considerations

### Card SVGs Should Be Small

Card visuals have limited space. Keep SVGs compact:
- Arrow indicators: `viewBox='0 0 20 20'`
- Mini gauges: `viewBox='0 0 100 60'`
- Progress bars: `viewBox='0 0 100 20'`

### Classic Card Does NOT Work

The classic card visual (`card`) does NOT support SVG measures. Only `cardVisual` (the "new card") works. If SVG renders as text or blank, verify the visual type is `cardVisual`.

### Color Rules

Same as all SVG visuals: always use hex codes with `#` (e.g., `fill='#2196F3'`). Never use `%23` URL encoding.

### Conditional Colors for KPI States

Common color scheme for KPI indicators:

```dax
VAR _Color = SWITCH(TRUE(),
    _Performance >= 1.0,  "#4CAF50",   -- Green (exceeds target)
    _Performance >= 0.8,  "#FF9800",   -- Amber (near target)
    "#F44336"                           -- Red (below target)
)
```

Or using the SpaceParts sentiment pattern with 4 levels:

```dax
VAR _Color = SWITCH(TRUE(),
    _Performance < -0.05, "#f4ae4c",   -- Dark yellow (very bad)
    _Performance < -0.025, "#ffe075",  -- Light yellow (bad)
    _Performance > 0.05,  "#2D6390",   -- Dark blue (very good)
    _Performance > 0.025, "#74B2FF",   -- Light blue (good)
    "#CCCCCC"                           -- Grey (neutral)
)
```
