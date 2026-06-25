"""emit_pbir.py — Stage 13 deterministic PBIR report generator.

Consumes analysis.json (IR) + decisions.json and writes a complete
{Model}.Report/ folder in enhanced PBIR folder format. Tableau zone coordinates
are scaled to Power BI pixels; field bindings come from pbir_bind. Each
visual.json root carries $schema/name/position/visual, plus an optional
filterConfig when the visual is part of a Tableau show/hide toggle group.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "twb"))
import pbir_blocks as P  # noqa: E402
import pbir_bind as B  # noqa: E402
import mark_infer as MI  # noqa: E402

PBIR = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
    "version": "4.0",
}
REPORT = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json",
    "themeCollection": {}, "settings": {"useStylableVisualContainerHeader": True},
}
VERSION = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
    "version": "2.0.0",
}
PLATFORM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"

# Tableau stores dashboard zone geometry in a normalized 0..100000 coordinate
# space (per axis). Scale into the page's pixel size so visuals land on-canvas.
COORD_SPACE = 100000.0

# Tableau show/hide containers collapse the inactive states of a parameter-driven
# toggle into near-zero-height slivers (e.g. the Daily bar / Data Table views that
# only appear when a parameter selects them). Any data visual whose rendered
# height is below this many pixels is one of those collapsed states; suppress it
# so it does not overlap the active visual. A genuine visual is never this short.
MIN_VISUAL_PX = 16

# Many Tableau dashboards float a filter/parameter panel over the right edge of a
# full-width content grid. Scaling content across the whole canvas then slides it
# UNDER that rail. We instead compress content into the area left of the rail and
# keep the rail at its real right-hand position, leaving this pixel gap between.
RAIL_GAP = 16
# A zone counts as part of the rail only if its left edge sits within this many
# Tableau coordinate units of the rail column's x (so content labels that merely
# happen to start at a large x — e.g. table header captions — stay with content).
RAIL_X_TOL = 2000


def _rail_left_tableau(dashboard: Dict) -> Optional[float]:
    """Detect a right-edge floating filter rail; return its Tableau x or None.

    Heuristic: two or more filter/paramctrl zones whose left edges align on the
    right third of the dashboard indicate a dedicated filter column.
    """
    xs = [z.get("x", 0) for z in dashboard.get("zones", [])
          if z.get("type") in ("filter", "paramctrl")]
    if len(xs) >= 2 and min(xs) > 0.6 * COORD_SPACE:
        return min(xs)
    return None


def _logical_id(seed: str) -> str:
    import hashlib
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-9{h[17:20]}-{h[20:32]}"


def load_json(path: str) -> Dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Always write UTF-8 WITHOUT BOM — Power BI Desktop rejects BOM in PBIR files.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def sanitize(name: str) -> str:
    """Page names must match ^[\\w-]+$."""
    return re.sub(r"[^\w-]+", "", name.replace(" ", "")) or "Page"


def _rmtree_robust(path: str) -> None:
    """Remove a directory tree, clearing read-only bits (Windows/OneDrive)."""
    import stat

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if hasattr(shutil, "rmtree"):
        try:
            shutil.rmtree(path, onerror=_onerror)
        except TypeError:  # Python 3.12+ renamed the callback
            shutil.rmtree(path, onexc=lambda f, p, e: _onerror(f, p, e))


# Unambiguous Tableau mark class -> Power BI visualType. Choosing a Power BI
# visual is a Power-BI-stage decision; the deterministic mapping is shared with
# the parser (which stamps it into the IR as inferredVisualType) via mark_infer.
MARK_MAP = MI.MARK_MAP
MAP_MARKS = MI.MAP_MARKS


def mark_to_visual(ws: Dict) -> Optional[str]:
    """Derive a Power BI visualType from the worksheet's Tableau mark FACTS.

    Prefers the IR-stamped inferredVisualType (set by the parser); otherwise
    recomputes from mark facts. Returns None when still ambiguous.
    """
    return ws.get("inferredVisualType") or MI.infer_visual_type(ws)


def resolve_visual_type(ws_name: str, ir: Dict, decisions: Dict) -> str:
    """Prefer LLM decision; else derive from Tableau mark facts; else table."""
    for vd in decisions.get("visualDecisions", []):
        if vd["worksheet"] == ws_name:
            return vd["visualType"]
    for ws in ir.get("worksheets", []):
        if ws["name"] == ws_name:
            return mark_to_visual(ws) or "tableEx"
    return "tableEx"


def visual_decision(ws_name: str, decisions: Dict) -> Dict:
    """Return the decisions.visualDecisions entry for a worksheet (or {})."""
    for vd in decisions.get("visualDecisions", []):
        if vd["worksheet"] == ws_name:
            return vd
    return {}


def _agent_decided(ws_name: str, decisions: Dict) -> bool:
    """True when the agent explicitly chose a visual type for this worksheet, so a
    deterministic mark-based promotion (e.g. text-table -> matrix) must not override
    it."""
    return any(vd.get("worksheet") == ws_name
               for vd in decisions.get("visualDecisions", []))


# A MAX/MIN aggregation over a date/datetime column -- Tableau's continuous time
# axis pill (e.g. ``MAX([EXTRACT_DATETIME])`` placed on the columns shelf), never a
# plottable Y metric.
_DATE_AGG_RX = re.compile(
    r"\b(?:MAX|MIN)\s*\(\s*[^()\[\]]*\[(?P<col>[^\]]+)\]\s*\)", re.IGNORECASE)


def _is_temporal_value(valbind: Optional[Dict], dcols: set,
                       decisions: Optional[Dict]) -> bool:
    """True when a cartesian value binding actually plots a DATE/DATETIME axis
    (a ``MAX``/``MIN`` of a date column, or an inline aggregation of one). Such a
    value is Tableau's time axis mis-routed onto Y; rendering a datetime on the
    value axis crashes Power BI's cartesian visual (``categoryIdentities`` error)."""
    if not valbind:
        return False
    prop = valbind.get("prop")
    if not prop:
        return False
    if valbind.get("isMeasure"):
        for m in (decisions or {}).get("measures", []):
            if m.get("name") == prop:
                mm = _DATE_AGG_RX.search(m.get("dax") or "")
                return bool(mm and mm.group("col") in dcols)
        return False
    # Inline aggregation pill straight off a date column.
    return prop in dcols


# Tableau-friendly aliases mapped to the concrete Power BI cartesian visualType.
CHART_TYPE_MAP = {
    "columnChart": "clusteredColumnChart",
    "barChart": "clusteredBarChart",
    "stackedColumn": "stackedColumnChart",
    "stackedBar": "stackedBarChart",
}
CARTESIAN = {
    "clusteredColumnChart", "clusteredBarChart", "stackedColumnChart",
    "stackedBarChart", "stackedAreaChart", "lineChart", "areaChart",
    "columnChart", "barChart",
}


def primary_entity(decisions: Dict) -> str:
    for t in decisions.get("tables", []):
        if t.get("role") == "fact":
            return t["name"]
    tables = decisions.get("tables", [])
    return tables[0]["name"] if tables else "Table"


def _build_kpi_stack(name: str, pos: Dict, x: int, y: int, w: int, h: int,
                     z: int, vd: Dict, entity: str, theme,
                     units: Optional[Dict] = None,
                     decisions: Optional[Dict] = None) -> List[Dict]:
    """Return [card_visual, pct_card_visual, sparkline_visual] for a KPI zone."""
    units = units or {}
    decisions = decisions or {}
    # A measure reference must name the table the measure is DEFINED on, not the
    # fact entity, or Power BI cannot resolve it and the tile renders broken.
    me = lambda m: B.measure_entity(m, decisions, entity)
    card_h  = round(h * 0.30)
    pct_h   = round(h * 0.15)
    spark_h = h - card_h - pct_h
    title_text = vd.get("kpiTitle", "KPI")
    total_m = vd.get("kpiMeasure", "Total Sales")
    pct_m   = vd.get("kpiPctMeasure", "% Diff Sales")
    sec_v   = vd.get("secondaryValue")
    # card — big CY number
    card = P.card_visual(
        f"card_{name}", P.position(x, y, w, card_h, z),
        me(total_m), total_m, title=title_text, theme=theme,
        display_units=units.get(total_m, 1),
    )
    # % diff card
    pct = P.card_visual(
        f"pct_{name}", P.position(x, y + card_h, w, pct_h, z + 1),
        me(pct_m), pct_m, title=None, theme=theme,
        display_units=units.get(pct_m, 1),
    )
    # sparkline — keep original type (lineChart)
    spark_vd = dict(vd)
    spark_vd.pop("kpiStack", None)
    sec_bind = ({"entity": me(sec_v), "prop": sec_v, "isMeasure": True,
                 "displayUnits": units.get(sec_v, 1)}) if sec_v else None
    add_binds = [{"entity": me(av), "prop": av, "isMeasure": True,
                  "displayUnits": units.get(av, 1)}
                 for av in (vd.get("additionalValues") or [])]
    catbind  = {"entity": entity, "prop": vd.get("categoryField", "Order Date")}
    valbind  = {"entity": me(total_m), "prop": total_m, "isMeasure": True,
                "displayUnits": units.get(total_m, 1)}
    spark = P.chart_visual(
        f"spark_{name}", P.position(x, y + card_h + pct_h, w, spark_h, z + 2),
        "lineChart", catbind, valbind, title_text, theme=theme,
        secondary_value=sec_bind, additional_values=add_binds or None,
        hide_value_axis=True, hide_labels=True,
    )
    return [card, pct, spark]


