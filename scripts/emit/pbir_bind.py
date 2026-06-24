"""pbir_bind.py — IR-aware field binding resolution for the PBIR emitter.

Given the IR (analysis.json) and the LLM decisions.json, resolve which entity +
property each dashboard zone should bind to. Keeps emit_pbir.py thin: charts use
the worksheet's resolved category/value fields, slicers map to real columns or
disconnected parameter tables, and measures bind as measures (not columns).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import date_levels as D  # noqa: E402
import field_param as FP  # noqa: E402
import emit_tmdl as ET  # noqa: E402  (reuse the same CSV probe the model emitter uses)
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "twb"))
import csv_probe as CP  # noqa: E402  (distinct-value counts for slicer-vs-table)


# A Tableau text-mark worksheet that lists the distinct values of ONE dimension
# (Rating, Duration) is faithfully an interactive slicer in Power BI, not a
# static value-list table. Above this distinct-value count a slicer is unusable
# (Genre ~461, Description ~6200), so the worksheet stays a table instead.
SLICER_MAX_CARDINALITY = 300



# A Tableau federated (multi-CSV) join disambiguates a column that exists in two
# joined files by suffixing the file name, e.g. ``state (state_region.csv)`` is
# the ``state`` column from state_region.csv. When that join is split into a
# star schema the column lives in its dim under its plain name (``state``), so
# the report binder must strip this suffix to resolve the owning table and emit
# a property name that actually exists on the model table.
_ALIAS_SUFFIX = re.compile(r"\s*\([^()]*\.(?:csv|xlsx?|txt)\)\s*$", re.IGNORECASE)


def base_field(name: Optional[str]) -> Optional[str]:
    """Strip a Tableau federated-join file alias suffix from a column name.

    ``state (state_region.csv)`` -> ``state``. Names without the suffix pass
    through unchanged (and None passes through as None).
    """
    if not name:
        return name
    return _ALIAS_SUFFIX.sub("", name).strip() or name


def strip_alias_in_ws(ws: Optional[Dict]) -> Optional[Dict]:
    """Return a shallow copy of a worksheet with federated-join file aliases
    stripped from every field-name-bearing key, so downstream bindings resolve
    to the dim that owns the plain column and emit a valid property name."""
    if not ws:
        return ws
    out = dict(ws)
    for key in ("categoryField", "valueField"):
        if out.get(key):
            out[key] = base_field(out[key])
    for key in ("rows", "cols", "dimensions", "values", "filters"):
        if isinstance(out.get(key), list):
            out[key] = [base_field(f) if isinstance(f, str) else f for f in out[key]]
    for key in ("tooltipFields", "filterDetails"):
        if isinstance(out.get(key), list):
            out[key] = [
                {**f, "field": base_field(f.get("field"))}
                if isinstance(f, dict) and f.get("field") else f
                for f in out[key]
            ]
    if isinstance(out.get("axes"), dict):
        ax = dict(out["axes"])
        if ax.get("category"):
            ax["category"] = base_field(ax["category"])
        if isinstance(ax.get("values"), list):
            ax["values"] = [base_field(v) if isinstance(v, str) else v
                            for v in ax["values"]]
        out["axes"] = ax
    if isinstance(out.get("topN"), dict) and out["topN"].get("field"):
        out["topN"] = {**out["topN"], "field": base_field(out["topN"]["field"])}
    return out


def fact_entity(decisions: Dict) -> str:
    for t in decisions.get("tables", []):
        if t.get("role") == "fact":
            return t["name"]
    tables = decisions.get("tables", [])
    return tables[0]["name"] if tables else "Table"


def measure_list(decisions: Dict) -> List[str]:
    return [m["name"] for m in decisions.get("measures", [])]


def display_units_map(decisions: Dict, ir: Dict) -> Dict[str, int]:
    """Measure name -> Power BI Display-units divisor (1 / 1000 / 1_000_000 / …)
    derived from each measure's Tableau number format. Reuses the model emitter so
    the report's ``labelDisplayUnits`` matches the model formatString exactly."""
    return ET.measure_display_units(decisions.get("measures", []), ir)


