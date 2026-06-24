"""twb_visuals.py — extract worksheet marks and dashboard zone layouts.

Deterministic extraction of visual metadata. Where the Tableau mark class is
unambiguous we set inferredVisualType; when it is "Automatic" (or otherwise
ambiguous) we leave it null so the LLM resolves it via decisions.json.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twb_xml as X  # noqa: E402
import twb_fields as F  # noqa: E402
import mark_infer as MI  # noqa: E402

# Tableau <column-instance> derivation -> DAX aggregation. The derivation is the
# AUTHORITATIVE signal that a field is used as a measure (value) on a sheet, even
# when the underlying column is dimension-typed (e.g. CountD of an id column).
DERIVATION_AGG = {
    "Sum": "SUM", "Avg": "AVG", "Count": "COUNT", "CountD": "COUNTD",
    "Min": "MIN", "Max": "MAX", "Median": "MEDIAN", "Attr": "ATTR",
    "StdDev": "STDEV", "Var": "VAR",
}
# Date-truncation derivations -> a date grain (these stay DIMENSIONS, not values).
DATE_DERIV_LEVEL = {
    "Year": "year", "Quarter": "quarter", "Month": "month", "Week": "week",
    "Day": "day", "Trunc Year": "year", "Trunc Quarter": "quarter",
    "Trunc Month": "month", "Trunc Week": "week", "Trunc Day": "day",
}
_HASH_SUFFIX = re.compile(r"\s*\(copy\)(\s*\(copy\))*(_\d{6,})?$|_\d{12,}$")


def _clean_field(name: str, calc_map: Dict) -> str:
    """Resolve a raw column token to a friendly field name.

    Maps internal Calculation_<id> -> caption, and strips Tableau duplicate-field
    noise ((copy) and _<long-digits> suffixes) so shelves read like Tableau's UI.
    """
    name = X.strip_brackets(name)
    if name in calc_map:
        return calc_map[name]
    stripped = _HASH_SUFFIX.sub("", name).strip()
    return calc_map.get(stripped, stripped or name)


def _column_instances(ws: ET.Element, calc_map: Dict) -> List[Dict]:
    """Parse a worksheet's <datasource-dependencies>/<column-instance> block.

    This is the authoritative per-sheet field list: each entry names the source
    column, its aggregation (derivation) and value/dimension type. Far more
    reliable than scraping the rows/cols shelf text, which is what the parser
    used before (and which lost every aggregated measure).
    """
    out: List[Dict] = []
    seen = set()
    for ci in ws.findall(".//datasource-dependencies/column-instance"):
        col = X.attr(ci, "column")
        deriv = X.attr(ci, "derivation", "None")
        typ = X.attr(ci, "type", "")
        iname = X.attr(ci, "name", "")
        if not col or iname in seen:
            continue
        seen.add(iname)
        agg = DERIVATION_AGG.get(deriv)
        date_level = DATE_DERIV_LEVEL.get(deriv)
        is_measure = bool(agg) or (typ == "quantitative" and not date_level)
        out.append({
            "field": _clean_field(col, calc_map),
            "column": X.strip_brackets(col),
            "agg": agg,
            "type": typ,
            "instanceName": iname,
            "isMeasure": is_measure,
            "dateLevel": date_level,
        })
    return out


def _tooltip_fields(instances: List[Dict], fmt_map: Dict[str, str]) -> List[Dict]:
    """All marks-card fields = Tableau's DEFAULT tooltip set (every pill on hover).

    Tableau's automatic tooltip lists every field placed on the worksheet (rows,
    cols and the marks card: Color/Size/Detail/Label/Text/Tooltip). The parser
    already resolves them in <column-instance>; this returns the full ordered,
    de-duplicated set (dimensions AND measures) so the Power BI visual can project
    EVERY one into its hover tooltip — matching Tableau, where the report showed 4
    fields but Power BI only carried the 2 plotted measures before.
    """
    out: List[Dict] = []
    seen = set()
    for ci in instances:
        f = ci.get("field")
        if not f or f in seen or f.endswith("(generated)"):
            continue
        seen.add(f)
        out.append({
            "field": f,
            "agg": ci.get("agg"),
            "isMeasure": bool(ci.get("isMeasure")),
            "format": fmt_map.get(f),
        })
    return out


def _same_field(a: Optional[str], b: Optional[str]) -> bool:
    """Compare two field names ignoring a federated-source suffix like
    ' (state_region.csv)' and case, so 'state (state_region.csv)' == 'state'."""
    def _norm(f: Optional[str]) -> str:
        return re.sub(r"\s*\([^)]*\)\s*$", "", f or "").strip().lower()
    return bool(a) and bool(b) and _norm(a) == _norm(b)


def _topn_filter(ws: ET.Element, calc_map: Dict,
                 category: Optional[str] = None) -> Optional[Dict]:
    """Extract a Tableau Top-N filter (groupfilter count/end='top'/'bottom').

    A worksheet often carries a cascade of Top-N filters — one per nested shelf
    dimension (e.g. region, subregion, state for a 'Top 10 States' chart). The
    one that actually limits the visible marks is the filter on the LEAF
    dimension being plotted (the category axis). So when ``category`` is known we
    return the Top-N whose column matches it; otherwise fall back to the first.
    This keeps the Power BI Top-N on the same field Tableau ranks, so a 'Top 10
    States' chart shows 10 states — never the outer region grain.
    """
    candidates: List[Dict] = []
    for filt in ws.findall(".//filter"):
        top = filt.find(".//groupfilter[@end='top']")
        if top is None:
            top = filt.find(".//groupfilter[@end='bottom']")
        if top is None:
            continue
        count = top.get("count")
        if not count:
            continue
        order = top.find(".//groupfilter[@function='order']")
        direction = (X.attr(order, "direction") or "DESC").upper() if order is not None else "DESC"
        by_expr = X.attr(order, "expression") if order is not None else None
        field = F.resolve_ref(X.attr(filt, "column"), calc_map)
        candidates.append({
            "field": field,
            "n": int(count) if count.isdigit() else count,
            "direction": "TOP" if X.attr(top, "end") == "top" else "BOTTOM",
            "byMeasure": by_expr,
            "sortDirection": direction,
        })
    if not candidates:
        return None
    if category:
        for c in candidates:
            if c["field"] and _same_field(c["field"], category):
                return c
    return candidates[0]


def worksheet_datasources(root: ET.Element) -> set:
    """Names of datasources referenced by any worksheet (for active flagging)."""
    used = set()
    for ws in root.iter("worksheet"):
        for ds in ws.iter("datasource"):
            cap = X.attr(ds, "caption") or X.attr(ds, "name")
            if cap and cap not in ("Parameters",):
                used.add(cap)
    return used


def _pane_mark_classes(ws: ET.Element) -> List[str]:
    """Mark class from every pane (a Tableau dual-axis sheet has two panes, e.g.
    one Bar pane + one Line pane). Used to recognise combo charts."""
    out: List[str] = []
    for pane in ws.findall(".//panes/pane"):
        mk = pane.find("mark")
        cls = X.attr(mk, "class", "") if mk is not None else ""
        if cls:
            out.append(cls)
    return out


def extract_worksheets(root: ET.Element, calc_map: Optional[Dict] = None,
                       measures: Optional[set] = None,
                       param_map: Optional[Dict] = None) -> List[Dict]:
    """Return IR worksheets with mark type, shelves, encodings and resolved fields."""
    calc_map = calc_map or {}
    measures = measures or set()
    param_map = param_map or {}
    color_maps = _discrete_color_maps(root, calc_map)
    sheets: List[Dict] = []
    for ws in root.iter("worksheet"):
        name = X.attr(ws, "name", "")
        pane = ws.find(".//panes/pane")
        mark = pane.find("mark") if pane is not None else None
        mark_class = X.attr(mark, "class", "Automatic")
        pane_marks = _pane_mark_classes(ws)
        rows = _shelf_fields(ws, "rows", calc_map)
        cols = _shelf_fields(ws, "cols", calc_map)
        encodings = _encodings(pane)
        instances = _column_instances(ws, calc_map)
        fields = _resolve_fields(instances, rows, cols, encodings, calc_map, measures)
        rows_text = (ws.findtext(".//rows") or "")
        cols_text = (ws.findtext(".//cols") or "")
        orientation = _orientation(
            [ci for ci in instances if ci["isMeasure"]], rows_text, cols_text)
        # Attach each measure's Tableau number-format mask (%, currency, K/M).
        fmt_map = _field_formats(ws, calc_map)
        for m in fields["measures"]:
            m["format"] = fmt_map.get(m["field"])
        formatting = _worksheet_formatting(ws)
        # Attach this sheet's discrete colour rule (e.g. 'Peak' -> orange) so the
        # mark keeps its Tableau colour in Power BI instead of a default palette.
        color_field = _sheet_color_field(instances, encodings)
        if color_field and color_field in color_maps:
            formatting = formatting or {}
            formatting["colorMap"] = {"field": color_field,
                                      "values": color_maps[color_field]}
        sheets.append({
            "name": name, "datasource": _primary_ds(ws), "markClass": mark_class,
            "rows": rows, "cols": cols, "encodings": encodings,
            "categoryField": fields["category"], "valueField": fields["value"],
            "categoryDateLevel": fields["categoryDateLevel"],
            "dimensions": fields["dimensions"], "values": fields["values"],
            "measures": fields["measures"],
            "paneMarks": pane_marks,
            "axes": _axes(fields, orientation),
            "orientation": orientation,
            "caption": _worksheet_caption(ws, calc_map, param_map),
            "title": _worksheet_title(ws, calc_map, param_map),
            "tooltip": _customized_tooltip(ws, calc_map, param_map),
            "tooltipFields": _tooltip_fields(instances, fmt_map),
            "filters": _worksheet_filters(ws, calc_map),
            "filterDetails": _worksheet_filter_details(ws, calc_map),
            "topN": _topn_filter(ws, calc_map, fields["category"]),
            "sort": _worksheet_sort(ws, calc_map),
            "referenceLines": _reference_lines(ws, calc_map),
            "formatting": formatting,
        })
        # Stamp the deterministic visual type into the IR so genuinely-resolvable
        # worksheets never reach the agent (null only = truly ambiguous).
        sheets[-1]["inferredVisualType"] = MI.infer_visual_type(sheets[-1])
    return sheets


def _resolve_fields(instances: List[Dict], rows: List[str], cols: List[str],
                    enc: Dict, calc_map: Dict, measures: set) -> Dict:
    """Pick category/value/dimensions/values from column-instances when present.

    The <column-instance> block is authoritative (it carries the aggregation), so
    it is preferred. When a worksheet has no dependency block we fall back to the
    legacy shelf-text heuristic in twb_fields.summarize.
    """
    if instances:
        meas = _dedupe([ci["field"] for ci in instances if ci["isMeasure"]])
        dims = _dedupe([ci["field"] for ci in instances if not ci["isMeasure"]])
        measures_detail = _dedupe_measures(
            {"field": ci["field"], "agg": ci["agg"] or "SUM", "column": ci["column"]}
            for ci in instances if ci["isMeasure"])
        date_level = next(
            (ci["dateLevel"] for ci in instances
             if not ci["isMeasure"] and ci["dateLevel"]), None)
        # Category axis = the sheet's OWN shelf field (the first non-measure field
        # actually placed on Columns/Rows), NOT dims[0]. dims[] also contains the
        # dashboard slicer fields (from <slices>), which otherwise hijack the axis
        # so every sheet wrongly reported the first slicer (e.g. 'grade'). Scanning
        # cols-then-rows yields the primary axis (X for vertical, Y for horizontal),
        # matching Tableau. If no field sits on a shelf (pie/bubble/treemap put the
        # category on a color/text encoding), fall back to the first dimension that
        # is actually referenced by an encoding. Still nothing => KPI card => None.
        # Skip measures AND Tableau's auto '(generated)' geo coords (Latitude/
        # Longitude generated) -- on a map the real category is the geo field on
        # the detail encoding (e.g. country), picked up by the encoding fallback.
        meas_set = set(meas)

        def _is_cat(f: str) -> bool:
            return bool(f) and f not in meas_set and "(generated)" not in f.lower()

        cat_cols = [f for f in cols if _is_cat(f)]
        cat_rows = [f for f in rows if _is_cat(f)]
        # When every category dimension is nested on a SINGLE shelf (a Tableau
        # drill hierarchy such as region > subregion > state), the marks render at
        # the LEAF grain, so the chart's category is the innermost (last) field --
        # not the outermost. Picking the leaf prevents binding the wrong, coarser
        # column (e.g. a 'Top 10 States' chart wrongly grouping by region) and is
        # what keeps the axis to a single, correct column. When dimensions are
        # split across both shelves (category + series), keep the first shelf
        # field as the category and let the other become the series/legend.
        if cat_cols and not cat_rows:
            category = cat_cols[-1] if len(cat_cols) > 1 else cat_cols[0]
        elif cat_rows and not cat_cols:
            category = cat_rows[-1] if len(cat_rows) > 1 else cat_rows[0]
        else:
            category = next((f for f in (cols + rows) if _is_cat(f)), None)
        if category is None:
            enc_blob = " ".join(str(v) for v in enc.values() if v)
            category = next(
                (ci["field"] for ci in instances
                 if not ci["isMeasure"]
                 and (ci.get("instanceName") or "").strip("[]")
                 and (ci.get("instanceName") or "").strip("[]") in enc_blob),
                None)
        return {
            "category": category,
            "value": meas[0] if meas else None,
            "dimensions": dims, "values": meas,
            "measures": measures_detail, "categoryDateLevel": date_level,
        }
    legacy = F.summarize(rows, cols, enc, calc_map, measures)
    return {
        "category": legacy["category"], "value": legacy["value"],
        "dimensions": legacy["dimensions"], "values": legacy["values"],
        "measures": [{"field": v, "agg": "SUM", "column": v}
                     for v in legacy["values"]],
        "categoryDateLevel": legacy["categoryDateLevel"],
    }


def _axes(fields: Dict, orientation: Optional[str]) -> Dict:
    """Make the 'what vs what' of the chart explicit (category vs value axes).

    Restates the resolved shelf fields as a plain category/value axis pair so the
    report layer (and a human reader) can see, at a glance, what sits on each
    axis: the category axis (X for vertical charts), its date grain if any, and
    the ordered measures plotted on the value axis (Y).
    """
    return {
        "category": fields.get("category"),
        "categoryLevel": fields.get("categoryDateLevel"),
        "values": list(fields.get("values") or []),
        "orientation": orientation,
    }


def _dedupe(items: List[Optional[str]]) -> List[str]:
    seen: List[str] = []
    for it in items:
        if it and it not in seen:
            seen.append(it)
    return seen


def _dedupe_measures(items) -> List[Dict]:
    seen: List[Dict] = []
    names = set()
    for it in items:
        if it["field"] and it["field"] not in names:
            names.add(it["field"])
            seen.append(it)
    return seen


def _orientation(measure_instances: List[Dict], rows_text: str,
                 cols_text: str) -> Optional[str]:
    """Bars are horizontal when the measure sits on Columns, vertical on Rows."""
    for ci in measure_instances:
        tok = ci.get("instanceName", "").strip("[]")
        if not tok:
            continue
        if tok in (cols_text or ""):
            return "horizontal"
        if tok in (rows_text or ""):
            return "vertical"
    return None


def _formatted_text_skeleton(node: Optional[ET.Element], calc_map: Dict,
                             param_map: Dict) -> Optional[str]:
    """Resolve a Tableau <formatted-text> block to a static label skeleton.

    Tableau title/caption runs embed live field/parameter tokens
    (<[Parameters].[..]>, <[ds].[none:Field:nk]>). PBIR textboxes cannot bind
    data, so each token becomes '{Resolved Field}' to preserve the label shape.
    'Æ' is Tableau's soft line-break glyph in titles — collapse it to a space.
    """
    if node is None:
        return None
    parts: List[str] = []
    for run in node.findall("run"):
        text = run.text or ""
        for tok in re.findall(r"<([^>]+)>", text):
            resolved = (F.resolve_param(tok, param_map)
                        if "Parameters" in tok else F.resolve_ref(tok, calc_map))
            # Auto-generated calcs with no friendly caption resolve to their
            # internal 'Calculation_<id>' name -> show a neutral '{value}' token.
            if resolved and re.fullmatch(r"Calculation_\d+", resolved):
                resolved = None
            text = text.replace(f"<{tok}>", f"{{{resolved or 'value'}}}")
        parts.append(text)
    return X.clean_text("".join(parts).replace("\u00c6", " ")) or None


def _worksheet_caption(ws: ET.Element, calc_map: Dict, param_map: Dict) -> Optional[str]:
    """Resolve a worksheet's dynamic <caption> to a static label skeleton."""
    return _formatted_text_skeleton(
        ws.find(".//layout-options/caption/formatted-text"), calc_map, param_map)