def _kpi_tile_decision(ws: Optional[Dict], mset: set) -> Optional[Dict]:
    """Recover an executive KPI/BAN tile as a kpiStack decision, or None.

    Superstore-style KPI tiles pair a current-year measure (``CY <topic>``) with a
    prior-year measure (``PY <topic>``) and a ``% Diff <topic>`` over a date
    sparkline; Tableau renders the trio as a stacked BAN (big number + % vs PY +
    trend). When the agent leaves no visual decision the deterministic path would
    otherwise dump every shelf field PLUS the dashboard-filter dimensions (which
    the IR keeps in ``dimensions``) as a wide flat table. Matching the CY/PY
    measure convention together with a plotted date grain rebuilds the tile
    deterministically. Returns None for any worksheet that is not this exact shape,
    so workbooks without the convention are never touched.
    """
    if not ws or not ws.get("categoryDateLevel"):
        return None
    names = [m.get("field") for m in (ws.get("measures") or []) if m.get("field")]
    cy = next((n for n in names if n.upper().startswith("CY ") and n in mset), None)
    py = next((n for n in names if n.upper().startswith("PY ") and n in mset), None)
    if not cy or not py:
        return None
    pct = next((n for n in names if n.startswith("% Diff") and n in mset), None)
    # Title head = the Tableau BAN title's first static line (drop {value} rows).
    title = ws.get("title") or ""
    head = next((ln.strip() for ln in title.splitlines()
                 if ln.strip() and "{" not in ln), None) \
        or re.sub(r"^KPI\s+", "", ws.get("name") or "KPI")
    vd: Dict = {
        "kpiStack": True,
        "kpiTitle": head,
        "kpiMeasure": cy,
        "secondaryValue": py,
        "categoryField": ws.get("categoryField") or "Order Date",
    }
    if pct:
        vd["kpiPctMeasure"] = pct
    return vd


def _col_caption(ir: Dict, prop: str) -> Optional[str]:
    """Friendly caption for a column name ('type' -> 'Type'), else None."""
    for c in ir.get("columns", []):
        if c.get("name") == prop and c.get("caption"):
            return c["caption"]
    return None


def _derive_theme(ir: Dict) -> Dict:
    """Build a default report theme from the Tableau worksheets' own formatting.

    Tableau stores each sheet's background / text / mark colour in
    ``ws.formatting`` (parsed into the IR). When ``decisions`` carries no explicit
    ``theme`` we adopt the dashboard's dominant colours so the Power BI page is
    not left on the default white canvas — which hides the white axis labels and
    leaves bar marks the default blue instead of the source palette. Fully generic:
    any dark/branded workbook inherits its own colours.
    """
    bgs: Counter = Counter()
    fgs: Counter = Counter()
    marks: Counter = Counter()
    titles: Counter = Counter()
    for ws in ir.get("worksheets", []):
        f = ws.get("formatting") or {}
        if f.get("background"):
            bgs[f["background"]] += 1
        if f.get("fontColor"):
            fgs[f["fontColor"]] += 1
        if f.get("markColor"):
            marks[f["markColor"]] += 1
        if f.get("titleColor"):
            titles[f["titleColor"]] += 1
    t: Dict = {}
    if bgs:
        bg = bgs.most_common(1)[0][0]
        t["pageBackground"] = bg
        t["outspace"] = bg
        t["visualBackground"] = bg
    if fgs:
        t["foreground"] = fgs.most_common(1)[0][0]
    if marks:
        t["markColor"] = marks.most_common(1)[0][0]
    if titles:
        t["titleColor"] = titles.most_common(1)[0][0]
    return t


def _ws_theme(base: Optional[Dict], ws: Optional[Dict]) -> Optional[Dict]:
    """Overlay a worksheet's own Tableau formatting onto the dashboard theme.

    The dashboard-wide ``decisions.theme`` sets the baseline (page background,
    foreground). Each Tableau worksheet, however, carries its OWN title colour /
    font / size and text colour (the Netflix sheets use red 'Tableau Bold' titles
    on a black card). Those are parsed into ``ws.formatting`` — overlay them here
    so every visual's title and text match its source sheet instead of a single
    global colour. Returns the base unchanged when the sheet has no formatting.
    """
    if not ws:
        return base
    fmt = ws.get("formatting") or {}
    t = dict(base or {})
    if fmt.get("titleColor"):
        t["titleColor"] = fmt["titleColor"]
    if fmt.get("titleFontName"):
        # 'Tableau Bold'/'Tableau Book' are Tableau-only fonts not installed in
        # Power BI — map to the closest Segoe face so the title still renders bold.
        fn = str(fmt["titleFontName"]).lower()
        t["titleFont"] = "Segoe UI Bold" if "bold" in fn else "Segoe UI Semibold"
    if fmt.get("titleFontSize"):
        try:
            t["titleFontSize"] = int(round(float(fmt["titleFontSize"])))
        except (TypeError, ValueError):
            pass
    if fmt.get("fontColor"):
        t["foreground"] = fmt["fontColor"]
    if fmt.get("markColor"):
        t["markColor"] = fmt["markColor"]
    if fmt.get("background"):
        t.setdefault("visualBackground", fmt["background"])
    return t


def _tooltip_binds(ws: Optional[Dict], vd: Dict, decisions: Dict, ir: Dict,
                   entity: str, mset: set, cols, primary: Optional[str],
                   exclude: set) -> List[Dict]:
    """Tooltip projections are disabled: no explicit Tooltips well is emitted on
    any visual. Power BI still shows its own default hover (the plotted category
    and value); we simply never add extra tooltip fields, which previously could
    change the aggregation grain or surface mismatched fields. Always returns []."""
    return []


def _is_card_ws(ws: Optional[Dict]) -> bool:
    """True when a worksheet is a single-value KPI/BAN card: it carries a measure
    but places NO dimension on a plotted shelf (rows/cols/categoryField).

    The IR's ``dimensions`` list also includes filter-only fields, so it cannot be
    used to decide whether a worksheet has a real category axis; the shelf does.
    """
    if not ws:
        return False
    if ws.get("categoryField"):
        return False
    vals = set(ws.get("values") or [])
    shelf_dims = [f for f in (ws.get("rows") or []) + (ws.get("cols") or [])
                  if f not in vals]
    if shelf_dims:
        return False
    # A temporal field on a shelf is a real time axis -- a trend line, not a
    # scalar card -- even when the parser aggregated it (SUM(year)) and counted it
    # among the values, leaving no plain shelf dimension. Shares mark_infer's
    # temporal signal so the parser inference and the emitter agree.
    if MI.has_temporal_axis(ws):
        return False
    return bool(ws.get("values") or ws.get("measures"))


def _is_date_trend(ws: Optional[Dict], mset: set) -> bool:
    """True when a worksheet plots a DATE axis with one or more measure pills on
    the rows/cols shelf: a time trend. Used as a last-resort emit rescue so an
    ambiguous date trend that carried no agent decision renders as a line chart
    instead of dumping its rows as a flat table. KPI/BAN tiles (CY+PY over a date
    grain) are recovered separately by ``_kpi_tile_decision`` and excluded by the
    caller, so this only catches the plain multi-measure trend (e.g. CY Sales +
    CY Profit by week)."""
    if not ws or not ws.get("categoryDateLevel"):
        return False
    if (ws.get("markClass") or "Automatic") == "Text":
        return False
    shelf = (ws.get("rows") or []) + (ws.get("cols") or [])
    return any(f in mset for f in shelf)