# Tableau aggregation -> the DAX function its deterministic measure is built from.
_PILL_AGG_DAX = {
    "SUM": "SUM", "COUNT": "COUNT", "COUNTD": "DISTINCTCOUNT",
    "AVG": "AVERAGE", "AVERAGE": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
    "MEDIAN": "MEDIAN",
}

# Tableau aggregation -> Power BI QueryAggregateFunction enum value, for inline
# visual-query aggregations (used when a pill has no named model measure, e.g. an
# implicit AVG(int_rate) dropped on a card, or COUNTD(show_id) plotted on a chart
# whose fact table carries no measures). DistinctCount is 2 and Min is 3 (see
# card_text_visual / _BY_MEASURE_AGG). MEDIAN has no single inline function.
_PILL_AGG_FUNC = {"SUM": 0, "AVG": 1, "AVERAGE": 1, "COUNTD": 2, "DISTINCTCOUNT": 2,
                  "MIN": 3, "MAX": 4, "COUNT": 5}
_AGG_FUNC_LABEL = {0: "Sum", 1: "Average", 2: "Count", 3: "Min", 4: "Max", 5: "Count"}


def agg_func(agg: Optional[str]) -> Optional[int]:
    """Power BI QueryAggregateFunction enum value for a Tableau aggregation, or
    None when the aggregation has no single inline function (COUNTD, MEDIAN)."""
    return _PILL_AGG_FUNC.get((agg or "").upper())


def agg_label(agg: Optional[str]) -> Optional[str]:
    """Human label (Sum/Average/Min/Max/Count) for a Tableau aggregation."""
    f = agg_func(agg)
    return _AGG_FUNC_LABEL.get(f) if f is not None else None


def measure_for_pill(ws: Optional[Dict], decisions: Dict, mset: Set[str]) -> Optional[str]:
    """Map a worksheet's primary measure pill (agg + column) to the model measure
    whose DAX is exactly that aggregation over that column.

    A Tableau chart/card value is an aggregation like ``COUNT([loan_id])``. The
    deterministic translator turned that same calc into a model measure (e.g.
    ``Total Loans`` = ``COUNT(loan[loan_id])``). Binding by name alone fails — the
    measure is ``Total Loans``, not ``loan_id`` — so without this the emitter falls
    back to the first measure and plots the wrong number. Matching on the DAX
    aggregation pattern recovers the correct measure deterministically.
    """
    if not ws:
        return None
    pills = ws.get("measures") or []
    if not pills:
        return None
    p = pills[0]
    fn = _PILL_AGG_DAX.get((p.get("agg") or "").upper())
    column = p.get("column") or p.get("field")
    if not fn or not column:
        return None
    pat = re.compile(
        rf"\b{fn}\s*\(\s*[^()\[\]]*\[\s*{re.escape(column)}\s*\]\s*\)", re.I)
    for m in decisions.get("measures", []):
        if m.get("name") in mset and pat.search(m.get("dax") or ""):
            return m["name"]
    return None


def pill_agg_binding(ws: Optional[Dict], entity: str, cols: Set[str],
                     decisions: Optional[Dict] = None,
                     ir: Optional[Dict] = None) -> Optional[Dict]:
    """Inline-aggregation value binding from a worksheet's primary measure pill.

    Used when a chart plots an aggregation like ``COUNTD([show_id])`` but the model
    carries no named measure to bind (e.g. a fact table with zero measures). Without
    this the emitter falls back to the raw column, so Power BI plots a text/ID column
    with no aggregation and the chart renders empty. Returns a binding dict carrying
    the QueryAggregateFunction code so ``binding_projection`` emits an inline
    Aggregation, or None when the pill has no inline-expressible aggregation.
    """
    if not ws:
        return None
    pills = ws.get("measures") or []
    if not pills:
        return None
    p = pills[0]
    col = p.get("column") or p.get("field")
    func = agg_func(p.get("agg"))
    if not col or col not in cols or func is None:
        return None
    ent = entity_for_field(col, entity, decisions, ir) if decisions else entity
    return {"entity": ent, "prop": col, "agg": func,
            "aggLabel": agg_label(p.get("agg")) or "Count"}