def _worksheet_title(ws: ET.Element, calc_map: Dict, param_map: Dict) -> Optional[str]:
    """Resolve a worksheet's <title> (shown above the viz) to a label skeleton."""
    return _formatted_text_skeleton(
        ws.find(".//layout-options/title/formatted-text"), calc_map, param_map)


def _worksheet_filters(ws: ET.Element, calc_map: Dict) -> List[str]:
    """Return the fields that filter this worksheet (from <slices>).

    Tableau lists every column applied as a filter to a sheet under <slices>.
    Action-generated filters (cross-viz interactions) are captured separately in
    actions[] and skipped here so this list reflects real user/data filters.
    """
    out: List[str] = []
    seen = set()
    for col in ws.findall(".//slices/column"):
        ref = (col.text or "").strip()
        if not ref or "Action (" in ref:
            continue
        field = F.resolve_ref(ref, calc_map)
        # Skip Tableau internal pseudo-fields (':Measure Names', ':Measure
        # Values') -- they are not real data filters in Power BI.
        if field and not field.startswith(":") and field not in seen:
            seen.add(field)
            out.append(field)
    return out


def _worksheet_filter_details(ws: ET.Element, calc_map: Dict) -> List[Dict]:
    """Capture every filter's column, type and include/exclude selection (metadata).

    Combines two XML sources so no filter column is missed: the <filter> elements
    (which carry the class + member logic) and the <slices> list (the complete set
    of columns filtered on the sheet). Reads only the filter DEFINITION, never the
    data. Internal pseudo-filters (:Measure Names) and action-generated filters are
    skipped — those are captured separately in actions[]. Member values capped.
    """
    by_field: Dict[str, Dict] = {}
    order: List[str] = []

    def _add(field: Optional[str], fclass, scope, values, rng):
        if not field or field.startswith(":"):
            return
        if field not in by_field:
            order.append(field)
            by_field[field] = {"field": field, "filterClass": fclass,
                               "scope": scope, "values": values, "range": rng}
        else:  # enrich an existing slice-only entry with <filter> detail
            rec = by_field[field]
            rec["filterClass"] = rec["filterClass"] or fclass
            rec["scope"] = rec["scope"] or scope
            rec["values"] = rec["values"] or values
            rec["range"] = rec["range"] or rng

    # 1) rich <filter> elements (type + member logic)
    for filt in ws.findall(".//filter"):
        col = X.attr(filt, "column")
        if not col or "Action (" in col:
            continue
        scope, values, rng = _filter_logic(filt)
        _add(F.resolve_ref(col, calc_map), X.attr(filt, "class"), scope, values, rng)

    # 2) <slices> columns (the complete filtered-column list)
    for col in ws.findall(".//slices/column"):
        ref = (col.text or "").strip()
        if not ref or "Action (" in ref:
            continue
        _add(F.resolve_ref(ref, calc_map), None, None, None, None)

    return [by_field[f] for f in order]


