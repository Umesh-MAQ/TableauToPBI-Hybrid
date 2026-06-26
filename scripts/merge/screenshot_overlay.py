"""screenshot_overlay.py — fold the screenshot (vision) layer into the visual decisions.

This is the deterministic half of the "screenshots" feature. The DESIGN is:

  * The .twb XML stays the single source of truth for STRUCTURE and DATA — every
    table, column, measure, relationship and field BINDING is resolved from the IR
    (analysis.json), never from a screenshot. A screenshot is lossy for names (it
    shows the label 'Customer Name', not the model column 'Customer_Name'), so it
    must never drive a binding.

  * The screenshot is authoritative for VISUAL INTENT — the rendered chart type
    (a KPI tile with a sparkline vs a plain card), display formatting and colour —
    exactly the things the XML leaves ambiguous. A vision agent records those as
    ``visualHints`` in agent-fragment.json, keyed by worksheet.

  * The two are merged by AUTHORITY/PRECEDENCE, never by union — so nothing is
    duplicated. Each worksheet owns exactly one visual decision; a hint upgrades
    that decision's ``visualType`` (and records a discrepancy) instead of appending
    a second, competing visual for the same worksheet.

``discover_screenshots`` is the file-system convention; ``apply_visual_hints`` is
the precedence merge that ``merge_decisions.py`` calls.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

# Folder (singular or plural) a user drops a report's dashboard screenshots into.
_SCREENSHOT_DIRS = ("Screenshot", "Screenshots", "screenshots")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# Trailing state qualifier on a screenshot filename (the same dashboard captured
# with the filter panel open vs closed) — stripped so both map to one dashboard.
_STATE_SUFFIX = re.compile(
    r"[ _-]*(open|close[d]?|expanded|collapsed)?[ _-]*filter[s]?\s*$", re.IGNORECASE)


def _norm(name: str) -> str:
    """Loose key: lower-cased, alphanumeric only (so 'Sales Dashboard' ==
    'SalesDashboard' == 'sales_dashboard')."""
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


def _dashboard_from_filename(fname: str) -> str:
    """Infer the dashboard a screenshot belongs to from its file name.

    Convention: ``<Dashboard>_<state> filter.png`` (e.g. 'Sales Dashboard_Open
    filter.png', 'SalesDashboard_Close filter.png'). The state qualifier is
    optional; whatever remains after stripping it is the dashboard label.
    """
    stem = os.path.splitext(os.path.basename(fname))[0]
    stem = _STATE_SUFFIX.sub("", stem)
    stem = stem.rstrip(" _-")
    return stem.strip()


def discover_screenshots(data_dir: Optional[str]) -> Dict[str, List[str]]:
    """Map ``dashboard label -> [screenshot paths]`` for a report's data folder.

    Looks for a ``Screenshot(s)`` sub-folder; returns ``{}`` when none exists so
    every report without screenshots is a strict no-op.
    """
    out: Dict[str, List[str]] = {}
    if not data_dir or not os.path.isdir(data_dir):
        return out
    for sub in _SCREENSHOT_DIRS:
        folder = os.path.join(data_dir, sub)
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if os.path.splitext(fn)[1].lower() not in _IMG_EXT:
                continue
            dash = _dashboard_from_filename(fn) or "Dashboard"
            out.setdefault(dash, []).append(os.path.join(folder, fn))
    return out


def apply_visual_hints(visual_decisions: List[Dict],
                       hints: Optional[List[Dict]],
                       ir: Optional[Dict] = None) -> Tuple[List[Dict], List[Dict]]:
    """Overlay screenshot ``visualHints`` onto ``visualDecisions`` by precedence.

    A hint is ``{worksheet, visualType?, displayFormat?, note?}`` derived from the
    screenshot. Rules:
      * One decision per worksheet (deduped) — a hint upgrades the existing
        decision in place, it never appends a duplicate visual for the same sheet.
      * The screenshot wins for ``visualType`` (visual intent it can see); a change
        is logged as a discrepancy ``{worksheet, from, to, source, note}``.
      * Field bindings / data are untouched here — they stay deterministic.

    Returns ``(merged_decisions, discrepancies)``. No-op (returns the input list and
    an empty discrepancy list) when there are no hints.
    """
    merged = [dict(d) for d in (visual_decisions or [])]
    discrepancies: List[Dict] = []
    if not hints:
        return merged, discrepancies

    known_ws = {w.get("name") for w in (ir or {}).get("worksheets", [])} if ir else None
    by_ws = {_norm(d.get("worksheet")): d for d in merged}

    for h in hints:
        ws = h.get("worksheet")
        if not ws:
            continue
        # Ignore a hint that points at a worksheet the workbook does not contain
        # (a mis-typed or stale screenshot label) so a bad hint never invents a visual.
        if known_ws is not None and ws not in known_ws and _norm(ws) not in {
                _norm(n) for n in known_ws}:
            discrepancies.append({"worksheet": ws, "from": None, "to": None,
                                  "source": "screenshot", "note": "hint worksheet not in workbook"})
            continue
        new_type = h.get("visualType")
        existing = by_ws.get(_norm(ws))
        if existing is None:
            # No deterministic/agent decision for this sheet yet: the screenshot
            # supplies one. Single slot -> still no duplicate.
            if new_type:
                rec = {"worksheet": ws, "visualType": new_type,
                       "reason": h.get("note") or "screenshot-derived visual type"}
                merged.append(rec)
                by_ws[_norm(ws)] = rec
                discrepancies.append({"worksheet": ws, "from": None, "to": new_type,
                                      "source": "screenshot", "note": h.get("note")})
            continue
        if new_type and new_type != existing.get("visualType"):
            discrepancies.append({"worksheet": ws, "from": existing.get("visualType"),
                                  "to": new_type, "source": "screenshot",
                                  "note": h.get("note")})
            existing["visualType"] = new_type
            if h.get("note"):
                existing["reason"] = h["note"]
    return merged, discrepancies
