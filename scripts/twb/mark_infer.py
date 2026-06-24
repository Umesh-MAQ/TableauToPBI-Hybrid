"""mark_infer.py — deterministic Tableau mark -> Power BI visualType inference.

Single source of truth shared by the parser (which stamps inferredVisualType into
the IR so genuinely-resolvable worksheets never reach the agent) and the report
emitter (which falls back to it when no decision exists). Pure stdlib; uses only
verbatim parser facts (markClass, resolved field counts, encodings, orientation),
never the raw XML. Returns None when the shape is still ambiguous — an 'Automatic'
mark whose layout cannot be resolved, OR a worksheet that would only be FLATTENED
into a table because it stacks 2+ shelf dimensions with no measure — leaving that
worksheet for the LLM/decisions layer instead of silently guessing a table.
"""
from __future__ import annotations

from typing import Dict, Optional

# Unambiguous Tableau mark class -> Power BI visualType.
MARK_MAP = {
    "Bar": "barChart", "Line": "lineChart", "Area": "areaChart", "Pie": "pieChart",
    "Square": "treemap", "Circle": "scatterChart", "Text": "tableEx",
    "Gantt": "ganttChart", "Map": "map",
}
MAP_MARKS = {"Map", "Multipolygon", "Polygon", "Filled Map"}
# Combo (Tableau dual-axis) = a bar-type pane AND a line-type pane on one sheet.
_BAR_MARKS = {"Bar", "Gantt"}
_LINE_MARKS = {"Line"}


def _is_combo(ws: Dict) -> bool:
    """True when the worksheet is a Tableau dual-axis bar+line combo.

    Detected from the per-pane mark classes: dual-axis produces two panes, one
    drawn as Bar and one as Line, over a shared category with >= 2 measures.
    """
    pm = ws.get("paneMarks") or []
    has_bar = any(m in _BAR_MARKS for m in pm)
    has_line = any(m in _LINE_MARKS for m in pm)
    n_dim = len(ws.get("dimensions") or [])
    n_val = len(ws.get("values") or [])
    return has_bar and has_line and n_dim >= 1 and n_val >= 2


def _ambiguous_flat_table(ws: Dict) -> bool:
    """True when a worksheet would only be FLATTENED into a table because it
    stacks two or more shelf dimensions (rows + cols) with no measure to plot.

    The deterministic heuristic cannot tell what such a worksheet should be (a
    matrix, a multi-level list, or which value to show), so it routes to the agent
    rather than silently emitting a guessed flat table. NOT ambiguous (these stay a
    confident ``tableEx``): a single-dimension value list (slicer / legend / filter
    control the emitter handles deterministically) and any worksheet that carries a
    measure value (a genuine detail table or cross-tab).
    """
    placed = len(ws.get("rows") or []) + len(ws.get("cols") or [])
    has_value = bool(ws.get("values")) or bool(ws.get("measures"))
    return placed >= 2 and not has_value


def infer_visual_type(ws: Dict) -> Optional[str]:
    """Derive a Power BI visualType from a worksheet's Tableau mark FACTS.

    Returns None when the mark is 'Automatic' and the shape is still ambiguous.
    """
    mark_class = ws.get("markClass") or "Automatic"
    if mark_class in MAP_MARKS:
        return "map"
    if mark_class == "Text":
        # A Text mark is normally a value/detail table, but a multi-dimension
        # text sheet with no measure is a genuine layout guess -> hand to agent.
        return None if _ambiguous_flat_table(ws) else "tableEx"
    # Dual-axis bar+line takes precedence over the first pane's mark class (which
    # would otherwise read as a plain Bar/Line chart and drop the second measure).
    if _is_combo(ws):
        return "comboChart"
    # A Circle mark is a scatter ONLY when two measures give it an X and a Y. With
    # a single measure split by a dimension (Tableau packed-bubbles / proportional
    # circles) there is no second axis, so Power BI's closest faithful render is a
    # pie -- one slice per dimension member, sized by the measure. Without this the
    # scatter fallback finds no second measure and collapses to a detail table.
    if (ws.get("markClass") == "Circle"
            and len(ws.get("values") or []) == 1
            and len(ws.get("dimensions") or []) >= 1):
        return "pieChart"
    if mark_class in MARK_MAP:
        return MARK_MAP[mark_class]
    enc = ws.get("encodings") or {}
    n_dim = len(ws.get("dimensions") or [])
    n_val = len(ws.get("values") or [])
    has_date = ws.get("categoryDateLevel") is not None
    orientation = ws.get("orientation")
    if mark_class == "Automatic":
        if n_dim == 0 and n_val == 0:
            return "card"
        if enc.get("color") and enc.get("size") and enc.get("text"):
            return "treemap"
        if n_dim == 0 and n_val >= 1:
            return "card"
        if n_val >= 2 and n_dim >= 1:
            # Multiple measures plotted against one-or-more dimensions is genuinely
            # ambiguous: it could be a multi-series line, a combo chart, a matrix or
            # a detail table. Returning None routes the worksheet to the agent for a
            # real decision (emit keeps a table only as the last-resort fallback)
            # instead of silently guessing a table here.
            return None
        if has_date and n_val >= 1:
            return "lineChart"
        if n_dim >= 1 and n_val >= 1:
            return "columnChart" if orientation == "vertical" else "barChart"
        if n_dim >= 1 and n_val == 0:
            # Dimensions only, no measure: a single-dimension list is a confident
            # slicer/list (Phase-26 emitter handles it); 2+ stacked dimensions is
            # an ambiguous flat dump -> route to the agent rather than guess.
            return None if _ambiguous_flat_table(ws) else "tableEx"
    return None