def column_names(ir: Dict) -> Set[str]:
    return {c["name"] for c in ir.get("columns", [])}


def param_tables(decisions: Dict) -> Set[str]:
    return {t["name"] for t in decisions.get("tables", []) if t.get("role") == "param"}


def first_date_col(ir: Dict) -> Optional[str]:
    for c in ir.get("columns", []):
        if c.get("dataType") in ("date", "datetime"):
            return c["name"]
    return None


def date_cols(ir: Dict) -> Set[str]:
    return {c["name"] for c in ir.get("columns", [])
            if c.get("dataType") in ("date", "datetime")}


def part_prop(field: Optional[str], level: Optional[str], dcols: Set[str]) -> Optional[str]:
    """Map a date field + Tableau level to its derived part column (or itself)."""
    if field and field in dcols and D.needs_part(level):
        return D.part_column_name(field, level)
    return field


def first_dim_col(ir: Dict) -> Optional[str]:
    for c in ir.get("columns", []):
        if c.get("dataType") in ("string", "date", "datetime"):
            return c["name"]
    return None


def ws_by_name(ir: Dict, name: str) -> Optional[Dict]:
    for ws in ir.get("worksheets", []):
        if ws["name"] == name:
            return ws
    return None


def decode_field(enc: Optional[str]) -> Optional[str]:
    """Extract the column name from a Tableau encoding ref.

    Examples: 'federated...].[none:rating:nk' -> 'rating',
    '...].[ctd:show_id:qk' -> 'show_id', '...].[yr:Calc_123:ok' -> 'Calc_123'.
    """
    if not enc:
        return None
    m = re.search(r"\]\.\[(.+)$", enc)
    body = m.group(1) if m else enc
    parts = body.split(":")
    if len(parts) >= 3:
        return parts[1]
    if len(parts) == 2:
        return parts[0]
    return parts[0] if parts and parts[0] else None


def card_column(ws: Optional[Dict], ir: Dict, cols: Set[str]) -> Optional[str]:
    """Resolve the text/dimension column a single-value card displays."""
    if not ws:
        return None
    enc = ws.get("encodings") or {}
    for key in ("text", "color"):
        c = decode_field(enc.get(key))
        if c and c in cols:
            return c
    for d in ws.get("dimensions") or []:
        if d in cols:
            return d
    return None


def geo_column(ir: Dict) -> Optional[str]:
    """First column whose name reads as a geographic location."""
    for c in ir.get("columns", []):
        if re.search(r"\b(country|state|province|city|region)\b", c["name"], re.I):
            return c["name"]
    return None


def color_field(ws: Optional[Dict], ir: Dict, cols: Set[str]) -> Optional[str]:
    """The category a pie/series split should use (Tableau color/text encoding)."""
    enc = (ws.get("encodings") or {}) if ws else {}
    c = decode_field(enc.get("color")) or decode_field(enc.get("text"))
    if c and c in cols:
        return c
    return first_dim_col(ir)


def series_from_color(ws: Optional[Dict], cols: Set[str],
                      category_prop: Optional[str]) -> Optional[str]:
    """The chart series/legend a Tableau colour encoding implies, or None.

    A Tableau colour pill on a DIMENSION splits the marks into one series per member
    (e.g. an area chart coloured by ``type`` draws a Movie area and a TV Show area).
    Returns that dimension column so the emitter can add a Series projection, but
    only when it is a real column, sits on the worksheet's dimension shelf, and is
    not already the plotted category (colour == category is a single-series recolour,
    not a breakdown).
    """
    if not ws:
        return None
    c = decode_field((ws.get("encodings") or {}).get("color"))
    if not c or c not in cols:
        return None
    if category_prop and c == category_prop:
        return None
    if c not in (ws.get("dimensions") or []):
        return None
    return c


