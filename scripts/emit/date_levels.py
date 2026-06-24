"""date_levels.py — shared date-part naming + DAX for the TMDL/PBIR emitters.

Tableau places date fields on shelves at a truncated grain (year/quarter/month/
week) rather than the exact day. To reproduce that aggregation in Power BI we
emit a calculated date-part column on the fact table and bind the visual to it.
Both emit_tmdl (model) and emit_pbir (report) import this module so the column
name they generate and reference is always identical.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

_SUFFIX = {"year": "Year", "quarter": "Quarter", "month": "Month", "week": "Week"}


def needs_part(level: Optional[str]) -> bool:
    """True when the level requires a derived column (day/None bind to raw date)."""
    return level in _SUFFIX


def part_column_name(col: str, level: Optional[str]) -> str:
    """Deterministic derived-column name, e.g. 'Census Date (Month)'."""
    return f"{col} ({_SUFFIX[level]})" if needs_part(level) else col


def part_dax(col: str, table: str, level: str) -> str:
    """DAX expression for a date-part calculated column."""
    c = f"'{table}'[{col}]"
    if level == "year":
        return f"YEAR({c})"
    if level == "quarter":
        return f'"Q" & ROUNDUP(MONTH({c}) / 3, 0) & " " & YEAR({c})'
    if level == "month":
        return f"DATE(YEAR({c}), MONTH({c}), 1)"
    if level == "week":
        return f"{c} - WEEKDAY({c}, 2) + 1"
    return c


def part_format(level: str) -> Optional[str]:
    """formatString for the derived column (None = inherit default)."""
    return {"year": "0", "month": "mmm yyyy", "week": "mmm d"}.get(level)


def part_datatype(level: str) -> str:
    """IR dataType for the derived column."""
    return {"year": "integer", "quarter": "string",
            "month": "datetime", "week": "datetime"}.get(level, "string")


def needed_parts(worksheets: List[Dict], date_cols: Set[str]) -> List[Dict]:
    """Collect unique (column, level) date parts required across all worksheets.

    Scans each worksheet's category date level (and any date dimension) so the
    model emits exactly the derived columns the report will reference.
    """
    seen = set()
    parts: List[Dict] = []
    for ws in worksheets:
        level = ws.get("categoryDateLevel")
        if not needs_part(level):
            continue
        for col in _date_dims(ws, date_cols):
            key = (col, level)
            if key in seen:
                continue
            seen.add(key)
            parts.append({
                "name": part_column_name(col, level), "baseColumn": col,
                "level": level, "dataType": part_datatype(level),
                "format": part_format(level),
            })
    return parts


def _date_dims(ws: Dict, date_cols: Set[str]) -> List[str]:
    """Date dimensions in a worksheet (category first, then other date dims)."""
    out: List[str] = []
    cat = ws.get("categoryField")
    if cat in date_cols:
        out.append(cat)
    for d in ws.get("dimensions", []) or []:
        if d in date_cols and d not in out:
            out.append(d)
    return out