# A model measure that is a single, simple aggregation of ONE column, e.g. the
# synthesized 'Sum of year' = SUM(date[year]). Used to detect when a chart's
# value resolved to the category-axis column's own aggregation (a spurious series).
_SIMPLE_AGG_RX = re.compile(
    r"^\s*(?:SUM|AVERAGE|AVG|MIN|MAX|COUNT|COUNTA|DISTINCTCOUNT)\s*\(\s*"
    r"(?:'[^']+'|\w+)\s*\[(?P<col>[^\]]+)\]\s*\)\s*$", re.IGNORECASE)


def _measure_aggs_column(measure_name: Optional[str], column: Optional[str],
                         decisions: Dict) -> bool:
    """True when ``measure_name`` is a plain aggregation of ``column`` (e.g.
    'Sum of year' = SUM(date[year])). Lets the cartesian emitter drop the
    category-axis field from the plotted values so a trend line shows the metric,
    not the axis column's totals as a second series."""
    if not measure_name or not column:
        return False
    for m in decisions.get("measures", []):
        if m.get("name") == measure_name:
            mm = _SIMPLE_AGG_RX.match(m.get("dax") or "")
            return bool(mm and mm.group("col") == column)
    return False


def _shelf_dims(ws: Optional[Dict], shelf: str, cols: set) -> List[str]:
    """Dimension fields placed on a worksheet's ``rows``/``cols`` shelf (excluding
    measure pills and fields that are not real model columns)."""
    if not ws:
        return []
    vals = set(ws.get("values") or [])
    return [f for f in (ws.get(shelf) or []) if f not in vals and f in cols]


def _is_crosstab_ws(ws: Optional[Dict], cols: set) -> bool:
    """True when a worksheet has dimensions on BOTH the rows and cols shelves: a
    Tableau cross-tab / Square-mark heat map. The faithful Power BI equivalent is a
    matrix, not a treemap (whose single Group would take a high-cardinality key and
    explode into thousands of tiles)."""
    return bool(_shelf_dims(ws, "rows", cols)) and bool(_shelf_dims(ws, "cols", cols))


# Tableau aggregation token -> Power BI QueryAggregateFunction enum code.
_BY_MEASURE_AGG = {
    "SUM": 0, "AVG": 1, "AVERAGE": 1, "COUNTD": 2, "DISTINCTCOUNT": 2,
    "MIN": 3, "MAX": 4, "COUNT": 5, "MEDIAN": 8,
}
_BY_MEASURE_RX = re.compile(r"\s*([A-Za-z]+)\s*\(\s*\[(.+?)\]\s*\)\s*$")


def _parse_by_measure(expr: Optional[str]):
    """Parse a Tableau Top-N ranking expression like ``COUNTD([show_id])`` into a
    ``(agg_code, column_name)`` pair. Returns ``(None, None)`` when it is not a
    simple single-column aggregation."""
    if not expr:
        return (None, None)
    m = _BY_MEASURE_RX.match(expr)
    if not m:
        return (None, None)
    return (_BY_MEASURE_AGG.get(m.group(1).upper()), m.group(2))


def _topn_order(ws: Dict, cat_entity: Optional[str], valbind: Optional[Dict],
                decisions: Dict, ir: Dict, mset: set) -> Optional[Dict]:
    """Resolve how a Top-N is ranked into kwargs for ``topn_filter_config``.

    Three tiers, most-faithful first:
      1. the value plotted on the axis is a model measure -> rank by that measure;
      2. the Tableau ranking expression references (a copy of) a model measure the
         worksheet also plots -> rank by that measure (handles ranked tables whose
         ``byMeasure`` is a calc copy like ``SUM([CY Sales (copy)_2378...])``);
      3. the ranking expression is an aggregation of a real base column
         (e.g. ``COUNTD([show_id])``) -> rank by an inline aggregation, so a count
         of a non-measure text column still ranks the Top-N.
    Returns None when none apply (so the caller emits no filter).
    """
    if valbind and valbind.get("isMeasure") and valbind.get("prop"):
        return {"order_measure": valbind["prop"], "order_entity": valbind.get("entity")}
    agg, col = _parse_by_measure((ws.get("topN") or {}).get("byMeasure"))
    if not col:
        return None
    # Strip Tableau calc-copy decoration: 'CY Sales (copy)_2378...' -> 'CY Sales'.
    clean = re.sub(r"\s*\(copy\)", "", col)
    clean = re.sub(r"_\d{6,}$", "", clean).strip()
    clean = B.base_field(clean)
    for v in (ws.get("values") or []):
        if v in mset and (B.base_field(v) or "").lower() == (clean or "").lower():
            return {"order_measure": v, "order_entity": B._field_entity(v, decisions, ir)}
    if agg is None:
        return None
    base_col = B.base_field(col)
    return {"order_agg": agg, "order_col": base_col,
            "order_entity": B.entity_for_field(base_col, cat_entity, decisions, ir)}


def _topn_config(ws: Optional[Dict], catbind: Optional[Dict],
                 valbind: Optional[Dict], decisions: Optional[Dict] = None,
                 ir: Optional[Dict] = None, mset: Optional[set] = None) -> Optional[Dict]:
    """Top-N visual filterConfig when the worksheet ranks its plotted category.

    Generic across reports: any worksheet whose Tableau Top-N filter is on the
    field bound to the category axis gets a matching Power BI Top-N filter ranked
    by the worksheet's own ranking expression (e.g. 'Top 10 States by Total Loan
    Volume' shows 10 states; 'Top 10 Genre' by DISTINCTCOUNT(show_id) shows 10
    genres). Returns None when there is no Top-N, the count is non-numeric, the
    Top-N is on a different field than the axis, or the ranking cannot be resolved.
    """
    tn = ws.get("topN") if ws else None
    if not tn or not catbind:
        return None
    n = tn.get("n")
    cat_prop = catbind.get("prop")
    tn_field = tn.get("field")
    if not isinstance(n, int) or not cat_prop or not tn_field:
        return None
    if B.base_field(cat_prop) != B.base_field(tn_field):
        return None
    order = _topn_order(ws, catbind.get("entity"), valbind,
                        decisions or {}, ir or {}, mset or set())
    if not order:
        return None
    return P.topn_filter_config(
        entity=catbind.get("entity"), category_prop=cat_prop, n=n,
        direction=tn.get("direction", "TOP"), **order)


def _topn_table_config(ws: Optional[Dict], tcols: List[Dict], entity: str,
                       decisions: Dict, ir: Dict, mset: set) -> Optional[Dict]:
    """Top-N filterConfig for a ranked detail TABLE (Tableau 'Top 10 Customers').

    Binds the Tableau Top-N field to the matching displayed column when present,
    then ranks via the same resolver as the chart path. Returns None when the
    worksheet carries no Top-N or the ranking field cannot be matched.
    """
    tn = ws.get("topN") if ws else None
    if not tn or not tn.get("field"):
        return None
    base = B.base_field(tn["field"])
    catbind = None
    for tc in tcols:
        if not tc.get("isMeasure") and B.base_field(tc.get("prop")) == base:
            catbind = {"entity": tc["entity"], "prop": tc["prop"]}
            break
    if catbind is None:
        catbind = {"entity": B.entity_for_field(tn["field"], entity, decisions, ir),
                   "prop": base}
    return _topn_config(ws, catbind, None, decisions, ir, mset)


