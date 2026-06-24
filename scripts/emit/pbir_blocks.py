"""pbir_blocks.py — PBIR JSON visual builders (enhanced folder format).

Builds visual.json / page.json dictionaries that conform to the PBIR
visualContainer schema. Per the format rules, a visual.json root may ONLY carry
$schema / name / position / visual — no filters or extra properties. Color and
boolean values use the Literal expression wrapper Power BI Desktop requires.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

VC_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json"
PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json"
PAGES_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json"


def literal(value) -> Dict:
    """Wrap a scalar in the PBIR Literal expression form."""
    if isinstance(value, bool):
        return {"expr": {"Literal": {"Value": "true" if value else "false"}}}
    if isinstance(value, str):
        return {"expr": {"Literal": {"Value": f"'{value}'"}}}
    return {"expr": {"Literal": {"Value": str(value)}}}


def color(hex_value: str) -> Dict:
    """Build a solid-color property value."""
    return {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_value}'"}}}}}


def opaque_color(hex_value: Optional[str], fallback: str = "#004263") -> str:
    """Return a guaranteed-visible opaque ``#RRGGBB`` hex for a button fill.

    A Power BI button fill/background MUST never be transparent or the button
    renders as an invisible (all-white) tile. Tableau worksheet formatting often
    yields a fully transparent mark colour (``#00000000``), and the auto-derived
    theme adopted it as the button fill -- which made every show/hide and
    navigation button disappear. This normalises any input to an opaque six-digit
    hex, substituting ``fallback`` whenever the colour is missing, malformed, or
    fully transparent (8-digit ``#AARRGGBB`` with alpha ``00``).
    """
    if not isinstance(hex_value, str):
        return fallback
    h = hex_value.strip().lstrip("#")
    if len(h) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in h):
        return fallback
    if len(h) == 8:
        if h[:2].lower() == "00":      # fully transparent -> unusable as a fill
            return fallback
        h = h[2:]                      # drop the alpha byte, keep the RGB
    return "#" + h.lower()


def _num(value) -> Dict:
    """Wrap a number as a PBIR double literal (e.g. 14 -> '14D')."""
    return {"expr": {"Literal": {"Value": f"{value}D"}}}


# Power BI's labelDisplayUnits enum is the literal DIVISOR applied to the value:
# 1 = None (divide by 1, no scaling), 1000 = Thousands, 1_000_000 = Millions,
# 1_000_000_000 = Billions, and 0 = Auto (let Power BI pick). Our measures already
# bake their own K/M/B scaling + suffix into the formatString, so the renderer
# MUST request None (1) -- requesting 0 (Auto) makes Power BI re-scale on top,
# doubling units to "bnM"/"KK" and rounding the value to 0.
DISPLAY_UNITS_NONE = 1


def _display_units_none() -> Dict:
    """Display-units literal that disables Power BI auto-scaling (None = 1)."""
    return _num(DISPLAY_UNITS_NONE)


def _display_units(value: Optional[Dict]) -> Dict:
    """Display-units literal for a value binding. A measure whose Tableau format
    scaled by thousands/millions carries ``displayUnits`` (1000 / 1_000_000 / …);
    Power BI then divides the value and appends the matching K/M/bn suffix. Absent
    or 0 -> None (1), i.e. no auto-scaling on top of the model formatString."""
    du = value.get("displayUnits") if isinstance(value, dict) else None
    return _num(du or DISPLAY_UNITS_NONE)



def container_objects(title: Optional[str], theme: Optional[Dict],
                      title_size: int = 14) -> Dict:
    """Build visualContainerObjects (title + themed background/border).

    When a theme carries titleColor/visualBackground/border the visual inherits
    the dark-card styling (red centered title, dark fill, subtle border) that
    matches the source dashboard. With no theme only title show/text is emitted.
    """
    t = theme or {}
    title_props: Dict = {"show": literal(bool(title))}
    if title:
        title_props["text"] = literal(title)
        if t.get("titleColor"):
            title_props["fontColor"] = color(t["titleColor"])
            title_props["fontSize"] = _num(t.get("titleFontSize", title_size))
            title_props["alignment"] = literal(t.get("titleAlignment", "center"))
            title_props["fontFamily"] = literal(t.get("titleFont", "Segoe UI Semibold"))
    obj: Dict = {"title": [{"properties": title_props}]}
    if t.get("visualBackground"):
        obj["background"] = [{"properties": {
            "show": literal(True), "color": color(t["visualBackground"])}}]
    if t.get("border"):
        obj["border"] = [{"properties": {
            "show": literal(True), "color": color(t["border"]), "radius": _num(5)}}]
    return obj


def _series_datapoint(series: Dict, label: str, hex_value: str) -> Dict:
    """A per-category dataPoint fill keyed to one series value (Movie/TV Show)."""
    return {
        "properties": {"fill": color(hex_value)},
        "selector": {"data": [{"scopeId": {"Comparison": {
            "ComparisonKind": 0,
            "Left": {"Column": {
                "Expression": {"SourceRef": {"Entity": series["entity"]}},
                "Property": series["prop"]}},
            "Right": {"Literal": {"Value": f"'{label}'"}}}}}]},
    }


def measure_sort(entity: str, measure: str, direction: str = "Descending") -> Dict:
    """A default sortDefinition ordering a chart by a measure."""
    return {"sort": [{"field": {"Measure": {
        "Expression": {"SourceRef": {"Entity": entity}}, "Property": measure}},
        "direction": direction}], "isDefaultSort": True}


def column_sort(entity: str, col: str, direction: str = "Ascending") -> Dict:
    """A default sortDefinition ordering a chart by a category column."""
    return {"sort": [{"field": {"Column": {
        "Expression": {"SourceRef": {"Entity": entity}}, "Property": col}},
        "direction": direction}], "isDefaultSort": True}


def position(x: int, y: int, w: int, h: int, z: int) -> Dict:
    """Standard visual position block."""
    return {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": z}


def measure_filter_config(entity: str, measure: str, value: int = 1) -> Dict:
    """visual.json filterConfig that shows the visual only when measure == value.

    Reproduces a Tableau show/hide toggle: each overlapping visual is filtered by
    a flag measure that returns 1 only for its parameter combination, so exactly
    one of the stacked visuals renders for the current slicer selection.
    """
    fid = "Filter" + hashlib.sha1(f"{entity}|{measure}".encode("utf-8")).hexdigest()[:20]
    return {"filters": [{
        "name": fid,
        "field": {"Measure": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": measure}},
        "type": "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": "m", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Measure": {
                    "Expression": {"SourceRef": {"Source": "m"}}, "Property": measure}},
                "Right": {"Literal": {"Value": f"{value}L"}},
            }}}],
        },
        "howCreated": "User",
        "objects": {},
        "isHiddenInViewMode": True,
        "isLockedInViewMode": True,
    }]}


def topn_filter_config(entity: str, category_prop: str, n: int,
                       direction: str = "TOP",
                       order_measure: Optional[str] = None,
                       order_agg: Optional[int] = None,
                       order_col: Optional[str] = None,
                       order_entity: Optional[str] = None) -> Optional[Dict]:
    """visual.json filterConfig that keeps only the Top-N categories — the Power BI
    equivalent of a Tableau Top-N filter, expressed as a PBIR ``VisualTopN`` filter.

    Reproduces 'Top 10 States by Total Loan Volume': the categorical axis field is
    limited to its N highest values. PBIR's ``VisualTopN`` condition ranks by the
    visual's own plotted measure (the value already bound to the axis), so it takes
    only ``ItemCount`` — the schema does NOT accept an inline ranking expression,
    ``Top``, or ``OrderBy`` (those are rejected by Power BI Desktop on open). The
    ``order_*`` arguments are kept for caller compatibility but are not emitted:
    the rank measure is the visual's value encoding. Bottom-N is not expressible
    as a VisualTopN, so non-TOP directions return None (no filter emitted).
    """
    if str(direction).upper() != "TOP":
        return None
    fid = "Filter" + hashlib.sha1(
        f"topn|{entity}|{category_prop}".encode("utf-8")).hexdigest()[:20]
    return {"filters": [{
        "name": fid,
        "field": {"Column": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": category_prop}},
        "type": "VisualTopN",
        "filter": {
            "Version": 2,
            "From": [],
            "Where": [{"Condition": {"VisualTopN": {"ItemCount": int(n)}}}],
        },
        "howCreated": "User",
        "objects": {},
    }]}


def projection(entity: str, prop: str, active: bool = True) -> Dict:
    """A single column projection for a visual query."""
    return {
        "field": {"Column": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop, "active": active,
    }


def measure_projection(entity: str, prop: str, active: bool = True) -> Dict:
    """A single measure projection for a visual query."""
    return {
        "field": {"Measure": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop, "active": active,
    }


def aggregation_projection(entity: str, prop: str, func: int,
                           label: str = "Count", active: bool = True) -> Dict:
    """An inline-aggregation projection (Power BI QueryAggregateFunction ``func``).

    Used when a chart plots an aggregation of a column that has no named model
    measure (e.g. ``COUNTD([show_id])`` on a fact table with no measures). Wrapping
    the column in an Aggregation makes Power BI compute the aggregate instead of
    trying to plot the raw column, so the visual renders a real number.
    """
    return {
        "field": {"Aggregation": {
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
            "Function": func}},
        "queryRef": f"{label}({entity}.{prop})", "nativeQueryRef": f"{label} of {prop}",
        "active": active,
    }


def binding_projection(b: Dict) -> Dict:
    """Build a projection from a {entity, prop, isMeasure?, agg?} binding dict."""
    if b.get("agg") is not None:
        return aggregation_projection(b["entity"], b["prop"], b["agg"],
                                      b.get("aggLabel") or "Count")
    if b.get("isMeasure"):
        return measure_projection(b["entity"], b["prop"])
    return projection(b["entity"], b["prop"])


def slicer_visual(name: str, pos: Dict, entity: str, prop: str,
                  title: str, header_color: str = "#004263",
                  mode: str = "Dropdown", single: bool = True,
                  default_value: Optional[str] = None,
                  theme: Optional[Dict] = None) -> Dict:
    """Build a slicer visual.json dict (Dropdown list or Between range).

    mode='Between' produces a numeric/date range slicer so two such slicers on
    the same column act as independent lower/upper bounds (Start / End dates).
    default_value pre-selects one item (used for field-parameter toggles so the
    bound visual opens on one field instead of showing every field at once).
    """
    t = theme or {}
    hdr_color = t.get("titleColor", header_color)
    data_props = {"mode": literal(mode)}
    hdr_props = {
        "show": literal(True), "text": literal(title),
        "fontColor": color(hdr_color),
    }
    # Match the Tableau filter card: bold red header in the worksheet title font.
    if t.get("titleFont"):
        hdr_props["fontFamily"] = literal(t["titleFont"])
    if t.get("titleFontSize"):
        hdr_props["textSize"] = _num(t["titleFontSize"])
    objects = {
        "data": [{"properties": data_props}],
        "header": [{"properties": hdr_props}],
    }
    if t.get("foreground"):
        objects["items"] = [{"properties": {"fontColor": color(t["foreground"])}}]
    if mode != "Between":
        objects["selection"] = [{"properties": {"singleSelect": literal(single)}}]
    if default_value is not None and mode != "Between":
        objects["general"] = [{"properties": {
            "filter": _slicer_default_filter(entity, prop, default_value)}}]
    vco = {"title": [{"properties": {"show": literal(False)}}]}
    if t.get("visualBackground"):
        vco["background"] = [{"properties": {
            "show": literal(True), "color": color(t["visualBackground"])}}]
    if t.get("border"):
        vco["border"] = [{"properties": {
            "show": literal(True), "color": color(t["border"]), "radius": _num(5)}}]
    return {
        "$schema": VC_SCHEMA, "name": name, "position": pos,
        "visual": {
            "visualType": "slicer",
            "query": {"queryState": {"Values": {"projections": [projection(entity, prop)]}}},
            "objects": objects,
            "visualContainerObjects": vco,
        },
    }


def _slicer_default_filter(entity: str, prop: str, value: str) -> Dict:
    """A single-value In condition used as a slicer's pre-selected default."""
    return {"filter": {
        "Version": 2,
        "From": [{"Name": "s", "Entity": entity, "Type": 0}],
        "Where": [{"Condition": {"In": {
            "Expressions": [{"Column": {
                "Expression": {"SourceRef": {"Source": "s"}}, "Property": prop}}],
            "Values": [[{"Literal": {"Value": f"'{value}'"}}]],
        }}}],
    }}


def chart_visual(name: str, pos: Dict, visual_type: str, category: Dict,
                 value: Dict, title: Optional[str] = None,
                 theme: Optional[Dict] = None, series: Optional[Dict] = None,
                 series_colors: Optional[Dict] = None,
                 single_color: Optional[str] = None,
                 sort: Optional[Dict] = None, data_labels: bool = True,
                 secondary_value: Optional[Dict] = None,
                 additional_values: Optional[List[Dict]] = None,
                 hide_value_axis: bool = False,
                 hide_labels: bool = False,
                 extra_categories: Optional[List[Dict]] = None,
                 tooltips: Optional[List[Dict]] = None) -> Dict:
    """Build a cartesian chart (bar/column/line/area) visual.json dict.

    secondary_value / additional_values add more Y projections (e.g. PY lines on
    KPI sparklines, Profit lines on trend charts).
    extra_categories add further fields to the category axis, reproducing a Tableau
    nested row/column hierarchy (e.g. region -> subregion -> state) as a Power BI
    drillable axis.
    tooltips add extra fields to the hover tooltip (e.g. Tableau's mark-card
    measures like YoY Growth %), without plotting them on the axis.
    hide_value_axis hides the Y axis (clean sparkline look).
    hide_labels suppresses data point labels.
    """
    all_values = [binding_projection(value)]
    if secondary_value:
        all_values.append(binding_projection(secondary_value))
    for av in (additional_values or []):
        all_values.append(binding_projection(av))
    cat_projs = [binding_projection(category)]
    for ec in (extra_categories or []):
        cat_projs.append(binding_projection(ec))
    qs = {"Category": {"projections": cat_projs},
          "Y": {"projections": all_values}}
    if series:
        qs["Series"] = {"projections": [binding_projection(series)]}
    if tooltips:
        qs["Tooltips"] = {"projections": [binding_projection(t) for t in tooltips]}
    query: Dict = {"queryState": qs}
    if sort:
        query["sortDefinition"] = sort
    objects: Dict = {}
    if series and series_colors:
        objects["dataPoint"] = [_series_datapoint(series, lbl, clr)
                                for lbl, clr in series_colors.items()]
    elif single_color:
        objects["dataPoint"] = [{"properties": {
            "fill": color(single_color), "showAllDataPoints": literal(True)}}]
    fg = (theme or {}).get("foreground")
    # Category axis — always show
    ax_props: Dict = {"show": literal(True)}
    if fg:
        ax_props["labelColor"] = color(fg)
        ax_props["titleColor"] = color(fg)
    objects["categoryAxis"] = [{"properties": ax_props}]
    # Value axis — hide for sparklines. Display units come from the bound measure
    # (e.g. Millions) so the measure's currency/precision formatString combines
    # with Power BI's own K/M/bn scaling instead of leaking the full raw number.
    if hide_value_axis:
        objects["valueAxis"] = [{"properties": {"show": literal(False)}}]
    else:
        va_props: Dict = {"show": literal(True),
                          "labelDisplayUnits": _display_units(value)}
        if fg:
            va_props["labelColor"] = color(fg)
            va_props["titleColor"] = color(fg)
        objects["valueAxis"] = [{"properties": va_props}]
    if series and fg:
        objects["legend"] = [{"properties": {
            "show": literal(True), "labelColor": color(fg)}}]
    # Data labels
    show_labels = not hide_labels and data_labels
    labels: Dict = {"show": literal(show_labels),
                    "labelDisplayUnits": _display_units(value)}
    if show_labels and fg:
        labels["color"] = color(fg)
    objects["labels"] = [{"properties": labels}]
    visual = {"visualType": visual_type, "query": query, "objects": objects,
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def combo_visual(name: str, pos: Dict, category: Dict,
                 column_values: List[Dict], line_values: List[Dict],
                 title: Optional[str] = None, theme: Optional[Dict] = None,
                 sort: Optional[Dict] = None,
                 tooltips: Optional[List[Dict]] = None) -> Dict:
    """Build a line-and-clustered-column combo chart (Tableau dual-axis).

    ``column_values`` plot as bars on the primary Y axis, ``line_values`` plot as
    a line on the secondary Y2 axis — reproducing a Tableau dual-axis worksheet
    (one measure as Bar, the other as Line over a shared category).
    """
    qs: Dict = {
        "Category": {"projections": [binding_projection(category)]},
        "Y": {"projections": [binding_projection(v) for v in column_values]},
        "Y2": {"projections": [binding_projection(v) for v in line_values]},
    }
    if tooltips:
        qs["Tooltips"] = {"projections": [binding_projection(t) for t in tooltips]}
    query: Dict = {"queryState": qs}
    if sort:
        query["sortDefinition"] = sort
    objects: Dict = {}
    fg = (theme or {}).get("foreground")
    ax_props: Dict = {"show": literal(True)}
    if fg:
        ax_props["labelColor"] = color(fg)
        ax_props["titleColor"] = color(fg)
    objects["categoryAxis"] = [{"properties": ax_props}]
    y1 = column_values[0] if column_values else None
    y2 = line_values[0] if line_values else None
    va_props: Dict = {"show": literal(True), "labelDisplayUnits": _display_units(y1)}
    if fg:
        va_props["labelColor"] = color(fg)
        va_props["titleColor"] = color(fg)
    objects["valueAxis"] = [{"properties": va_props}]
    if fg:
        objects["legend"] = [{"properties": {
            "show": literal(True), "labelColor": color(fg)}}]
    labels = {"show": literal(True), "labelDisplayUnits": _display_units(y2 or y1)}
    if fg:
        labels["color"] = color(fg)
    objects["labels"] = [{"properties": labels}]
    visual = {"visualType": "lineClusteredColumnComboChart", "query": query,
              "objects": objects,
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def pie_visual(name: str, pos: Dict, category: Dict, value: Dict,
               title: Optional[str] = None, theme: Optional[Dict] = None,
               donut: bool = False, series_colors: Optional[Dict] = None,
               tooltips: Optional[List[Dict]] = None) -> Dict:
    """Build a pie/donut visual.json dict (Category legend + Y values)."""
    qs = {
        "Category": {"projections": [binding_projection(category)]},
        "Y": {"projections": [binding_projection(value)]}}
    if tooltips:
        qs["Tooltips"] = {"projections": [binding_projection(t) for t in tooltips]}
    query = {"queryState": qs}
    objects: Dict = {}
    if series_colors:
        objects["dataPoint"] = [_series_datapoint(category, lbl, clr)
                                for lbl, clr in series_colors.items()]
    fg = (theme or {}).get("foreground")
    legend = {"show": literal(True)}
    labels = {"show": literal(True),
              "labelStyle": literal("Category, percent of total"),
              "labelDisplayUnits": _display_units(value)}
    if fg:
        legend["labelColor"] = color(fg)
        labels["color"] = color(fg)
    objects["legend"] = [{"properties": legend}]
    objects["labels"] = [{"properties": labels}]
    visual = {"visualType": "donutChart" if donut else "pieChart",
              "query": query, "objects": objects,
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def map_visual(name: str, pos: Dict, location: Dict, value: Dict,
               title: Optional[str] = None, theme: Optional[Dict] = None,
               gradient: Optional[List[str]] = None,
               tooltips: Optional[List[Dict]] = None) -> Dict:
    """Build a filledMap visual.json dict (Location category + Size measure).

    gradient=[minHex, maxHex] shades the choropleth by the measure via a
    linearGradient2 FillRule (light -> brand red for title density).
    """
    qs = {
        "Category": {"projections": [binding_projection(location)]},
        "Size": {"projections": [binding_projection(value)]}}
    if tooltips:
        qs["Tooltips"] = {"projections": [binding_projection(t) for t in tooltips]}
    query = {"queryState": qs}
    objects: Dict = {}
    if gradient:
        fill_hex = gradient[-1] if isinstance(gradient, list) and gradient else "#E50914"
        objects["dataPoint"] = [{"properties": {
            "fill": color(fill_hex),
            "showAllDataPoints": {"expr": {"Literal": {"Value": "true"}}}}}]
    # Pin the measure-driven colour legend to the bound measure's display units so
    # its labels scale consistently with the map saturation.
    objects["legend"] = [{"properties": {
        "show": literal(True), "labelDisplayUnits": _display_units(value)}}]
    visual = {"visualType": "filledMap", "query": query, "objects": objects,
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def table_visual(name: str, pos: Dict, columns: List[Dict],
                 title: Optional[str] = None,
                 theme: Optional[Dict] = None) -> Dict:
    """Build a tableEx visual.json dict from a list of {entity, prop} columns."""
    projections = [binding_projection(c) for c in columns]
    visual = {"visualType": "tableEx",
              "query": {"queryState": {"Values": {"projections": projections}}},
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def treemap_visual(name: str, pos: Dict, group: Dict, value: Dict,
                   title: Optional[str] = None, theme: Optional[Dict] = None,
                   series_colors: Optional[Dict] = None,
                   single_color: Optional[str] = None,
                   tooltips: Optional[List[Dict]] = None) -> Dict:
    """Build a treemap visual.json dict (Group category + Values measure).

    Reproduces Tableau's color+size+text 'Square' marks where rectangle area is
    the measure. Group is the categorical field, Values the sized measure.
    """
    qs = {
        "Group": {"projections": [binding_projection(group)]},
        "Values": {"projections": [binding_projection(value)]}}
    if tooltips:
        qs["Tooltips"] = {"projections": [binding_projection(t) for t in tooltips]}
    query = {"queryState": qs}
    objects: Dict = {}
    if series_colors:
        objects["dataPoint"] = [_series_datapoint(group, lbl, clr)
                                for lbl, clr in series_colors.items()]
    elif single_color:
        objects["dataPoint"] = [{"properties": {
            "fill": color(single_color), "showAllDataPoints": literal(True)}}]
    fg = (theme or {}).get("foreground")
    labels = {"show": literal(True), "labelDisplayUnits": _display_units(value)}
    if fg:
        labels["labelColor"] = color(fg)
    objects["dataLabels"] = [{"properties": labels}]
    # Pin the colour/size legend to the bound measure's display units too, so a
    # measure-driven legend scales consistently with the tile labels.
    legend_props: Dict = {"show": literal(True),
                          "labelDisplayUnits": _display_units(value)}
    if fg:
        legend_props["labelColor"] = color(fg)
    objects["legend"] = [{"properties": legend_props}]
    visual = {"visualType": "treemap", "query": query, "objects": objects,
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def matrix_visual(name: str, pos: Dict, rows: List[Dict],
                  columns: Optional[List[Dict]], values: List[Dict],
                  title: Optional[str] = None, theme: Optional[Dict] = None) -> Dict:
    """Build a matrix (pivotTable) visual.json dict.

    Reproduces a Tableau cross-tab / highlight table: row dimensions down the
    side, optional column dimensions across the top, measures in the cells.
    """
    qs: Dict = {"Rows": {"projections": [binding_projection(r) for r in rows]}}
    if columns:
        qs["Columns"] = {"projections": [binding_projection(c) for c in columns]}
    qs["Values"] = {"projections": [binding_projection(v) for v in values]}
    visual = {"visualType": "pivotTable", "query": {"queryState": qs},
              "visualContainerObjects": container_objects(title, theme)}
    return {"$schema": VC_SCHEMA, "name": name, "position": pos, "visual": visual}


def textbox_visual(name: str, pos: Dict, text: str, size: int = 18,
                   bold: bool = True, hex_color: str = "#004263") -> Dict:
    """Build a textbox visual with a single styled paragraph run."""
    run = {"value": text, "textStyle": {
        "fontSize": f"{size}pt", "fontWeight": "bold" if bold else "normal",
        "color": hex_color}}
    paragraphs = [{"textRuns": [run]}]
    return {
        "$schema": VC_SCHEMA, "name": name, "position": pos,
        "visual": {"visualType": "textbox",
                   "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]}},
    }


def nav_button_visual(name: str, pos: Dict, label: Optional[str],
                      target_page: Optional[str], fill: str = "#004263",
                      text_color: str = "#ffffff") -> Dict:
    """Build an actionButton wired to Power BI page navigation.

    Reproduces a Tableau goto-sheet button. The navigation action MUST live in
    visualContainerObjects.visualLink (type=PageNavigation + navigationSection);
    placing it under visual.objects makes the button render but do nothing. When
    target_page is None the button still renders (styled) but carries no action.
    """
    # A transparent fill (e.g. a Tableau '#00000000' mark colour adopted by the
    # auto-derived theme) renders the button as an invisible all-white tile, so
    # force an opaque fill and frame it with a contrasting outline -- the button
    # is then always visible regardless of the source theme or page background.
    fill = opaque_color(fill)
    objects: Dict = {
        "icon": [{"properties": {"shapeType": literal("Arrow"),
                                 "lineColor": color(text_color)}}],
        "outline": [{"properties": {"show": literal(True),
                                    "lineColor": color(text_color),
                                    "weight": _num(1)}}],
        "fill": [{"properties": {"show": literal(True), "fillColor": color(fill)}}],
        "text": [{"properties": {
            "show": literal(bool(label)),
            "text": literal(label or ""),
            "fontColor": color(text_color)}}],
    }
    link_props: Dict = {"show": literal(True), "type": literal("PageNavigation")}
    if target_page:
        link_props["navigationSection"] = literal(target_page)
    vco: Dict = {
        "visualLink": [{"properties": link_props}],
        "title": [{"properties": {"show": literal(False)}}],
        "background": [{"properties": {"show": literal(True), "color": color(fill)}}],
        "border": [{"properties": {"show": literal(False)}}],
    }
    return {"$schema": VC_SCHEMA, "name": name, "position": pos,
            "visual": {"visualType": "actionButton",
                       "objects": objects, "visualContainerObjects": vco}}


BOOKMARK_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.4.0/schema.json"
BOOKMARKS_META_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json"


def bookmark_button_visual(name: str, pos: Dict, label: Optional[str],
                           bookmark_id: str, fill: str = "#004263",
                           text_color: str = "#ffffff", icon: str = "Filter",
                           show_text: bool = False) -> Dict:
    """Build an actionButton wired to a generated bookmark (Tableau toggle).

    Reproduces a Tableau show/hide toggle button. The action MUST live in
    visualContainerObjects.visualLink (type=Bookmark + bookmark id); putting it
    under visual.objects makes the button render but do nothing.
    """
    # A transparent fill (e.g. a Tableau '#00000000' mark colour adopted by the
    # auto-derived theme) renders the toggle button as an invisible all-white
    # tile, so force an opaque fill, a contrasting icon glyph and a framing
    # outline -- the button is always visible regardless of theme/page colour.
    fill = opaque_color(fill)
    objects: Dict = {
        "icon": [{"properties": {"shapeType": literal(icon),
                                 "lineColor": color(text_color)}}],
        "outline": [{"properties": {"show": literal(True),
                                    "lineColor": color(text_color),
                                    "weight": _num(1)}}],
        "fill": [{"properties": {"show": literal(True), "fillColor": color(fill)}}],
        "text": [{"properties": {
            "show": literal(bool(show_text and label)),
            "text": literal(label or ""),
            "fontColor": color(text_color)}}],
    }
    vco: Dict = {
        "visualLink": [{"properties": {
            "show": literal(True),
            "type": literal("Bookmark"),
            "bookmark": literal(bookmark_id)}}],
        "title": [{"properties": {"show": literal(False)}}],
        "background": [{"properties": {"show": literal(True), "color": color(fill)}}],
        "border": [{"properties": {"show": literal(False)}}],
    }
    return {"$schema": VC_SCHEMA, "name": name, "position": pos,
            "visual": {"visualType": "actionButton",
                       "objects": objects, "visualContainerObjects": vco}}


def bookmark_definition(bm_id: str, display_name: str, page_name: str,
                        target_visual_names: List[str],
                        hidden_visual_names: List[str]) -> Dict:
    """Build one .bookmark.json. Listed visuals are HIDDEN; omitted ones show.

    The bookmark/1.4.0 schema's display.mode allows ONLY hidden/maximize/spotlight/
    elevation — there is NO "visible" mode. To make a visual VISIBLE we OMIT it
    from visualContainers; to HIDE it we list it with mode "hidden".
    """
    containers = {n: {"singleVisual": {"display": {"mode": "hidden"}}}
                  for n in hidden_visual_names}
    return {
        "$schema": BOOKMARK_SCHEMA,
        "displayName": display_name,
        "name": bm_id,
        "options": {
            "applyOnlyToTargetVisuals": True,
            "targetVisualNames": list(target_visual_names),
            "suppressData": True,
            "suppressActiveSection": True,
        },
        "explorationState": {
            "version": "1.3",
            "activeSection": page_name,
            "sections": {page_name: {"visualContainers": containers}},
        },
    }


def bookmarks_metadata(names: List[str]) -> Dict:
    """Build definition/bookmarks/bookmarks.json listing every generated bookmark."""
    return {"$schema": BOOKMARKS_META_SCHEMA,
            "items": [{"name": n} for n in names]}


def card_visual(name: str, pos: Dict, entity: str, measure: str,
                title: Optional[str] = None, theme: Optional[Dict] = None,
                display_units: int = DISPLAY_UNITS_NONE) -> Dict:
    """Build a single-value card bound to a measure."""
    proj = {"field": {"Measure": {
        "Expression": {"SourceRef": {"Entity": entity}}, "Property": measure}},
        "queryRef": f"{entity}.{measure}", "nativeQueryRef": measure}
    objects = _card_label_objects(theme, display_units)
    return {"$schema": VC_SCHEMA, "name": name, "position": pos,
            "visual": {"visualType": "card",
                       "query": {"queryState": {"Values": {"projections": [proj]}}},
                       "objects": objects,
                       "visualContainerObjects": container_objects(title, theme, title_size=13)}}


def card_text_visual(name: str, pos: Dict, entity: str, column: str,
                     title: Optional[str] = None,
                     theme: Optional[Dict] = None) -> Dict:
    """Build a card bound to a text/dimension column (shows the value text).

    The column is wrapped in a Min aggregation (QueryAggregateFunction=3) so the
    card always collapses to a single value: when a title is selected it shows
    that title's value, and with no selection it shows a representative (first
    alphabetical) value instead of a blank "(multiple values)" card.
    """
    proj = {
        "field": {"Aggregation": {
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": column}},
            "Function": 3}},
        "queryRef": f"Min({entity}.{column})",
        "nativeQueryRef": f"First {column}",
    }
    objects = _card_label_objects(theme)
    return {"$schema": VC_SCHEMA, "name": name, "position": pos,
            "visual": {"visualType": "card",
                       "query": {"queryState": {"Values": {"projections": [proj]}}},
                       "objects": objects,
                       "visualContainerObjects": container_objects(title, theme, title_size=13)}}


def card_agg_visual(name: str, pos: Dict, entity: str, column: str, func: int,
                    label: str, title: Optional[str] = None,
                    theme: Optional[Dict] = None) -> Dict:
    """Build a card whose value is an inline aggregation of a numeric column.

    Used when a worksheet drops an implicit aggregation straight onto a card (e.g.
    ``AVG(int_rate)``) with no named calculation, so there is no model measure to
    bind to. ``func`` is the Power BI QueryAggregateFunction enum value and
    ``label`` its human name (Sum/Average/Min/Max/Count), so the card reproduces the
    exact aggregation the Tableau worksheet showed instead of defaulting to Min.
    """
    proj = {
        "field": {"Aggregation": {
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": column}},
            "Function": func}},
        "queryRef": f"{label}({entity}.{column})",
        "nativeQueryRef": f"{label} of {column}",
    }
    objects = _card_label_objects(theme)
    return {"$schema": VC_SCHEMA, "name": name, "position": pos,
            "visual": {"visualType": "card",
                       "query": {"queryState": {"Values": {"projections": [proj]}}},
                       "objects": objects,
                       "visualContainerObjects": container_objects(title, theme, title_size=13)}}


def _card_label_objects(theme: Optional[Dict],
                        display_units: int = DISPLAY_UNITS_NONE) -> Dict:
    """Card value label styling (light bold value, hidden category label)."""
    fg = (theme or {}).get("foreground")
    label = {"show": literal(True), "bold": literal(True), "fontSize": _num(20),
             "labelDisplayUnits": _num(display_units or DISPLAY_UNITS_NONE)}
    if fg:
        label["color"] = color(fg)
    return {"labels": [{"properties": label}],
            "categoryLabels": [{"properties": {"show": literal(False)}}]}


def page_json(name: str, display: str, width: int, height: int,
              background: Optional[str] = None,
              outspace: Optional[str] = None) -> Dict:
    """Build a page.json dict (optionally with a dark canvas + outspace)."""
    page = {"$schema": PAGE_SCHEMA, "name": name, "displayName": display,
            "displayOption": "FitToPage", "height": height, "width": width}
    objects: Dict = {}
    if background:
        objects["background"] = [{"properties": {
            "color": color(background),
            "transparency": {"expr": {"Literal": {"Value": "0D"}}}}}]
    if outspace:
        objects["outspace"] = [{"properties": {
            "color": color(outspace),
            "transparency": {"expr": {"Literal": {"Value": "0D"}}}}}]
    if objects:
        page["objects"] = objects
    return page


def pages_json(order: List[str], active: str) -> Dict:
    """Build the pages.json metadata dict."""
    return {"$schema": PAGES_SCHEMA, "pageOrder": order, "activePageName": active}
