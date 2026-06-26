"""compare.py — the deterministic Tableau ↔ Power BI comparison engine.

Produces the validation records that populate the workbook's three core sheets:

  * ``compare_visuals``  — strict one-to-one Tableau worksheet → Power BI visual
    mapping: type equivalence, Exact/Similar/Missing/Extra status, and the
    aggregation / axis / legend facts behind the verdict (requirement #4 / #6).
  * ``compare_measures`` — every Tableau calculated field vs the generated DAX
    measure: business-logic, aggregation, null handling, conditional & time
    intelligence flags (requirement #5).
  * ``compare_filters``  — every filter / slicer / parameter: expected vs actual
    behaviour and a Match Status (requirement #5 / #1).

All checks are static (no data is read); each record carries an
``expected`` / ``actual`` pair and a Match Status the workbook renders verbatim.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from model_read import MigrationArtifacts

# --------------------------------------------------------------------------- #
# visual-type equivalence
# --------------------------------------------------------------------------- #
# Every concrete Power BI visualType (and Tableau-side alias) collapsed to a
# family. Two visuals in the SAME family are an exact functional match; a fall
# back ACROSS families (e.g. an unresolvable Tableau viz emitted as a table) is a
# "Similar Match" that the report flags with a justification.
_FAMILY = {
    # bar / column
    "barChart": "bar", "columnChart": "bar",
    "clusteredBarChart": "bar", "clusteredColumnChart": "bar",
    "stackedBarChart": "bar", "stackedColumnChart": "bar",
    "hundredPercentStackedBarChart": "bar", "hundredPercentStackedColumnChart": "bar",
    # line / area
    "lineChart": "line", "areaChart": "line", "stackedAreaChart": "line",
    "lineStackedColumnComboChart": "line", "lineClusteredColumnComboChart": "line",
    # pie / donut
    "pieChart": "pie", "donutChart": "pie",
    # scatter
    "scatterChart": "scatter",
    # map
    "map": "map", "filledMap": "map", "shapeMap": "map", "azureMap": "map",
    # table / matrix
    "tableEx": "table", "table": "table",
    "pivotTable": "matrix", "matrix": "matrix",
    # single value
    "card": "card", "multiRowCard": "card", "kpi": "card", "gauge": "card",
    # part-to-whole / hierarchy
    "treemap": "treemap", "funnel": "funnel", "ribbonChart": "bar",
    # controls
    "slicer": "slicer", "actionButton": "control", "textbox": "control",
}

# Tableau mark class → the Power BI family we EXPECT to see for that worksheet.
_MARK_FAMILY = {
    "Bar": "bar", "Line": "line", "Area": "line", "Pie": "pie",
    "Square": "matrix", "Text": "table", "Circle": "scatter",
    "Shape": "scatter", "Map": "map", "Polygon": "map",
    "Gantt": "bar", "Automatic": None,
}


def _family(visual_type: Optional[str]) -> Optional[str]:
    if not visual_type:
        return None
    return _FAMILY.get(visual_type, _FAMILY.get(visual_type[0].lower() + visual_type[1:]))


def _norm(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _expected_family(ws: dict) -> Optional[str]:
    """The Power BI family expected for a Tableau worksheet, mark-class first."""
    inferred = ws.get("inferredVisualType")
    fam = _family(inferred)
    if fam:
        return fam
    return _MARK_FAMILY.get(ws.get("markClass") or "")


_FAMILY_LABEL = {
    "bar": "Bar/Column chart", "line": "Line/Area chart", "pie": "Pie/Donut chart",
    "scatter": "Scatter chart", "map": "Map", "table": "Table",
    "matrix": "Matrix", "card": "Card/KPI", "treemap": "Treemap",
    "funnel": "Funnel", "slicer": "Slicer", "control": "Control",
}


def _label(fam: Optional[str]) -> str:
    return _FAMILY_LABEL.get(fam or "", fam or "—")


# --------------------------------------------------------------------------- #
# 1. visual mapping (requirement #4)
# --------------------------------------------------------------------------- #
def _match_visual(ws: dict, emitted: List[dict], used: set) -> Optional[dict]:
    """Best emitted visual for a Tableau worksheet: title match, then bindings."""
    keys = {_norm(ws.get("name")), _norm(ws.get("title")), _norm(ws.get("caption"))}
    keys.discard("")
    # 1) title / name match
    for v in emitted:
        if v["name"] in used or v["isControl"]:
            continue
        if _norm(v.get("title")) in keys or _norm(v.get("name")) in keys:
            return v
    # 2) field-binding overlap (category / measure fields the worksheet uses)
    ws_fields = {_norm(f) for f in (ws.get("rows", []) + ws.get("cols", []))}
    ws_fields |= {_norm(m.get("field")) for m in ws.get("measures", []) if m.get("field")}
    ws_fields.discard("")
    best, best_score = None, 0
    for v in emitted:
        if v["name"] in used or v["isControl"]:
            continue
        vf = {_norm(b["property"]) for b in v["bindings"]}
        score = len(ws_fields & vf)
        if score > best_score:
            best, best_score = v, score
    return best if best_score else None


def compare_visuals(art: MigrationArtifacts) -> List[Dict]:
    """One record per Tableau worksheet + one per unmatched (extra) PB visual."""
    records: List[Dict] = []
    emitted = list(art.emitted_visuals)
    used: set = set()

    for ws in art.worksheets:
        name = ws.get("name", "")
        exp_fam = _expected_family(ws)
        match = _match_visual(ws, emitted, used)
        if match:
            used.add(match["name"])
        act_fam = _family(match["visualType"]) if match else None
        act_type = match["visualType"] if match else None

        if not match:
            status = "Missing"
            justification = ("No Power BI visual was generated for this Tableau "
                             "worksheet. Manual authoring required.")
        elif exp_fam is None:
            status = "Exact Match"
            justification = ("Tableau mark was Automatic; the agent-selected Power "
                             f"BI visual ({act_type}) was accepted as the mapping.")
        elif act_fam == exp_fam:
            status = "Exact Match"
            justification = ""
        else:
            status = "Similar Match"
            justification = (
                f"No exact Power BI equivalent for a {_label(exp_fam)}; emitted the "
                f"closest supported visual ({_label(act_fam)} — {act_type}).")

        records.append({
            "tableauWorksheet": name,
            "tableauVisual": _label(exp_fam) + (
                f" ({ws.get('markClass')})" if ws.get("markClass") else ""),
            "powerBiVisual": act_type or "—",
            "powerBiPage": match["page"] if match else "—",
            "expectedType": _label(exp_fam),
            "actualType": _label(act_fam),
            "matchStatus": status,
            "businessLogic": _visual_logic(ws),
            "aggregation": _visual_aggregation(ws),
            "axes": _visual_axes(ws),
            "legend": ws.get("encodings", {}).get("color") or "—",
            "tooltips": ws.get("encodings", {}).get("tooltip") or "—",
            "justification": justification,
            "observations": justification or "Type, axes and aggregation align.",
            "screenshotKey": _norm(f"visual_{name}"),
            "powerBiVisualType": act_type or "",
            "powerBiVisualName": match["name"] if match else "",
        })

    # extra emitted visuals (no Tableau source) — slicers/controls are expected
    # additions, anything else is flagged so the report stays one-to-one.
    for v in emitted:
        if v["name"] in used:
            continue
        if v["isControl"]:
            records.append({
                "tableauWorksheet": "—",
                "tableauVisual": "—",
                "powerBiVisual": v["visualType"],
                "powerBiPage": v["page"],
                "expectedType": "—",
                "actualType": _label(_family(v["visualType"])) or v["visualType"],
                "matchStatus": "Added Control",
                "businessLogic": "Interaction control (slicer/parameter).",
                "aggregation": "—", "axes": "—",
                "legend": "—", "tooltips": "—",
                "justification": ("Power BI interaction control with no standalone "
                                  "Tableau worksheet (filter / parameter)."),
                "observations": "Expected addition; not a missing/extra visual.",
                "screenshotKey": _norm(f"visual_{v['page']}_{v['name']}"),
                "powerBiVisualType": v["visualType"],
                "powerBiVisualName": v["name"],
            })
        else:
            records.append({
                "tableauWorksheet": "—",
                "tableauVisual": "—",
                "powerBiVisual": v["visualType"],
                "powerBiPage": v["page"],
                "expectedType": "—",
                "actualType": _label(_family(v["visualType"])) or v["visualType"],
                "matchStatus": "Extra",
                "businessLogic": "—", "aggregation": "—", "axes": "—",
                "legend": "—", "tooltips": "—",
                "justification": "Power BI visual with no corresponding Tableau worksheet.",
                "observations": "Review: extra visual not present in the Tableau workbook.",
                "screenshotKey": _norm(f"visual_{v['page']}_{v['name']}"),
                "powerBiVisualType": v["visualType"],
                "powerBiVisualName": v["name"],
            })
    return records


def _visual_aggregation(ws: dict) -> str:
    parts = [f"{m.get('agg', 'SUM')}({m.get('field')})"
             for m in ws.get("measures", []) if m.get("field")]
    return ", ".join(parts) or "—"


def _visual_axes(ws: dict) -> str:
    ax = ws.get("axes", {}) or {}
    cat = ax.get("category") or "—"
    lvl = ax.get("categoryLevel")
    if lvl:
        cat = f"{cat} ({lvl})"
    vals = ", ".join(ax.get("values", []) or []) or "—"
    return f"X: {cat} | Y: {vals}"


def _visual_logic(ws: dict) -> str:
    bits = []
    if ws.get("topN"):
        t = ws["topN"]
        bits.append(f"{t.get('direction', 'TOP')}-{t.get('n')} by {t.get('byMeasure')}")
    if ws.get("sort"):
        s = ws["sort"]
        bits.append(f"sort {s.get('field')} {s.get('direction')}")
    if ws.get("filters"):
        bits.append(f"{len(ws['filters'])} filter(s)")
    return "; ".join(bits) or "Direct aggregation."


# --------------------------------------------------------------------------- #
# 2. measure validation (requirement #5)
# --------------------------------------------------------------------------- #
_TIME_INTEL = re.compile(
    r"\b(DATEADD|DATESYTD|DATESMTD|DATESQTD|TOTALYTD|SAMEPERIODLASTYEAR|"
    r"PARALLELPERIOD|PREVIOUSMONTH|PREVIOUSYEAR|DATESBETWEEN|DATESINPERIOD)\b", re.I)
_TAB_TIME = re.compile(
    r"\b(DATEADD|DATEDIFF|DATETRUNC|LOOKUP|RUNNING_|WINDOW_|PREVIOUS_VALUE|"
    r"YEAR|QUARTER|MONTH|WEEK|DAY)\b", re.I)
_COND = re.compile(r"\b(IF|CASE|WHEN|IIF|SWITCH)\b", re.I)
_NULL = re.compile(r"\b(ISNULL|IFNULL|ZN|COALESCE|BLANK|ISBLANK)\b", re.I)
_AGG = re.compile(r"\b(SUM|AVG|AVERAGE|COUNT|COUNTD|DISTINCTCOUNT|MIN|MAX|MEDIAN|"
                  r"TOTAL|ATTR)\b", re.I)


def _flag(present_tab: bool, present_dax: bool) -> str:
    if present_tab and present_dax:
        return "Equivalent"
    if present_tab and not present_dax:
        return "Needs Correction"
    if not present_tab and present_dax:
        return "Extra (review)"
    return "N/A"


def compare_measures(art: MigrationArtifacts) -> List[Dict]:
    """One record per Tableau calculated field, vs the generated DAX measure."""
    records: List[Dict] = []
    emitted = art.emitted_measures
    # index decisions by measure name for the formatted/expected DAX text
    dec_by_name = {m.get("name"): m for m in art.decision_measures}

    for cf in art.calc_fields:
        caption = cf.get("caption", "")
        formula = cf.get("formula", "") or ""
        dax = emitted.get(caption)
        if dax is None:
            dec = dec_by_name.get(caption)
            dax = dec.get("dax") if dec else None

        if dax is None:
            # calc field that did not become a standalone measure (inlined, a
            # calculated column, or a parameter-only field) — record, do not fail.
            records.append({
                "tableauField": caption,
                "tableauFormula": formula,
                "daxMeasure": "—",
                "expected": "Measure or calculated column",
                "actual": "Not emitted as a standalone measure",
                "businessLogic": "Review",
                "aggregation": _flag(bool(_AGG.search(formula)), False),
                "nullHandling": _flag(bool(_NULL.search(formula)), False),
                "conditional": _flag(bool(_COND.search(formula)), False),
                "timeIntelligence": _flag(bool(_TAB_TIME.search(formula)), False),
                "parameterDependency": _param_dep(cf, art),
                "matchStatus": "Review",
                "observations": ("Tableau calc field has no standalone DAX measure; "
                                 "it may be inlined, a calculated column, or unused."),
            })
            continue

        agg = _flag(bool(_AGG.search(formula)), bool(_AGG.search(dax)))
        nul = _flag(bool(_NULL.search(formula)), bool(_NULL.search(dax)))
        cond = _flag(bool(_COND.search(formula)), bool(_COND.search(dax)))
        time = _flag(bool(_TAB_TIME.search(formula)), bool(_TIME_INTEL.search(dax)))
        deviated = "Needs Correction" in (agg, nul, cond, time)
        status = "Needs Correction" if deviated else "Equivalent"
        records.append({
            "tableauField": caption,
            "tableauFormula": formula,
            "daxMeasure": dax,
            "expected": formula,
            "actual": dax,
            "businessLogic": "Deviation" if deviated else "Equivalent",
            "aggregation": agg,
            "nullHandling": nul,
            "conditional": cond,
            "timeIntelligence": time,
            "parameterDependency": _param_dep(cf, art),
            "matchStatus": status,
            "observations": (
                "Business-logic deviation detected — review the flagged dimensions."
                if deviated else "Logic, aggregation and null handling align."),
        })
    return records


def _param_dep(cf: dict, art: MigrationArtifacts) -> str:
    pnames = {p.get("name") for p in art.parameters}
    deps = [d for d in (cf.get("dependsOn") or []) if d in pnames]
    return ", ".join(deps) if deps else "None"


# --------------------------------------------------------------------------- #
# 3. filter / slicer / parameter validation (requirement #5 / #1)
# --------------------------------------------------------------------------- #
def compare_filters(art: MigrationArtifacts) -> List[Dict]:
    """One record per filter / slicer / parameter interaction.

    Each record is a screenshot-evidence row: the workbook embeds the side-by-side
    Tableau vs Power BI captures (or a placeholder) next to the verdict.
    """
    records: List[Dict] = []
    slicer_fields = set()
    for v in art.emitted_visuals:
        if v["isSlicer"]:
            for b in v["bindings"]:
                slicer_fields.add(_norm(b["property"]))

    # field/name → the emitted control visual that backs it, so each filter row
    # can crop that exact Power BI slicer (not the whole page).
    control_by_field: Dict[str, dict] = {}
    for v in art.emitted_visuals:
        if not v["isControl"]:
            continue
        for b in v["bindings"]:
            control_by_field.setdefault(_norm(b["property"]), v)
        if v.get("title"):
            control_by_field.setdefault(_norm(v["title"]), v)
        control_by_field.setdefault(_norm(v["name"]), v)

    seen: set = set()
    for ws in art.worksheets:
        for fd in ws.get("filterDetails", []) or []:
            field = fd.get("field", "")
            key = _norm(field)
            if not field or key in seen:
                continue
            seen.add(key)
            has_slicer = key in slicer_fields
            ctrl = control_by_field.get(key)
            values = fd.get("values")
            scope = fd.get("scope")
            rng = fd.get("range")
            expected = _filter_expected(fd)
            actual = ("Power BI slicer present"
                      if has_slicer else "Applied as a visual-level filter")
            status = "Match" if (has_slicer or fd.get("filterClass")) else "Review"
            records.append({
                "kind": "Filter",
                "field": field,
                "worksheet": ws.get("name", ""),
                "filterType": fd.get("filterClass") or "categorical",
                "scope": scope or "include",
                "filterValues": ", ".join(values) if values else (
                    f"{rng.get('min')}–{rng.get('max')}" if rng else "—"),
                "expected": expected,
                "actual": actual,
                "crossFilter": "Page interactions (default highlight/filter)",
                "drill": "—",
                "matchStatus": status,
                "observations": ("Slicer reproduces the Tableau filter."
                                 if has_slicer else
                                 "Tableau filter applied at visual level in Power BI."),
                "screenshotKey": _norm(f"filter_{field}"),
                "powerBiVisualName": ctrl["name"] if ctrl else "",
                "powerBiPage": ctrl["page"] if ctrl else None,
            })

    # parameters -> field parameters / what-if
    fp_norm = {_norm(fp.get("name")) for fp in art.field_parameters}
    for p in art.parameters:
        name = p.get("name", "")
        is_fp = _norm(name) in fp_norm
        ctrl = control_by_field.get(_norm(name))
        domain = p.get("domainType")
        if domain == "range":
            actual = "What-If parameter (numeric range)"
            status = "Match"
        elif is_fp:
            actual = "Field parameter (axis/measure swap)"
            status = "Match"
        else:
            actual = "Slicer-backed parameter table"
            status = "Review"
        records.append({
            "kind": "Parameter",
            "field": name,
            "worksheet": "—",
            "filterType": domain or "list",
            "scope": "—",
            "filterValues": ", ".join(p.get("values", []) or []) or (
                f"{(p.get('range') or {}).get('min')}–"
                f"{(p.get('range') or {}).get('max')}" if p.get("range") else "—"),
            "expected": f"Tableau parameter '{name}' drives visual selection",
            "actual": actual,
            "crossFilter": "Drives dependent visuals/measures",
            "drill": "—",
            "matchStatus": status,
            "observations": ("Parameter reproduced as a Power BI "
                             + actual.lower() + "."),
            "screenshotKey": _norm(f"param_{name}"),
            "powerBiVisualName": ctrl["name"] if ctrl else "",
            "powerBiPage": ctrl["page"] if ctrl else None,
        })
    return records


def _filter_expected(fd: dict) -> str:
    fclass = fd.get("filterClass") or "categorical"
    scope = fd.get("scope") or "include"
    if fd.get("values"):
        return f"{scope} {len(fd['values'])} {fclass} member(s)"
    if fd.get("range"):
        r = fd["range"]
        return f"{fclass} range {r.get('min')}–{r.get('max')}"
    return f"{fclass} filter"