def _filter_logic(filt: ET.Element):
    """Return (scope, values, range) for a <filter>: include/exclude + members.

    Categorical filters list selected members via <groupfilter function='member'>;
    an 'except'/'exclusions' wrapper means EXCLUDE. Quantitative/range filters
    carry numeric <min>/<max>. Member values are capped at 25 (this is the filter
    definition, not data extraction).
    """
    members: List[str] = []
    scope: Optional[str] = None
    for gf in filt.findall(".//groupfilter"):
        fn = (X.attr(gf, "function") or "").lower()
        if fn in ("except", "exclusions"):
            scope = "exclude"
        elif fn == "member":
            if scope is None:
                scope = "include"
            mem = X.attr(gf, "member")
            if mem:
                members.append(mem.strip('"').strip("[]"))
    rng = None
    mn, mx = filt.findtext(".//min"), filt.findtext(".//max")
    if mn is not None or mx is not None:
        rng = {"min": mn, "max": mx}
    values = members[:25] if members else None
    return scope, values, rng


def _worksheet_sort(ws: ET.Element, calc_map: Dict) -> Optional[Dict]:
    """Return {field, direction} for an explicit field sort, else None."""
    s = ws.find(".//sort")
    if s is None:
        return None
    field = F.resolve_ref(X.attr(s, "column"), calc_map)
    direction = (X.attr(s, "direction") or "").upper() or None
    if not field:
        return None
    return {"field": field, "direction": direction}