def build_visual(zone: Dict, ir: Dict, decisions: Dict, z: int, geom) -> Optional[Dict]:
    """Build one visual.json dict from a classified dashboard zone.

    ``geom`` is the pre-scaled pixel rectangle ``(x, y, w, h, raw_h)`` computed by
    ``build_page`` (which compresses content to the left of any floating filter
    rail and clamps it on-canvas). ``raw_h`` is the unclamped scaled height, used
    only for the collapsed-sliver test.
    """
    x, y, w, h, raw_h = geom
    theme = decisions.get("theme")
    pos = P.position(x, y, w, h, z)
    entity = primary_entity(decisions)
    ztype = zone.get("type")
    if ztype == "text":
        txt = zone.get("text") or ""
        return P.textbox_visual(f"text_{z}", pos, txt) if txt else None
    if ztype in ("filter", "paramctrl"):
        # Slicers inherit the referenced worksheet's title formatting (Tableau
        # filter cards use the same red bold header as the sheet titles).
        sl_theme = _ws_theme(theme, B.ws_by_name(ir, zone.get("worksheet")))
        fp = B.field_param_by_field(zone.get("field"), decisions)
        if fp is not None:
            default = (fp.get("fields") or [{}])[0].get("label")
            return P.slicer_visual(f"slicer_{B.slug(fp['name'])}_{z}", pos,
                                   fp["name"], fp["name"], fp["name"],
                                   default_value=default, theme=sl_theme)
        ent, prop, title, mode = B.resolve_slicer(zone, ir, decisions)
        # Prefer the column's friendly caption ('type' -> 'Type') for the header.
        title = _col_caption(ir, prop) or title
        return P.slicer_visual(f"slicer_{B.slug(prop)}_{z}", pos, ent, prop, title,
                               mode=mode, theme=sl_theme)
    ws_name = zone.get("worksheet") or ""
    # A field parameter collapses several toggle worksheets into one chart; the
    # non-primary ones (e.g. the Daily duplicate) are suppressed here.
    if ws_name in B.suppressed_worksheets(decisions):
        return None
    # Suppress Tableau show/hide collapsed slivers (inactive parameter states)
    # so they never overlap the active visual. Generic across workbooks.
    if raw_h < MIN_VISUAL_PX:
        return None
    ws = B.ws_by_name(ir, ws_name)
    # A Tableau federated (multi-CSV) join exposes a shared column under a file
    # alias (e.g. 'state (state_region.csv)'). After the star split that column
    # lives in its dim under the plain name, so strip the alias on every field
    # reference before binding.
    ws = B.strip_alias_in_ws(ws)
    # Overlay this worksheet's own Tableau title/text formatting (red title, font,
    # text colour) onto the dashboard theme so each visual matches its source sheet.
    theme = _ws_theme(theme, ws)
    name = f"visual_{sanitize(ws_name or 'zone')}_{z}".lower()
    vd = visual_decision(ws_name, decisions)
    vtype = resolve_visual_type(ws_name, ir, decisions)
    mlist = B.measure_list(decisions)
    mset = set(mlist)
    cols = B.column_names(ir)
    valf = ws.get("valueField") if ws else None
    # Parameter-echo measures ('Current Year' = SELECTEDVALUE of the year slicer)
    # return a constant scalar, not a metric. They must never be a card headline
    # or a plotted series, else every KPI tile shows '2023' and charts grow a flat
    # year bar. Computed once here and excluded at every value-selection point.
    echo = B.parameter_echo_measures(decisions)
    # With no agent decision, recover an executive KPI/BAN tile (CY/PY measures +
    # date sparkline) so it renders as a stacked card instead of a wide dimension
    # dump. The kpiStack branch below returns before the table fallback.
    if not vd:
        kvd = _kpi_tile_decision(ws, mset)
        if kvd:
            vd = kvd
    # An explicit 'kpiStack' decision (the screenshot layer sees the big-number +
    # ▲% vs PY + sparkline tile and marks the worksheet as kpiStack) is enriched
    # with the CY/PY/%Diff measures and date grain the deterministic recovery
    # derives. A plain 'card' decision is ALSO upgraded when the worksheet has the
    # exact executive-KPI shape (CY/PY[/% Diff] measures over a date sparkline) so
    # every KPI tile renders like the Tableau dashboard -- big number + ▲% vs PY +
    # 12-month trend -- regardless of whether the agent (or a screenshot hint)
    # labelled it 'card' or 'kpiStack'. If the worksheet lacks that KPI shape an
    # explicit kpiStack degrades to a plain card so it still shows a single number.
    elif (vtype in ("kpiStack", "card") or vd.get("visualType") in ("kpiStack", "card")) \
            and not vd.get("kpiStack"):
        kvd = _kpi_tile_decision(ws, mset)
        if kvd:
            for k, v in vd.items():
                if k not in ("visualType", "reason", "worksheet"):
                    kvd[k] = v
            vd, vtype = kvd, "kpiStack"
        elif vtype == "kpiStack":
            vtype = "card"
    # Last-resort rescue: a worksheet with a plotted date axis + measure pills that
    # stayed ambiguous (no agent decision -> tableEx fallback) is a time trend, not
    # a flat table. Promote it to a line chart so the deterministic path shows the
    # trend (the multi-measure block in the cartesian branch keeps every series).
    # KPI/BAN tiles are already recovered above and excluded via vd.kpiStack.
    if vtype == "tableEx" and not vd.get("kpiStack") and _is_date_trend(ws, mset):
        vtype = "lineChart"
    # Per-measure Display-units divisor (1 / 1000 / 1_000_000 / …) so a measure
    # whose Tableau format scaled by millions renders as "$4,166.07M" instead of
    # the full unscaled number. Attached to each measure binding below.
    units = B.display_units_map(decisions, ir)

    def _with_units(b: Dict) -> Dict:
        if b.get("isMeasure"):
            b["displayUnits"] = units.get(b["prop"], 1)
            # A measure reference must name the table the measure is DEFINED on,
            # not the visual's fact entity. Naming the fact for a measure that
            # lives on a dim makes Power BI fail to resolve it -> broken visual.
            b["entity"] = B.measure_entity(b["prop"], decisions, b.get("entity"))
        return b

    # A worksheet with a measure value and NO category on any shelf is a KPI/BAN
    # card. The mark heuristic can mis-guess a chart here because the IR's
    # ``dimensions`` list also carries filter-only fields (a Tableau filter shelf
    # is not a plotted axis); trust the actual shelf (rows/cols/categoryField).
    if vtype in CARTESIAN and not vd.get("category") and _is_card_ws(ws):
        vtype = "card"

    # A Tableau Square-mark cross-tab (dimensions on BOTH rows and cols, coloured
    # by a measure) is a heat map -> Power BI matrix. The mark heuristic maps
    # Square -> treemap, whose single Group would otherwise fall back to a high-
    # cardinality key column and render as thousands of tiny tiles.
    if vtype == "treemap" and not vd.get("category") and _is_crosstab_ws(ws, cols):
        vtype = "matrix"

    # A Tableau text-table / cross-tab worksheet (a multi-level ROW hierarchy with
    # measure value columns -- e.g. Entity > Site > Level Of Care > Department x
    # Licensed, Staffed, Occupied, ...) is faithfully a Power BI MATRIX, not a flat
    # detail table nor a choropleth map. The flat ``tableEx`` fallback emits columns
    # in the IR's unordered ``dimensions`` order (threshold/helper columns first,
    # the real hierarchy last) and drops grouped levels; a Polygon/Filled-Map mark
    # mis-infers a map when the sheet carries no geographic field. Only fires for
    # the deterministic fallbacks (no agent visual decision) so agent-chosen charts
    # and genuine single-location maps are untouched, and only when the worksheet
    # has >=2 stacked row dimensions (a real hierarchy worth a matrix).
    if vtype in ("tableEx", "filledMap", "map") and not _agent_decided(ws_name, decisions) \
            and not vd.get("tableColumns") and not vd.get("kpiStack"):
        pivot = B.pivot_matrix_layout(ws, ir, entity, mset, cols, decisions)
        if pivot:
            vd = {**vd, "rows": pivot["rows"], "columns": pivot.get("columns"),
                  "values": pivot["values"]}
            vtype = "matrix"

    def value_bind() -> Dict:
        v = vd.get("value")
        if v and v in mset:
            return _with_units({"entity": entity, "prop": v, "isMeasure": True})
        # Map the worksheet's primary measure pill (agg+column) to the model
        # measure built from that same aggregation, so the correct measure is
        # plotted instead of a name-guess fallback (e.g. COUNT(loan_id) ->
        # 'Total Loans', not the first measure 'Total Funded Amount').
        mapped = B.measure_for_pill(ws, decisions, mset)
        if mapped:
            return _with_units({"entity": entity, "prop": mapped, "isMeasure": True})
        # No named model measure: when the worksheet's value is an aggregation of a
        # plain column (e.g. COUNTD(show_id) on a fact table that carries no
        # measures), emit that exact aggregation inline so the chart plots a real
        # number instead of binding the raw column (which renders empty).
        agg = B.pill_agg_binding(ws, entity, cols, decisions, ir)
        if agg:
            return agg
        return _with_units(B.value_binding(valf, entity, mset, mlist, cols, ir))

    # Caption-only worksheets (dynamic <caption>, no real shelves) -> textbox.
    caption = ws.get("caption") if ws else None
    if caption and not (ws.get("dimensions") or ws.get("values")):
        return P.textbox_visual(f"caption_{z}", pos, caption, size=10, bold=False,
                                hex_color="#666666")
    if caption and ws_name and "caption" in ws_name.lower():
        return P.textbox_visual(f"caption_{z}", pos, caption, size=10, bold=False,
                                hex_color="#666666")

    # Single-value card: measure value, else a text/dimension column value.
    if vtype == "card":
        # A KPI card's headline is the metric, never the year selector. The parser
        # often sets ``valueField`` to the first measure pill, which on a Tableau
        # KPI sheet is the 'Current Year' parameter echo -> the card would show
        # '2023'. Skip echo measures and pick the worksheet's real metric (CY
        # Sales / CY Profit / …) matching the sheet name.
        m = (vd.get("value") if vd.get("value") in mset else None) \
            or (valf if (valf in mset and valf not in echo) else None) \
            or B.kpi_value_measure(ws, ws_name, mset, echo, decisions)
        if not m:
            mp = B.measure_for_pill(ws, decisions, mset)
            m = mp if (mp and mp not in echo) else None
        if m:
            return P.card_visual(name, pos, B.measure_entity(m, decisions, entity),
                                 m, title=ws_name, theme=theme,
                                 display_units=units.get(m, 1))
        # No named model measure: if the worksheet dropped an implicit numeric
        # aggregation onto the card (e.g. AVG(int_rate)), reproduce that exact
        # aggregation inline rather than falling to a text card (which would show
        # Min of the value).
        pills = (ws or {}).get("measures") or []
        if pills:
            p0 = pills[0]
            pcol = p0.get("column") or p0.get("field")
            func = B.agg_func(p0.get("agg"))
            if pcol and pcol in cols and func is not None:
                pent = B.entity_for_field(pcol, entity, decisions, ir)
                return P.card_agg_visual(name, pos, pent, pcol, func,
                                         B.agg_label(p0.get("agg")),
                                         title=ws_name, theme=theme)
        col = vd.get("textColumn") or B.card_column(ws, ir, cols)
        if col and col in cols:
            return P.card_text_visual(name, pos, entity, col, title=ws_name, theme=theme)
        return None

    # Pie / donut: category from color encoding, value measure, branded slices.
    if vtype in ("pieChart", "donutChart"):
        cat = vd.get("category") or B.color_field(ws, ir, cols)
        cprop = cat or B.first_dim_col(ir)
        catbind = {"entity": B.entity_for_field(cprop, entity, decisions, ir), "prop": cprop}
        vb = value_bind()
        tips = _tooltip_binds(ws, vd, decisions, ir, entity, mset, cols,
                              vb.get("prop"), {cprop})
        pie = P.pie_visual(name, pos, catbind, vb, ws_name, theme=theme,
                            donut=(vtype == "donutChart"),
                            series_colors=vd.get("seriesColors"),
                            tooltips=tips or None)
        cfg = _topn_config(ws, catbind, vb, decisions, ir, mset)
        if cfg:
            pie["filterConfig"] = cfg
        return pie

    # Treemap: category area sized by a measure (Tableau color+size+text squares).
    if vtype == "treemap":
        # The partition is the text/detail DIMENSION, not the colour/size measure.
        # Prefer the worksheet's resolved category and shelf dimensions; only then
        # fall back to a colour-encoded field (which may itself be a measure).
        cat = (vd.get("category")
               or (ws.get("categoryField") if ws else None)
               or (_shelf_dims(ws, "rows", cols) + _shelf_dims(ws, "cols", cols)
                   or [None])[0]
               or B.color_field(ws, ir, cols)
               or B.first_dim_col(ir))
        catbind = {"entity": B.entity_for_field(cat, entity, decisions, ir), "prop": cat}
        vb = value_bind()
        tips = _tooltip_binds(ws, vd, decisions, ir, entity, mset, cols,
                              vb.get("prop"), {catbind["prop"]})
        tm = P.treemap_visual(name, pos, catbind, vb, ws_name, theme=theme,
                                series_colors=vd.get("seriesColors"),
                                single_color=vd.get("color"),
                                tooltips=tips or None)
        cfg = _topn_config(ws, catbind, vb, decisions, ir, mset)
        if cfg:
            tm["filterConfig"] = cfg
        return tm


    # Filled choropleth map: location column + measure-driven saturation.
    if vtype in ("filledMap", "map"):
        loc = vd.get("location") or B.geo_column(ir) or B.first_dim_col(ir)
        locbind = {"entity": B.entity_for_field(loc, entity, decisions, ir), "prop": loc}
        vb = value_bind()
        tips = _tooltip_binds(ws, vd, decisions, ir, entity, mset, cols,
                              vb.get("prop"), {loc})
        mp = P.map_visual(name, pos, locbind, vb, ws_name, theme=theme,
                            gradient=vd.get("gradient"), tooltips=tips or None)
        cfg = _topn_config(ws, locbind, vb, decisions, ir, mset)
        if cfg:
            mp["filterConfig"] = cfg
        return mp

    # Skip empty worksheets (no dims/values/caption) so they don't become cards.
    has_data = bool(ws and (ws.get("dimensions") or ws.get("values")))
    if not has_data and not vd.get("category"):
        return None

    # KPI stack: card (big number) + pct card (% diff) + sparkline
    if vd.get("kpiStack"):
        return _build_kpi_stack(name, pos, x, y, w, h, z, vd, entity, theme, units,
                                decisions)

    # Combo chart (Tableau dual-axis): bars for the primary measure(s) on Y, a
    # line for the secondary measure on Y2, over a shared category. Columns vs
    # line split follows explicit decisions when given, else first-as-bars /
    # last-as-line (the usual Tableau primary/secondary convention).
    if vtype in ("comboChart", "lineClusteredColumnComboChart"):
        if vd.get("category"):
            catbind = {"entity": B.entity_for_field(vd["category"], entity, decisions, ir),
                       "prop": vd["category"]}
        else:
            catbind = B.category_binding(ws, entity, cols, ir, decisions)
        col_vals = [v for v in (vd.get("columnValues") or []) if v in mset]
        line_vals = [v for v in (vd.get("lineValues") or []) if v in mset]
        if not (col_vals and line_vals):
            plotted = [v for v in (vd.get("values")
                                   or (ws.get("values") if ws else []) or [])
                       if v in mset]
            if len(plotted) >= 2:
                col_vals = plotted[:-1]
                line_vals = [plotted[-1]]
        if catbind and catbind.get("prop") and col_vals and line_vals:
            cvb = [_with_units({"entity": entity, "prop": v, "isMeasure": True})
                   for v in col_vals]
            lvb = [_with_units({"entity": entity, "prop": v, "isMeasure": True})
                   for v in line_vals]
            combo = P.combo_visual(name, pos, catbind, cvb, lvb, ws_name, theme=theme)
            cfg = _topn_config(ws, catbind, cvb[0], decisions, ir, mset)
            if cfg:
                combo["filterConfig"] = cfg
            return combo
        # Could not resolve a clean combo -> fall through to the table fallback.

    # Scatter (Tableau Circle mark with two measures): X and Y are the two plotted
    # measures, one point per detail dimension member. A 1-measure Circle is mapped
    # to a pie upstream, so a scatter here implies two measures; if two distinct
    # measures cannot be resolved, fall through to the table fallback.
    if vtype == "scatterChart":
        plotted: List[str] = []
        shelf = ((ws.get("values") or []) + (ws.get("rows") or [])
                 + (ws.get("cols") or [])) if ws else []
        for f in shelf:
            if f in mset and f not in echo and f not in plotted:
                plotted.append(f)
        xm = vd.get("xValue") if vd.get("xValue") in mset else None
        ym = vd.get("yValue") if vd.get("yValue") in mset else None
        if not (xm and ym) and len(plotted) >= 2:
            xm, ym = plotted[0], plotted[1]
        if xm and ym:
            xb = _with_units({"entity": entity, "prop": xm, "isMeasure": True})
            yb = _with_units({"entity": entity, "prop": ym, "isMeasure": True})
            if vd.get("category"):
                catbind = {"entity": B.entity_for_field(vd["category"], entity, decisions, ir),
                           "prop": vd["category"]}
            else:
                catbind = B.category_binding(ws, entity, cols, ir, decisions)
            sizem = vd.get("size") if vd.get("size") in mset else None
            sizeb = (_with_units({"entity": entity, "prop": sizem, "isMeasure": True})
                     if sizem else None)
            scatter = P.scatter_visual(
                name, pos, xb, yb, category=catbind, title=ws_name, theme=theme,
                size=sizeb,
                single_color=vd.get("color") or (theme or {}).get("markColor"))
            cfg = _topn_config(ws, catbind, yb, decisions, ir, mset)
            if cfg:
                scatter["filterConfig"] = cfg
            return scatter
        # Could not resolve two measures -> fall through to the table fallback.

    # Gantt (Tableau Gantt mark): a timeline of duration bars per task. Power BI
    # has no native Gantt, so render the faithful native equivalent — a horizontal
    # stacked bar with a transparent start offset and a visible duration segment.
    if vtype == "ganttChart":
        if vd.get("category"):
            catbind = {"entity": B.entity_for_field(vd["category"], entity, decisions, ir),
                       "prop": vd["category"]}
        else:
            catbind = B.category_binding(ws, entity, cols, ir, decisions)
        durb = value_bind()
        startm = vd.get("startValue") if vd.get("startValue") in mset else None
        startb = (_with_units({"entity": entity, "prop": startm, "isMeasure": True})
                  if startm else None)
        if catbind and catbind.get("prop") and durb:
            gantt = P.gantt_visual(name, pos, catbind, durb, start=startb,
                                   title=ws_name, theme=theme)
            cfg = _topn_config(ws, catbind, durb, decisions, ir, mset)
            if cfg:
                gantt["filterConfig"] = cfg
            return gantt
        # Could not resolve a clean Gantt -> fall through to the table fallback.

    if vtype in CARTESIAN:
        mapped = CHART_TYPE_MAP.get(vtype, vtype)
        # categoryIsMeasure: treat the category field as a Measure (for histograms)
        if vd.get("category") and vd.get("categoryIsMeasure"):
            catbind = {"entity": entity, "prop": vd["category"], "isMeasure": True}
        elif vd.get("category"):
            catbind = {"entity": B.entity_for_field(vd["category"], entity, decisions, ir),
                       "prop": vd["category"]}
        else:
            catbind = None  # resolved below
        valbind = value_bind()
        # A parameter-echo measure ('Current Year') is a constant year, not a
        # plottable metric; if the primary value resolved to one (the parser's
        # first-pill guess), swap it for the first real measure on the shelf so the
        # chart shows the metric instead of a flat year bar.
        if valbind.get("isMeasure") and valbind.get("prop") in echo:
            alt = next((v for v in ((ws.get("cols") or []) + (ws.get("rows") or [])
                                    + (ws.get("values") or []))
                        if v in mset and v not in echo), None)
            if alt:
                valbind = _with_units({"entity": entity, "prop": alt, "isMeasure": True})
        fp = B.field_param_for_ws(ws_name, decisions)
        if catbind is None:
            if fp is not None:
                catbind = {"entity": fp["name"], "prop": fp["name"]}
            else:
                catbind = B.category_binding(ws, entity, cols, ir, decisions)
        # Temporal-axis correction: when the resolved Y value is really a date/time
        # axis (MAX/MIN of a date column -- Tableau puts the continuous date pill on
        # the columns shelf of a trend chart), rebinding it onto Y both plots a
        # meaningless aggregated date AND crashes Power BI's cartesian renderer
        # (``categoryIdentities is not a function``). Move the date to the category
        # (X) axis and plot the worksheet's first real numeric measure on Y instead.
        if not vd.get("value") and _is_temporal_value(valbind, B.date_cols(ir), decisions):
            pills = (ws.get("measures") or []) if ws else []
            dcols = B.date_cols(ir)
            real_pill = next((p for p in pills
                              if (p.get("column") or p.get("field")) not in dcols
                              and B.agg_func(p.get("agg")) is not None), None)
            date_col = next(((p.get("column") or p.get("field")) for p in pills
                             if (p.get("column") or p.get("field")) in dcols), None)
            if real_pill and date_col:
                rp_ws = {**ws, "measures": [real_pill]}
                nm = B.measure_for_pill(rp_ws, decisions, mset)
                new_val = (_with_units({"entity": entity, "prop": nm, "isMeasure": True})
                           if nm else B.pill_agg_binding(rp_ws, entity, cols, decisions, ir))
                if new_val:
                    if not vd.get("category"):
                        catbind = {"entity": B.entity_for_field(date_col, entity,
                                                                decisions, ir),
                                   "prop": date_col}
                    valbind = new_val
        # The category axis field must never ALSO be plotted as a value. When the
        # parser aggregated a continuous axis (SUM(year)) the field became both the
        # category AND the worksheet's primary pill, so value_bind() can resolve to
        # that axis measure (e.g. 'Sum of year') and draw the year totals as a
        # spurious series. The temporal correction above handles the date case; this
        # is the fallback for any other aggregated-axis column. Swap it for the first
        # real shelf/value measure that does not merely aggregate the axis column.
        cat_axis = catbind.get("prop") if catbind else None
        if (cat_axis and valbind.get("isMeasure")
                and _measure_aggs_column(valbind.get("prop"), cat_axis, decisions)):
            alt = next((v for v in ((ws.get("rows") or []) + (ws.get("cols") or [])
                                    + (ws.get("values") or []))
                        if v in mset and v not in echo
                        and not _measure_aggs_column(v, cat_axis, decisions)), None)
            if alt:
                valbind = _with_units({"entity": entity, "prop": alt, "isMeasure": True})
        series = vd.get("series")
        seriesbind = ({"entity": B.entity_for_field(series, entity, decisions, ir),
                       "prop": series} if series else None)
        # No agent series decision: a Tableau colour pill on a dimension distinct
        # from the category is a series/legend split (e.g. an area chart coloured by
        # 'type' draws one area per Movie/TV Show). Recover it so the breakdown
        # survives instead of collapsing every member into one undifferentiated mark.
        if seriesbind is None:
            cser = B.series_from_color(ws, cols, catbind.get("prop") if catbind else None)
            if cser:
                series = cser
                seriesbind = {"entity": B.entity_for_field(cser, entity, decisions, ir),
                              "prop": cser}
        sort = None
        sd = vd.get("sort")
        if sd == "valueDesc":
            sort = P.measure_sort(entity, valbind["prop"]) if valbind.get("isMeasure") else None
        elif sd == "categoryAsc":
            sort = P.column_sort(catbind["entity"], catbind["prop"])
        # Secondary / additional measures (e.g. PY lines on KPI sparklines).
        # Parameter-echo measures ('Current Year') are constants, never a series.
        sec_v = vd.get("secondaryValue")
        sec_bind = _with_units({"entity": entity, "prop": sec_v, "isMeasure": True}) \
            if sec_v and sec_v in mset and sec_v not in echo else None
        add_binds = [_with_units({"entity": entity, "prop": av, "isMeasure": True})
                     for av in (vd.get("additionalValues") or []) if av in mset and av not in echo]
        # Tooltip fields: Tableau shows the mark-card measures on hover. Auto-add
        # the worksheet's extra measures (beyond the plotted Y) plus any explicit
        # decisions.tooltips, so the PBI hover matches Tableau's tooltip.
        # Tooltip fields: Tableau shows the mark-card measures AND dimensions on
        # hover. _tooltip_binds returns the extra measures (ws.values beyond the
        # plotted Y + decisions.tooltips) plus every marks-card dimension not on
        # the axis/series, so the PBI tooltip matches Tableau field-for-field.
        cat_prop = catbind.get("prop") if catbind else None
        ser_prop = series if series else None
        primary = valbind.get("prop")
        # Multi-measure chart with no agent decision: Tableau plots EVERY measure
        # pill on the rows/cols shelf as its own series (e.g. 'Weekly Trends' draws
        # both CY Sales and CY Profit). Add the shelf measures beyond the primary
        # so all series survive. The SHELF -- not the filter-inflated ``values``
        # list -- is the authoritative plotted set, and a single-series ``series``
        # encoding already covers the breakdown case, so skip it then.
        if not add_binds and not sec_bind and not series:
            shelf_meas, seen_m = [], set()
            for f in (ws.get("rows") or []) + (ws.get("cols") or []):
                if f in mset and f != primary and f not in seen_m and f not in echo:
                    seen_m.add(f)
                    shelf_meas.append(f)
            add_binds = [_with_units({"entity": entity, "prop": m, "isMeasure": True})
                         for m in shelf_meas]
        add_props = {b["prop"] for b in add_binds}
        tooltips = _tooltip_binds(ws, vd, decisions, ir, entity, mset, cols,
                                  primary, {cat_prop, ser_prop, *add_props})
        # Tableau nested row/column hierarchy (e.g. region > subregion > state)
        # becomes a drillable Power BI category axis: append the remaining shelf
        # dimensions below the primary category. Only when the category was
        # resolved from the shelf (no explicit override or field parameter).
        # A chart visual binds EXACTLY ONE category column. Tableau nests a drill
        # hierarchy (region > subregion > state) on a shelf, but the resolved
        # category is already the leaf grain those marks render at; stacking the
        # outer levels onto the Power BI axis adds extra grouping columns that
        # change the aggregation grain and break the numbers. Never append the
        # remaining shelf dimensions -- keep the single resolved category.
        extra_cats = None
        chart = P.chart_visual(name, pos, mapped, catbind, valbind, ws_name, theme=theme,
                              series=seriesbind, series_colors=vd.get("seriesColors"),
                              single_color=vd.get("color") or (theme or {}).get("markColor"),
                              sort=sort,
                              secondary_value=sec_bind, additional_values=add_binds or None,
                              hide_value_axis=bool(vd.get("hideValueAxis")),
                              hide_labels=bool(vd.get("hideLabels")),
                              extra_categories=extra_cats,
                              tooltips=tooltips or None)
        cfg = _topn_config(ws, catbind, valbind, decisions, ir, mset)
        if cfg:
            chart["filterConfig"] = cfg
        return chart
    # Matrix (pivotTable): Tableau cross-tab / highlight table with row + column
    # dimensions and measure cells. Resolves each field to its owning entity.
    if vtype in ("matrix", "pivotTable"):
        def _bind(spec):
            if isinstance(spec, dict):
                ent = spec.get("entity") or B._field_entity(spec.get("prop"), decisions, ir)
                out = {"entity": ent, "prop": spec["prop"],
                       "isMeasure": spec.get("isMeasure", spec.get("prop") in mset)}
                for k in ("agg", "aggLabel", "displayName"):
                    if spec.get(k) is not None:
                        out[k] = spec[k]
                return out
            ent = entity if spec in mset else B._field_entity(spec, decisions, ir)
            return {"entity": ent, "prop": spec, "isMeasure": spec in mset}
        # Deterministic fallback when no agent decision: rows/cols from the
        # worksheet shelf, value from the measure pill mapped to its model measure.
        # A parameter-echo measure ('Current Year') is a constant year, not a
        # cell value, so it is never used as the matrix measure.
        mapped_m = B.measure_for_pill(ws, decisions, mset)
        if mapped_m in echo:
            mapped_m = None
        det_vals = [mapped_m] if mapped_m else [v for v in (ws.get("values") or [])
                                                if ws and v in mset and v not in echo]
        rows = [_bind(r) for r in (vd.get("rows") or _shelf_dims(ws, "rows", cols))]
        cols_m = [_bind(c) for c in (vd.get("columns") or _shelf_dims(ws, "cols", cols))]
        vals = [_bind(v) for v in (vd.get("values") or det_vals)]
        if rows and vals:
            mtx = P.matrix_visual(name, pos, rows, cols_m or None, vals, ws_name, theme=theme)
            # A Top-N ranked cross-tab limits its row dimension to the N members,
            # ranked the same way as the chart/table paths (the row dims are the
            # candidate category columns for the Top-N field).
            cfg = _topn_table_config(ws, rows, entity, decisions, ir, mset)
            if cfg:
                mtx["filterConfig"] = cfg
            return mtx

    # A Tableau text-mark worksheet that just lists the distinct values of one
    # low-cardinality dimension (Rating, Duration) is faithfully an interactive
    # slicer in Power BI, not a static value-list table. Only fires for the
    # ambiguous tableEx fallback (no agent decision, no explicit tableColumns) so
    # real cross-tabs / detail tables are untouched; high-cardinality lists
    # (Genre, Description) stay tables because slicer_dimension caps cardinality.
    if vtype == "tableEx" and not vd.get("tableColumns"):
        sdim = B.slicer_dimension(ws, ir, cols)
        if sdim:
            sl_theme = _ws_theme(theme, ws)
            sl_ent = B.entity_for_field(sdim, entity, decisions, ir)
            sl_title = _col_caption(ir, sdim) or ws_name
            return P.slicer_visual(f"slicer_{B.slug(sdim)}_{z}", pos, sl_ent, sdim,
                                   sl_title, mode="Dropdown", theme=sl_theme)

    # tableColumns override from decisions.json (for Top N / custom column sets)
    if vd.get("tableColumns"):
        tcols = [{"entity": tc["entity"], "prop": tc["prop"],
                  "isMeasure": tc.get("isMeasure", False)}
                 for tc in vd["tableColumns"]]
    else:
        tcols = B.table_columns(ws, ir, entity, mset, cols, decisions)
    tbl = P.table_visual(name, pos, tcols, ws_name, theme=theme)
    # A Top-N ranked detail table ('Top 10 Customers') keeps only its N rows: apply
    # the same Power BI Top-N filter on the ranked field as the chart path does.
    cfg = _topn_table_config(ws, tcols, entity, decisions, ir, mset)
    if cfg:
        tbl["filterConfig"] = cfg
    return tbl