def slug(text: str) -> str:
    return (re.sub(r"[^\w]+", "", (text or "").replace(" ", "")) or "f").lower()


def field_param_for_ws(ws_name: str, decisions: Dict) -> Optional[Dict]:
    """Field parameter whose PRIMARY (live) worksheet is ws_name, if any."""
    for fp in decisions.get("fieldParameters", []):
        if FP.primary_worksheet(fp) == ws_name:
            return fp
    return None


def field_param_by_field(field: Optional[str], decisions: Dict) -> Optional[Dict]:
    """Field parameter matching a paramctrl zone field (by name slug)."""
    if not field:
        return None
    for fp in decisions.get("fieldParameters", []):
        if slug(fp["name"]) == slug(field):
            return fp
    return None


def suppressed_worksheets(decisions: Dict) -> set:
    return FP.suppressed_worksheets(decisions.get("fieldParameters", []))


def _owned_columns(table: Dict, ir: Dict) -> Set[str]:
    """Logical column names a dim/date table actually owns.

    A synthetic table (calendar / datatable, ``sourceDatasource`` is None) owns
    only its declared key/datatable columns — without this guard a calendar
    DimDate would claim every IR column and mis-bind their slicers.

    A real source-backed dim is normally scoped by its datasource, but a
    *federated multi-CSV* datasource exposes every CSV's columns under ONE
    datasource name (e.g. "Sales DataSource" spanning Orders/Customers/Location/
    Products). In that case the dim's ``sourceFile`` is probed so each dim claims
    only its own CSV's columns, matching exactly what emit_tmdl declares.
    """
    src = table.get("sourceDatasource")
    if src is None:
        own = set(table.get("keyColumns", []))
        dt = table.get("datatable") or {}
        own |= {c["name"] for c in dt.get("columns", [])}
        return own
    if table.get("sourceFile"):
        probe = ET._probe_for_table(table, ir)
        if probe and probe.get("columns"):
            return {c["name"] for c in probe["columns"]}
    return {c["name"] for c in ir.get("columns", []) if c.get("datasource") == src}


def entity_for_field(field: Optional[str], default_entity: str,
                     decisions: Dict, ir: Dict) -> str:
    """Resolve a category/table column field to its owning dim/date table.

    Falls back to ``default_entity`` (typically the fact) when the field is not
    owned by any dimension — e.g. fact columns or date-part derived columns.
    """
    if not field:
        return default_entity
    field = base_field(field)
    for table in decisions.get("tables", []):
        if table.get("role") in ("dim", "date") and field in _owned_columns(table, ir):
            return table["name"]
    return default_entity


def _field_entity(field: Optional[str], decisions: Dict, ir: Dict) -> str:
    """Return the correct entity (table name) for a slicer field.

    Looks up the field in dim tables first; falls back to the fact entity.
    This ensures Category/Sub-Category → DimProduct, Region/State/City → DimLocation, etc.
    """
    if not field:
        return fact_entity(decisions)
    field = base_field(field)
    # Check each dim/date table's REAL columns (physical CSV header), so a field
    # only resolves to a dim that actually contains it; otherwise fall through to
    # the fact entity.
    for table in decisions.get("tables", []):
        if table.get("role") in ("dim", "date"):
            if field in _owned_columns(table, ir):
                return table["name"]
    # Check param tables
    for t in decisions.get("tables", []):
        if t.get("role") == "param" and t["name"] == field:
            return field
    return fact_entity(decisions)