def _field_formats(ws: ET.Element, calc_map: Dict) -> Dict[str, str]:
    """Map a sheet field -> its Tableau number-format mask (verbatim).

    Reads ``<format attr='text-format' field='[ds].[agg:Field:qk]' value='p0.00%'/>``
    from the worksheet <style>. The mask is kept EXACTLY as Tableau wrote it (the
    emitter/LLM converts ``p0.00%`` -> percent, ``c"$"#,##0,,M`` -> $ millions,
    ``n#,##0,.00K`` -> thousands). This is what makes a measure shown as a percent /
    currency / K-M in Tableau keep that format in Power BI instead of a raw number.
    """
    out: Dict[str, str] = {}
    for f in ws.findall(".//style//format"):
        if X.attr(f, "attr") != "text-format":
            continue
        field, val = X.attr(f, "field"), X.attr(f, "value")
        if field and val:
            name = _clean_pill(field, calc_map)
            if name and name not in out:
                out[name] = val
    return out


def _discrete_color_maps(root: ET.Element, calc_map: Dict) -> Dict[str, Dict[str, str]]:
    """Global field -> {member value: #hex} from ``<encoding attr='color'>`` palettes.

    Captures Tableau's discrete colour assignment -- the literal "this category is
    orange" rule: ``<encoding attr='color' field=...><map to='#f28e2b'><bucket>
    "Peak"</bucket>``. Built once across the workbook and attached to each sheet
    whose colour encoding uses that field, so an orange bar in Tableau stays orange
    in Power BI. Never invents a colour: an absent map yields no entry.
    """
    out: Dict[str, Dict[str, str]] = {}
    for enc in root.findall(".//style-rule[@element='mark']/encoding"):
        if X.attr(enc, "attr") != "color":
            continue
        raw = X.attr(enc, "field")
        field = _clean_pill(raw, calc_map) if raw else None
        if not field:
            continue
        cmap: Dict[str, str] = {}
        for mp in enc.findall("map"):
            hexv, bucket = X.attr(mp, "to"), mp.findtext("bucket")
            if hexv and bucket:
                cmap[bucket.strip().strip('"')] = hexv
        if cmap:
            out.setdefault(field, {}).update(cmap)
    return out