def _hex_id(*parts: str) -> str:
    r"""Deterministic 20-char hex token (matches the bookmark ^[\w-]+$ rule)."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _resolve_nav_target(button: Dict, page_map: Dict[str, str],
                        current_page: str) -> Optional[str]:
    """Resolve a goto-sheet button to a target page name.

    The Tableau window-id GUID is not reliably mapped to a dashboard in the .twb,
    so the button label ("Go to Sales Dashboard") is the authoritative signal.
    Strip a leading "go to"/"goto" and match the remainder against known page
    names; fall back to the only other page when there are exactly two.
    """
    label = (button.get("label") or button.get("tooltip") or "").strip()
    key = re.sub(r"^\s*go\s*to\s*", "", label, flags=re.I).strip()
    if key:
        san = sanitize(key)
        for pg in page_map.values():
            if pg.lower() == san.lower() or san.lower() in pg.lower() or pg.lower() in san.lower():
                return pg
    others = [p for p in page_map.values() if p != current_page]
    return others[0] if len(others) == 1 else None


def build_page(dashboard: Dict, ir: Dict, decisions: Dict, pages_dir: str,
               page_map: Optional[Dict[str, str]] = None,
               bookmark_sink: Optional[List[Dict]] = None) -> str:
    """Write one page folder (page.json + visuals/*) and return its page name.

    ``bookmark_sink``, when given, collects the report-level .bookmark.json dicts
    generated for Tableau show/hide toggle buttons on this page (the caller writes
    them under definition/bookmarks/).
    """
    page_map = page_map or {}
    page_name = sanitize(dashboard["name"])
    page_dir = os.path.join(pages_dir, page_name)
    pw, ph = dashboard["size"]["w"], dashboard["size"]["h"]
    theme = decisions.get("theme") or {}
    write_json(os.path.join(page_dir, "page.json"),
               P.page_json(page_name, dashboard["name"], pw, ph,
                           background=theme.get("pageBackground"),
                           outspace=theme.get("outspace")))
    sx, sy = pw / COORD_SPACE, ph / COORD_SPACE
    rail = _rail_left_tableau(dashboard)
    # Content (non-rail) zones are compressed into the area LEFT of the floating
    # filter rail so they never slide underneath it; rail zones keep their real
    # right-hand position. With no rail, content spans the full canvas width.
    content_sx = ((round(rail * sx) - RAIL_GAP) / COORD_SPACE) if rail else sx

    def _geom(zone: Dict):
        """Pre-scaled, rail-aware, on-canvas-clamped pixel rect for a zone."""
        in_rail = rail is not None and abs(zone.get("x", 0) - rail) <= RAIL_X_TOL
        zsx = sx if in_rail else content_sx
        raw_h = zone.get("h", 0) * sy
        gx = round(zone.get("x", 0) * zsx)
        gy = round(zone.get("y", 0) * sy)
        gw = max(round(zone.get("w", 0) * zsx), 80)
        gh = max(round(raw_h), 40)
        # Clamp fully on-canvas so nothing renders off the page edge.
        gx = max(0, min(gx, pw - 1))
        gy = max(0, min(gy, ph - 1))
        gw = min(gw, pw - gx)
        gh = min(gh, ph - gy)
        return (gx, gy, gw, gh, raw_h)

    # Tableau show/hide toggle groups: several worksheets stacked in the same spot
    # whose visibility is driven by a parameter. In Power BI we overlay all members
    # at the active member's rect and filter each by a flag measure so the slicer
    # selection reveals exactly one. Member worksheets are emitted here, not in the
    # normal zone loop (skipped below) and never suppressed as collapsed slivers.
    overlays = decisions.get("visualOverlays", [])
    overlay_members = {m["worksheet"] for ov in overlays for m in ov.get("members", [])}
    zone_by_ws = {zn.get("worksheet"): zn for zn in dashboard.get("zones", [])}
    entity = primary_entity(decisions)

    # zone id -> the visual name(s) generated for it, so a Tableau toggle button
    # can bind a bookmark that hides/shows exactly the visuals in its container.
    zone_visuals: Dict[str, List[str]] = {}

    z = 100
    for zone in dashboard.get("zones", []):
        if zone.get("type") == "viz" and zone.get("worksheet") in overlay_members:
            continue  # emitted by the overlay pass below
        result = build_visual(zone, ir, decisions, z, _geom(zone))
        if result is None:
            continue
        # kpiStack returns a list of 3 visuals: [card, pct_card, sparkline]
        visuals = result if isinstance(result, list) else [result]
        for visual in visuals:
            write_json(os.path.join(page_dir, "visuals", visual["name"], "visual.json"), visual)
            if zone.get("id") is not None:
                zone_visuals.setdefault(str(zone["id"]), []).append(visual["name"])
            z += 1

    for ov in overlays:
        pos_zone = zone_by_ws.get(ov.get("positionWorksheet"))
        if pos_zone is None:
            continue
        for member in ov.get("members", []):
            synth = dict(pos_zone, worksheet=member["worksheet"], type="viz")
            result = build_visual(synth, ir, decisions, z, _geom(synth))
            if result is None:
                continue
            visuals = result if isinstance(result, list) else [result]
            for visual in visuals:
                visual["filterConfig"] = P.measure_filter_config(entity, member["filterMeasure"])
                write_json(os.path.join(page_dir, "visuals", visual["name"], "visual.json"), visual)
                z += 1

    # Navigation buttons: Tableau goto-sheet buttons -> Power BI actionButton with
    # page navigation. Toggle (show/hide) buttons -> a bookmark pair + two stacked
    # bookmark buttons (handled in the toggle loop below).
    # The auto-derived theme can carry a transparent mark colour ('#00000000'),
    # which would paint the buttons as invisible all-white tiles; opaque_color
    # guarantees a visible solid fill regardless of the source theme.
    btn_fill = P.opaque_color(theme.get("markColor") or theme.get("outspace"), "#004263")
    bz = 1000
    for button in dashboard.get("buttons", []):
        if button.get("action") != "goto-sheet":
            continue
        gx, gy, gw, gh, _ = _geom(button)
        target = _resolve_nav_target(button, page_map, page_name)
        bname = "nav_" + sanitize((button.get("label") or f"button_{bz}")) + f"_{bz}"
        visual = P.nav_button_visual(
            bname, P.position(gx, gy, gw, gh, bz),
            button.get("label"), target, fill=btn_fill)
        write_json(os.path.join(page_dir, "visuals", visual["name"], "visual.json"), visual)
        bz += 1

    # Toggle (show/hide) buttons: reproduce the Tableau filter-drawer toggle with
    # an auto-generated Show/Hide bookmark pair plus two stacked bookmark buttons.
    # A Power BI bookmark button applies ONE bookmark, so a Show button (reveals
    # the drawer) and a Hide button (collapses it) swap places as the user toggles.
    for button in dashboard.get("buttons", []):
        if button.get("action") != "toggle":
            continue
        drawer = [n for zid in (button.get("targetZoneIds") or [])
                  for n in zone_visuals.get(str(zid), [])]
        if not drawer:
            continue  # nothing resolved to hide -> a dead button would mislead
        gx, gy, gw, gh, _ = _geom(button)
        label = (button.get("label") or "Filters").strip()
        # Strip a leading Show/Hide/Close verb so we can phrase both directions.
        topic = re.sub(r"^\s*(show|hide|close|open|toggle)\s*", "", label, flags=re.I).strip() or "Filters"
        tag = sanitize(label) or "toggle"
        show_id = _hex_id(page_name, tag, "show")
        hide_id = _hex_id(page_name, tag, "hide")
        show_btn = f"toggle_show_{tag}_{bz}"
        hide_btn = f"toggle_hide_{tag}_{bz + 1}"
        targets = drawer + [show_btn, hide_btn]
        # Show state: drawer + Hide button visible -> hide only the Show button.
        # Hide state: drawer + Hide button hidden -> the Show button reappears.
        if bookmark_sink is not None:
            bookmark_sink.append(P.bookmark_definition(
                show_id, f"Show {topic}", page_name, targets, [show_btn]))
            bookmark_sink.append(P.bookmark_definition(
                hide_id, f"Hide {topic}", page_name, targets, drawer + [hide_btn]))
        pos = P.position(gx, gy, gw, gh, bz)
        sb = P.bookmark_button_visual(show_btn, pos, f"Show {topic}", show_id,
                                      fill=btn_fill, icon="Filter")
        hb = P.bookmark_button_visual(
            hide_btn, P.position(gx, gy, gw, gh, bz + 1),
            label, hide_id, fill=btn_fill, icon="Filter")
        write_json(os.path.join(page_dir, "visuals", sb["name"], "visual.json"), sb)
        write_json(os.path.join(page_dir, "visuals", hb["name"], "visual.json"), hb)
        bz += 2
    return page_name


def emit(ir: Dict, decisions: Dict, analysis_path: str) -> str:
    model_name = decisions.get("modelName") or ir["workbook"]["pascalName"]
    # Adopt the dashboard's own colour scheme (dark page + branded marks) as the
    # baseline theme when decisions.json does not specify one, so the page is not
    # left on the default white canvas. Explicit decisions.theme keys still win.
    decisions = dict(decisions)
    decisions["theme"] = {**_derive_theme(ir), **(decisions.get("theme") or {})}
    base = os.path.dirname(os.path.abspath(analysis_path))
    report_dir = os.path.join(base, f"{model_name}.Report")
    # Wipe any prior report so stale/orphan visual folders never accumulate.
    if os.path.isdir(report_dir):
        _rmtree_robust(report_dir)
    defin = os.path.join(report_dir, "definition")
    pages_dir = os.path.join(defin, "pages")
    # Clear any pages from a previous run so renamed/renumbered visual folders do
    # not linger as orphans (e.g. an unfiltered toggle visual left after enabling
    # an overlay group). Regeneration must be deterministic.
    if os.path.isdir(pages_dir):
        shutil.rmtree(pages_dir)
    os.makedirs(pages_dir, exist_ok=True)

    pbir = dict(PBIR, datasetReference={"byPath": {"path": f"../{model_name}.SemanticModel"}})
    write_json(os.path.join(report_dir, "definition.pbir"), pbir)
    write_json(os.path.join(defin, "report.json"), REPORT)
    write_json(os.path.join(defin, "version.json"), VERSION)
    write_json(os.path.join(report_dir, ".platform"), {
        "$schema": PLATFORM_SCHEMA,
        "metadata": {"type": "Report", "displayName": model_name},
        "config": {"version": "2.0", "logicalId": _logical_id(model_name + ".rpt")},
    })

    # Pre-compute the dashboard-name -> page-name map so navigation buttons can
    # resolve their cross-page targets (a button on page A links to page B).
    page_map = {d.get("name", ""): sanitize(d.get("name", ""))
                for d in ir.get("dashboards", [])}
    page_names: List[str] = []
    bookmarks: List[Dict] = []
    for dashboard in ir.get("dashboards", []):
        page_names.append(build_page(dashboard, ir, decisions, pages_dir,
                                     page_map, bookmarks))
    if not page_names:
        page_names = ["Page1"]
        write_json(os.path.join(pages_dir, "Page1", "page.json"),
                   P.page_json("Page1", "Page 1", 1280, 720))
    write_json(os.path.join(pages_dir, "pages.json"),
               P.pages_json(page_names, page_names[0]))
    # Report-level bookmarks for Tableau show/hide toggle buttons.
    if bookmarks:
        bm_dir = os.path.join(defin, "bookmarks")
        for bm in bookmarks:
            write_json(os.path.join(bm_dir, f"{bm['name']}.bookmark.json"), bm)
        write_json(os.path.join(bm_dir, "bookmarks.json"),
                   P.bookmarks_metadata([bm["name"] for bm in bookmarks]))
    return report_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Emit PBIR report from IR + decisions.")
    parser.add_argument("analysis", help="path to analysis.json")
    parser.add_argument("--decisions", required=True, help="path to decisions.json")
    args = parser.parse_args(argv)
    for p in (args.analysis, args.decisions):
        if not os.path.isfile(p):
            print(f"ERROR: file not found: {p}", file=sys.stderr)
            return 2
    ir = load_json(args.analysis)
    report_dir = emit(ir, load_json(args.decisions), args.analysis)
    print(f"Wrote report: {report_dir}\n  pages: {len(ir.get('dashboards', [])) or 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