def resolve_slicer(zone: Dict, ir: Dict, decisions: Dict):
    """Return (entity, prop, title, mode) for a filter / parameter-control zone.

    mode is 'Between' for date-range parameters (Start/End Date) so each acts as
    an independent bound on the date column, else 'Dropdown' for list slicers.
    """
    field = zone.get("field")
    cols = column_names(ir)
    ptables = param_tables(decisions)
    title = field or zone.get("worksheet") or "Filter"
    # Date-range parameter (e.g. 'Start Date' / 'End Date') -> Between slicer on
    # the fact date column, so Start and End are independent bounds.
    if zone.get("type") == "paramctrl" and _is_date_param(field, decisions):
        entity = fact_entity(decisions)
        dcol = first_date_col(ir)
        if dcol:
            return entity, dcol, title, "Between"
    # Parameter list-slicers bind to a disconnected table (column shares name).
    pmap = {slug(t): t for t in ptables}
    if field and slug(field) in pmap:
        tbl = pmap[slug(field)]
        return tbl, tbl, title, "Dropdown"
    # Resolve the correct entity (dim table or fact) for this field
    entity = _field_entity(field, decisions, ir)
    if field and field in cols:
        return entity, field, title, "Dropdown"
    if zone.get("type") == "paramctrl":
        dcol = first_date_col(ir)
        if dcol:
            return entity, dcol, title, "Dropdown"
    fallback = field if field in cols else (next(iter(cols)) if cols else "Column")
    return entity, fallback, title, "Dropdown"


def _is_date_param(field: Optional[str], decisions: Dict) -> bool:
    """A range/date parameter that is NOT backed by a disconnected list table."""
    if not field:
        return False
    names = {t["name"] for t in decisions.get("tables", []) if t.get("role") == "param"}
    if field in names or slug(field) in {slug(n) for n in names}:
        return False  # it's a list parameter -> dropdown
    return bool(re.search(r"\bdate\b", field, re.IGNORECASE))


def category_binding(ws: Optional[Dict], entity: str, cols: Set[str],
                     ir: Dict, decisions: Optional[Dict] = None) -> Dict:
    """Resolve a chart category, aggregating dates to the Tableau date level."""
    catf = ws.get("categoryField") if ws else None
    level = ws.get("categoryDateLevel") if ws else None
    dcols = date_cols(ir)
    prop = part_prop(catf, level, dcols)

    def _ent(p: str) -> str:
        return entity_for_field(p, entity, decisions, ir) if decisions else entity

    if prop and (catf in dcols):
        # date-part columns live on the fact table that owns the base date column
        return {"entity": entity, "prop": prop}
    if catf and catf in cols:
        return {"entity": _ent(catf), "prop": catf}
    # Date-level category whose field name is a Tableau date-part pseudonym (e.g.
    # 'Year' for YEAR([date_added])) rather than a real column. Bind to the derived
    # date-part column of the worksheet's date dimension, which emit_tmdl emits via
    # date_levels.needed_parts. Without this the binder falls through to the first
    # dimension column and plots an arbitrary axis (e.g. 'type' instead of years).
    if D.needs_part(level):
        dcol = next((d for d in (ws.get("dimensions") or []) if d in dcols), None) \
            if ws else None
        if dcol:
            return {"entity": entity, "prop": D.part_column_name(dcol, level)}
    dim = first_dim_col(ir)
    return {"entity": _ent(dim) if dim else entity,
            "prop": dim or (next(iter(cols)) if cols else "Column")}


def _col_role(ir: Dict, name: Optional[str]) -> Optional[str]:
    for c in ir.get("columns", []):
        if c.get("name") == name:
            return c.get("role")
    return None


def value_binding(valf: Optional[str], entity: str, mset: Set[str],
                  mlist: List[str], cols: Set[str], ir: Dict) -> Dict:
    if valf and valf in mset:
        return {"entity": entity, "prop": valf, "isMeasure": True}
    # A Tableau chart value is an aggregation (e.g. CNT(show_id)). When the
    # resolved field is a raw dimension/ID column, Power BI would blindly SUM it,
    # producing a meaningless number. Bind the model's measure instead — prefer
    # one whose name references the field, else the first measure. Additive
    # numeric measure-role columns are left as-is (Power BI sums them correctly).
    if mlist and (not valf or _col_role(ir, valf) != "measure"):
        chosen = next((m for m in mlist if valf and valf.lower() in m.lower()), mlist[0])
        return {"entity": entity, "prop": chosen, "isMeasure": True}
    if valf and valf in cols:
        return {"entity": entity, "prop": valf, "isMeasure": False}
    col = next(iter(cols)) if cols else "Value"
    return {"entity": entity, "prop": col, "isMeasure": False}