def _sheet_color_field(instances: List[Dict], enc: Dict) -> Optional[str]:
    """Return the clean field name placed on the colour encoding of a sheet."""
    blob = str(enc.get("color") or "")
    if not blob:
        return None
    for ci in instances:
        tok = (ci.get("instanceName") or "").strip("[]")
        if tok and tok in blob:
            return ci["field"]
    return None


def _reference_lines(ws: ET.Element, calc_map: Dict) -> List[Dict]:
    """Capture ``<reference-line>`` definitions (constant / average / median bands).

    Best-effort and None-safe: reads the referenced field, aggregation, value and
    scope from any ``<reference-line>`` element. Returns ``[]`` when the sheet has
    none -- it never fabricates a line.
    """
    out: List[Dict] = []
    for rl in ws.findall(".//reference-line"):
        field = (X.attr(rl, "column") or X.attr(rl, "field")
                 or X.attr(rl, "value-field"))
        out.append({
            "field": F.resolve_ref(field, calc_map) if field else None,
            "aggregation": X.attr(rl, "aggregation") or X.attr(rl, "agg"),
            "value": X.attr(rl, "value"),
            "scope": X.attr(rl, "scope"),
        })
    return out


def _customized_tooltip(ws: ET.Element, calc_map: Dict,
                        param_map: Dict) -> Optional[str]:
    """Resolve a worksheet's custom tooltip ``<formatted-text>`` to a label skeleton."""
    return _formatted_text_skeleton(
        ws.find(".//customized-tooltip/formatted-text"), calc_map, param_map)