# Visual types that plot a measure VALUE (every one of these needs a number to
# draw; all except the KPI card also need a category axis). When a worksheet
# resolves to one of these but exposes no usable value, the emitter has nothing
# real to bind and falls back to the first model measure / dimension column — an
# arbitrary, likely-wrong guess. We escalate that case to the agent instead.
_VALUE_CHARTS = {
    "barChart", "columnChart", "lineChart", "areaChart", "pieChart",
    "treemap", "scatterChart", "ganttChart", "map", "comboChart",
}


def _has_plotted_value(ws: Dict) -> bool:
    """True when a worksheet carries a real measure to plot — a measure pill, a
    field on the values shelf, or a resolved value field."""
    return bool(ws.get("measures") or ws.get("values") or ws.get("valueField"))


def _category_is_guess(ws: Dict, cols) -> bool:
    """True when a worksheet EXPRESSES a category but the emitter could only GUESS
    it — the category field is not a real model column, there is no date grain, and
    no real dimension sits on the rows/cols shelf. In that case the deterministic
    binder falls back to the first dimension column, plotting an arbitrary axis.

    Returns False when there is no category intent at all (the emitter flips such a
    value-only worksheet to a KPI card, which is a confident render, not a guess).
    """
    if cols is None:
        return False
    catf = ws.get("categoryField")
    vals = set(ws.get("values") or [])
    shelf_dims = [f for f in (ws.get("rows") or []) + (ws.get("cols") or [])
                  if f not in vals]
    if not (catf or shelf_dims):
        return False  # no category intent -> emitted as a card, not guessed

    def _real(field) -> bool:
        # Accept a federated alias ('state (state_region.csv)') by its base name.
        return bool(field) and (field in cols or field.split(" (")[0] in cols)

    if ws.get("categoryDateLevel") is not None:
        return False
    if _real(catf):
        return False
    if any(_real(f) for f in shelf_dims):
        return False
    return True


def binding_needs_agent(ws: Optional[Dict], cols=None) -> bool:
    """True when a worksheet's RESOLVED chart type cannot be bound deterministically
    without guessing, so it must be routed to the agent for a better binding.

    This is the binding-level safety net behind the type-level inference above.
    Output quality is the priority: even when the chart TYPE is known, the
    deterministic emitter still has to bind a category and a value from the
    worksheet's real fields. When the needed field is absent it falls back to
    ``first_dim_col`` / the first model measure, plotting an arbitrary axis. Rather
    than emit that guess, we surface the worksheet as ambiguous so the agent
    authors the binding (a strictly better output wherever it would otherwise be a
    guess).

    Escalates two genuinely-unbindable cases:
      * a chart that needs a measure value but the worksheet exposes none;
      * a chart that needs a category but the category could only be guessed (the
        named field is not a real column and no date grain / real shelf dimension
        is available) — requires ``cols`` (the set of model column names).

    Conservative where the deterministic render IS confident — it never escalates:
      * an ambiguous type (``inferredVisualType is None``; already gated), a detail
        table (``tableEx``) or a KPI card (``card``);
      * a caption worksheet (the emitter renders it as a textbox, not a chart);
      * a value-bearing chart with no category at all (flipped to a card).
    """
    if not ws:
        return False
    if ws.get("inferredVisualType") not in _VALUE_CHARTS:
        return False
    if ws.get("caption"):
        return False
    if not _has_plotted_value(ws):
        return True
    return _category_is_guess(ws, cols)