def text_only_dimension(ws: Optional[Dict], cols: Set[str]) -> Optional[str]:
    """Return the single Text-encoded dimension of a text-table worksheet, else None.

    A Tableau text mark with no rows/cols/values/measures shelves shows exactly
    one column: the distinct values of its Text-encoded dimension (the same shape
    ``table_columns`` collapses such a worksheet to). Detail/Tooltip pills carried
    in ``dimensions`` are ignored — Tableau does not display them.
    """
    if not ws:
        return None
    if ws.get("rows") or ws.get("cols") or ws.get("values") or ws.get("measures"):
        return None
    txt = decode_field((ws.get("encodings") or {}).get("text"))
    return txt if txt and txt in cols else None


def _csv_path_for_field(field: str, ir: Dict) -> Optional[str]:
    """Resolve the absolute path of the active CSV whose header carries ``field``."""
    target = ET._normalize_name(field)
    for ds in ir.get("dataSources", []):
        if ds.get("internalName") == "Parameters":
            continue
        for f in ds.get("files", []) or []:
            if not str(f).lower().endswith(".csv"):
                continue
            path = ET._abs_csv(ir, f)
            _delim, headers = CP.detect(path)
            if any(ET._normalize_name(h) == target for h in headers):
                return path
    return None


def slicer_dimension(ws: Optional[Dict], ir: Dict, cols: Set[str]) -> Optional[str]:
    """Return the dimension a text-table worksheet should render as a SLICER, else None.

    Text-only single-dimension worksheets are Tableau distinct-value lists; the
    faithful interactive Power BI equivalent is a slicer. Restricted to low-
    cardinality dimensions (``SLICER_MAX_CARDINALITY``) so a high-cardinality list
    (Genre, Description) stays a table where a slicer would be unusable.
    """
    dim = text_only_dimension(ws, cols)
    if not dim:
        return None
    path = _csv_path_for_field(dim, ir)
    if not path:
        return None
    n = CP.distinct_count(path, dim)
    return dim if 2 <= n <= SLICER_MAX_CARDINALITY else None


def table_columns(ws: Optional[Dict], ir: Dict, entity: str,
                  mset: Set[str], cols: Set[str],
                  decisions: Optional[Dict] = None) -> List[Dict]:
    out: List[Dict] = []
    dcols = date_cols(ir)
    level = ws.get("categoryDateLevel") if ws else None

    def _ent(prop: str) -> str:
        return entity_for_field(prop, entity, decisions, ir) if decisions else entity

    if ws:
        dims = list(ws.get("dimensions") or [])
        # A Tableau text-table mark (no rows/cols/values shelves, only a Text pill)
        # renders ONE column: the distinct values of the text-encoded dimension. The
        # other entries in `dimensions` are Detail/Tooltip pills Tableau does not show
        # as columns. Without this restriction every Detail dim leaks in as a spurious
        # column (e.g. a 'Rating' value list gaining bogus title + type columns and
        # exploding to one row per title), so the visual no longer matches Tableau.
        if not (ws.get("rows") or ws.get("cols")
                or ws.get("values") or ws.get("measures")):
            txt = decode_field((ws.get("encodings") or {}).get("text"))
            if txt and txt in cols:
                dims = [txt]
        for d in dims:
            if d in dcols and D.needs_part(level):
                out.append({"entity": entity, "prop": D.part_column_name(d, level),
                            "isMeasure": False})
            elif d in cols:
                out.append({"entity": _ent(d), "prop": d, "isMeasure": False})
        for v in ws.get("values", []) or []:
            if v in mset:
                out.append({"entity": entity, "prop": v, "isMeasure": True})
            elif v in cols:
                out.append({"entity": _ent(v), "prop": v, "isMeasure": False})
    if not out:
        out = [{"entity": _ent(c["name"]), "prop": c["name"], "isMeasure": False}
               for c in ir.get("columns", [])[:6]]
    return out or [{"entity": entity, "prop": "Column", "isMeasure": False}]