def _worksheet_formatting(ws: ET.Element) -> Optional[Dict]:
    """Extract a compact formatting summary for the worksheet's visual.

    Pulls the colour palette, gridline visibility, field-label visibility, and
    mark stroke/label flags from Tableau <style-rule>/<format> blocks. Returns
    None when the sheet carries no explicit formatting (honest empty, no guess).
    """
    fmt: Dict[str, object] = {
        "colorPalette": None, "gridlines": None, "showFieldLabels": None,
        "markStroke": None, "showMarkLabels": None,
        "markColor": None, "markSize": None, "background": None,
        "fontName": None, "fontSize": None, "fontColor": None,
        "alignment": None, "titleColor": None, "titleFontSize": None,
        "titleFontName": None, "titleAlignment": None,
    }
    # The worksheet title's font (colour/family/size/alignment) is carried as
    # attributes on the <run> inside <layout-options><title><formatted-text>, NOT
    # in a style-rule. Tableau's Netflix titles are red 'Tableau Bold' 12pt —
    # capture them so the Power BI visual title matches instead of defaulting.
    trun = ws.find(".//layout-options/title/formatted-text/run")
    if trun is not None:
        tcolor = X.attr(trun, "fontcolor")
        if tcolor:
            fmt["titleColor"] = tcolor
        tsize = X.attr(trun, "fontsize")
        if tsize:
            fmt["titleFontSize"] = tsize
        tfname = X.attr(trun, "fontname")
        if tfname:
            fmt["titleFontName"] = tfname
        talign = X.attr(trun, "fontalignment")
        if talign:
            # Tableau fontalignment: 0=left, 1=center, 2=right.
            fmt["titleAlignment"] = {"0": "left", "1": "center",
                                     "2": "right"}.get(talign)
    palette = [c.text for c in ws.findall(".//table/style//color-palette/color") if c.text]
    if palette:
        fmt["colorPalette"] = palette
    style = ws.find(".//table/style")
    if style is not None:
        for rule in style.findall("style-rule"):
            elem = X.attr(rule, "element")
            for f in rule.findall("format"):
                attr, val = X.attr(f, "attr"), X.attr(f, "value")
                if elem == "gridline" and attr == "line-visibility":
                    fmt["gridlines"] = (val == "on")
                elif elem == "worksheet" and attr == "display-field-labels":
                    fmt["showFieldLabels"] = (val == "true")
                elif elem == "table" and attr == "background-color" and val:
                    fmt["background"] = val
                # Font + alignment (first value wins; title scoped separately)
                if attr == "font-family" and val and not fmt["fontName"]:
                    fmt["fontName"] = val
                elif attr == "font-size" and val:
                    if elem == "title" and not fmt["titleFontSize"]:
                        fmt["titleFontSize"] = val
                    elif not fmt["fontSize"]:
                        fmt["fontSize"] = val
                elif attr == "text-align" and val and not fmt["alignment"]:
                    fmt["alignment"] = val
                elif attr == "color" and val:
                    if elem == "title" and not fmt["titleColor"]:
                        fmt["titleColor"] = val
                    elif not fmt["fontColor"]:
                        fmt["fontColor"] = val
    for f in ws.findall(".//panes/pane/style//format"):
        attr, val = X.attr(f, "attr"), X.attr(f, "value")
        if attr == "stroke-color":
            fmt["markStroke"] = val
        elif attr == "mark-labels-show":
            fmt["showMarkLabels"] = (val == "true")
        elif attr == "mark-color" and val:
            fmt["markColor"] = val
        elif attr == "size" and val:
            fmt["markSize"] = val
    if all(v is None for v in fmt.values()):
        return None
    return fmt


def _primary_ds(ws: ET.Element) -> Optional[str]:
    ds = ws.find(".//datasources/datasource")
    return X.attr(ds, "caption") or X.attr(ds, "name") if ds is not None else None


def _clean_pill(token: str, calc_map: Dict) -> Optional[str]:
    """Resolve a raw Tableau shelf pill to a friendly field name.

    A shelf pill looks like ``[ds].[deriv:Field Name:typesuffix]`` (e.g.
    ``[federated.x].[usr:Min/Max Quantity (copy)_123:qk]``). We drop the
    datasource qualifier, the derivation prefix (yr/mn/sum/usr/none/...) and the
    :qk/:ok/:nk type suffix, keep the field name verbatim (so 'Min/Max' is NOT
    split), and resolve internal Calculation_<id> names to their caption. The
    Tableau pseudo-fields 'Multiple Values' and ':Measure Names' are preserved
    as readable labels so the LLM knows the sheet stacks several measures.
    """
    refs = re.findall(r"\[([^\]]*)\]", token)
    if not refs:
        return None
    inner = refs[-1].strip()
    if inner == "Multiple Values":
        return "Multiple Values"
    if inner.lstrip(":") == "Measure Names":
        return "Measure Names"
    parts = inner.split(":")
    if len(parts) >= 3:          # deriv : Field Name : suffix
        name = ":".join(parts[1:-1])
    elif len(parts) == 2:        # deriv : Field Name
        name = parts[1]
    else:
        name = inner
    return _clean_field(name, calc_map)


def _shelf_fields(ws: ET.Element, shelf: str, calc_map: Dict) -> List[str]:
    """Return the ordered, de-noised field names placed on a shelf (rows/cols).

    Extracts every bracketed pill reference (ignoring the +,/ layout operators
    and parentheses) and cleans each one. This fixes the old splitter that broke
    field names containing '/' (e.g. 'Min/Max') and left raw 'usr:...:qk' tokens.
    """
    node = ws.find(f".//{shelf}")
    if node is None or not node.text:
        return []
    out: List[str] = []
    seen = set()
    for tok in re.findall(r"\[[^\]]*\](?:\.\[[^\]]*\])?", node.text):
        name = _clean_pill(tok, calc_map)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _encodings(pane: Optional[ET.Element]) -> Dict[str, Optional[str]]:
    enc: Dict[str, Optional[str]] = {
        "color": None, "size": None, "text": None,
        "shape": None, "detail": None, "label": None,
        "tooltip": None, "wedgeSize": None, "path": None,
    }
    node = pane.find("encodings") if pane is not None else None
    if node is None:
        return enc
    # Tableau tags map to IR encoding channels (wedge-size -> pie angle, lod -> detail)
    tag_to_key = {
        "color": "color", "size": "size", "text": "text",
        "shape": "shape", "lod": "detail", "label": "label",
        "tooltip": "tooltip", "wedge-size": "wedgeSize", "path": "path",
    }
    for tag, key in tag_to_key.items():
        child = node.find(tag)
        if child is not None:
            enc[key] = X.strip_brackets(X.attr(child, "column"))
    return enc


def extract_dashboards(root: ET.Element, calc_map: Optional[Dict] = None,
                       param_map: Optional[Dict] = None) -> List[Dict]:
    """Return IR dashboards with size, classified zones and navigation buttons."""
    calc_map = calc_map or {}
    param_map = param_map or {}
    ws_names = {X.attr(w, "name", "") for w in root.iter("worksheet")}
    dashboards: List[Dict] = []
    for db in root.iter("dashboard"):
        size = db.find("size")
        zones, buttons = _zones_and_buttons(db, ws_names, calc_map, param_map)
        dashboards.append({
            "name": X.attr(db, "name", ""),
            "size": {"w": X.int_attr(size, "maxwidth", 1366),
                     "h": X.int_attr(size, "maxheight", 768)},
            "title": _formatted_text_skeleton(
                db.find(".//layout-options/title/formatted-text"), calc_map, param_map),
            "background": _dashboard_background(db),
            "zones": zones, "buttons": buttons,
        })
    return dashboards


def _dashboard_background(db: ET.Element) -> Optional[str]:
    """Return the dashboard background colour (#hex) if explicitly set."""
    for f in db.findall(".//style//format"):
        if X.attr(f, "attr") == "background-color":
            val = X.attr(f, "value")
            if val and val.startswith("#"):
                return val
    return None


def extract_actions(root: ET.Element, calc_map: Optional[Dict] = None) -> List[Dict]:
    """Return dashboard actions (filter / highlight / url) -> PBI interactions.

    Tableau <action> elements drive cross-viz behaviour: filter actions become
    Power BI cross-filtering, highlight actions become cross-highlighting, URL
    actions become drillthrough/links. Each record names the source sheet and
    target so the report layer can wire interactions deterministically.
    """
    calc_map = calc_map or {}
    out: List[Dict] = []
    seen = set()
    for actions in root.iter("actions"):
        for act in actions.findall("action"):
            name = X.strip_brackets(X.attr(act, "caption") or X.attr(act, "name") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            cmd = act.find(".//command")
            cmd_name = (X.attr(cmd, "command", "") or "").lower()
            atype = ("filter" if "filter" in cmd_name else
                     "highlight" if "highlight" in cmd_name else
                     "url" if "url" in cmd_name else "other")
            src = act.find("source")
            target = None
            if cmd is not None:
                for p in cmd.findall("param"):
                    if X.attr(p, "name") == "target":
                        target = X.attr(p, "value")
            activation = act.find("activation")
            out.append({
                "name": name,
                "type": atype,
                "sourceSheet": X.attr(src, "worksheet") if src is not None else None,
                "sourceDashboard": X.attr(src, "dashboard") if src is not None else None,
                "target": target,
                "activation": X.attr(activation, "type") if activation is not None else None,
            })
    return out


def _zones_and_buttons(db: ET.Element, ws_names: set, calc_map: Dict, param_map: Dict):
    """Classify only the main layout's leaf zones (skip the Phone devicelayout)."""
    zones: List[Dict] = []
    buttons: List[Dict] = []
    seen = set()
    layout = db.find("zones")  # direct child -> excludes <devicelayouts>
    if layout is None:
        return zones, buttons
    # Map each zone id -> the set of its descendant zone ids. A Tableau toggle
    # button targets a CONTAINER zone (e.g. the filter drawer); the visuals it
    # shows/hides are the leaf zones nested inside that container.
    descendants: Dict[str, set] = {}
    for z in layout.iter("zone"):
        zid = X.attr(z, "id")
        if zid is None:
            continue
        descendants.setdefault(zid, set()).update(
            X.attr(c, "id") for c in z.iter("zone")
            if X.attr(c, "id") and X.attr(c, "id") != zid)
    for zone in layout.iter("zone"):
        zid = X.attr(zone, "id")
        if zid in seen:
            continue
        rect = {
            "x": X.int_attr(zone, "x"), "y": X.int_attr(zone, "y"),
            "w": X.int_attr(zone, "w"), "h": X.int_attr(zone, "h"),
        }
        button = zone.find("button")
        if button is not None:
            seen.add(zid)
            buttons.append(_button_record(button, rect))
            continue
        rec = _classify_zone(zone, ws_names, calc_map, param_map, rect)
        if rec is not None:
            seen.add(zid)
            rec["id"] = zid
            zones.append(rec)
    # Resolve each toggle button's container zone-ids down to the EMITTED leaf zone
    # ids it shows/hides, so the report layer can bind a bookmark to those visuals.
    emitted = {z.get("id") for z in zones}
    for b in buttons:
        if b.get("action") == "toggle" and b.get("zoneIds"):
            targets: set = set()
            for cid in b["zoneIds"]:
                cid = str(cid)
                if cid in emitted:
                    targets.add(cid)
                for d in descendants.get(cid, ()):
                    if d in emitted:
                        targets.add(d)
            b["targetZoneIds"] = sorted(
                targets, key=lambda s: int(s) if str(s).isdigit() else 0)
    return zones, buttons


def _classify_zone(zone: ET.Element, ws_names: set, calc_map: Dict,
                   param_map: Dict, rect: Dict) -> Optional[Dict]:
    """Return an IR zone record, or None for containers/spacers/images."""
    name = X.attr(zone, "name")
    ztype = (X.attr(zone, "type-v2") or X.attr(zone, "type") or "").lower()
    param = X.attr(zone, "param")
    if "filter" in ztype:
        return {"type": "filter", "worksheet": name, "field": F.resolve_ref(param, calc_map), **rect}
    if "paramctrl" in ztype or "parameter" in ztype:
        return {"type": "paramctrl", "worksheet": None, "field": F.resolve_param(param, param_map), **rect}
    # Encoding legend zones (color/size/shape) carry a worksheet name but are NOT
    # separate data visuals; rendering them duplicates the real worksheet visual.
    if ztype in ("color", "size", "shape", "legend", "measure-names"):
        return None
    if name and name in ws_names:
        return {"type": "viz", "worksheet": name, "field": None, **rect}
    if ztype == "text":
        return {"type": "text", "worksheet": None, "field": None, "text": _zone_text(zone), **rect}
    if "bitmap" in ztype or "image" in ztype:
        # Logo / picture object: keep the source path so the report can re-embed it.
        return {"type": "image", "worksheet": None, "field": None,
                "path": X.attr(zone, "param"), **rect}
    return None  # layout-basic/flow container or empty spacer -> not a data visual


def _zone_text(zone: ET.Element) -> str:
    """Concatenate formatted-text runs inside a text zone."""
    runs = [r.text or "" for r in zone.findall(".//formatted-text/run")]
    return X.clean_text(X.decode_entities(" ".join(runs)))


def _button_record(button: ET.Element, rect: Dict) -> Dict:
    action_raw = X.attr(button, "action", "") or ""
    toggle_el = button.find("toggle-action")
    toggle = toggle_el is not None
    state = button.find(".//button-visual-state")
    # Tableau stores the button label as a <tooltip-text> child element, not an
    # attribute (e.g. "Go to Sales Dashboard").
    tip_el = state.find(".//tooltip-text") if state is not None else None
    tooltip = (X.clean_text(X.decode_entities(tip_el.text))
               if tip_el is not None and tip_el.text else None)
    action = "toggle" if toggle else ("goto-sheet" if "goto-sheet" in action_raw else "other")
    # The window-id GUID lives INSIDE the action / toggle-action text, e.g.
    #   tabdoc:goto-sheet window-id="{GUID}"  /  toggle-button-click-action ... zone-ids=[6]
    src = action_raw + ((toggle_el.text or "") if toggle_el is not None else "")
    src = X.decode_entities(src)
    m = re.search(r'window-id="?\{?([0-9A-Fa-f-]+)\}?"?', src)
    window_id = m.group(1) if m else None
    zone_ids = None
    if toggle and toggle_el is not None and toggle_el.text:
        zm = re.search(r"zone-ids=\[([^\]]*)\]", X.decode_entities(toggle_el.text))
        if zm:
            zone_ids = [int(t) for t in re.findall(r"\d+", zm.group(1))]
    return {"action": action, "windowId": window_id, "zoneIds": zone_ids,
            "tooltip": tooltip, "label": tooltip, **rect}
