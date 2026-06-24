"""field_param.py — Power BI field-parameter TMDL builder + report helpers.

A field parameter is a calculated table of (label, NAMEOF(field), order) rows.
A slicer on its first column lets the user swap which field a visual uses on an
axis. This reproduces Tableau parameter-driven toggles (e.g. Date Agg Level
switching a chart between daily and monthly date fields) deterministically.

Both emit_tmdl (emits the table) and emit_pbir (binds the chart axis + slicer)
import this module so the generated names always match.
"""
from __future__ import annotations

from typing import Dict, List, Optional

TAB = "\t"


def _lineage(seq: int) -> str:
    return f"a1000000-0000-4000-9000-{seq:012x}"


def _q(name: str) -> str:
    if name and (name[0].isdigit() or any(c in name for c in " '\"-./()&")):
        return f"'{name}'"
    return name


def value_column(fp: Dict) -> str:
    """Display column name the slicer binds to (same as the parameter name)."""
    return fp["name"]


def applies_to(fp: Dict) -> List[str]:
    return fp.get("appliesTo", []) or []


def primary_worksheet(fp: Dict) -> Optional[str]:
    """The single worksheet kept live for this parameter (others suppressed)."""
    ws = applies_to(fp)
    return ws[0] if ws else None


def suppressed_worksheets(field_params: List[Dict]) -> set:
    """All non-primary worksheets across every field parameter (skip emitting)."""
    out = set()
    for fp in field_params or []:
        for extra in applies_to(fp)[1:]:
            out.add(extra)
    return out


def table_tmdl(fp: Dict, seq: int) -> str:
    """Emit the full TMDL for one field-parameter calculated table."""
    name = fp["name"]
    fields = fp["fields"]
    rows = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}("{f["label"]}", '
        f"NAMEOF('{f['table']}'[{f['column']}]), {i})"
        for i, f in enumerate(fields)
    )
    lines = [f"table {_q(name)}", f"{TAB}lineageTag: {_lineage(seq)}", ""]
    lines.append(_param_column(name, name, 0, seq * 10 + 1, is_key=False))
    lines.append("")
    lines.append(_fields_column(name, seq * 10 + 2))
    lines.append("")
    lines.append(_order_column(name, seq * 10 + 3))
    lines.append("")
    lines.append(
        f"{TAB}partition {_q(name)} = calculated\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}{{\n{rows}\n{TAB}{TAB}{TAB}}}\n"
    )
    return "\n".join(lines)


def _param_column(table: str, name: str, ordinal: int, seq: int, is_key: bool) -> str:
    meta = '{"version":3,"kind":2}'
    return (
        f"{TAB}column {_q(name)}\n"
        f"{TAB}{TAB}lineageTag: {_lineage(seq)}\n"
        f"{TAB}{TAB}summarizeBy: none\n"
        f"{TAB}{TAB}sourceColumn: [Value{ordinal + 1}]\n"
        f"{TAB}{TAB}sortByColumn: {_q(name + ' Order')}\n\n"
        f"{TAB}{TAB}relatedColumnDetails\n"
        f"{TAB}{TAB}{TAB}groupByColumn: {_q(name + ' Fields')}\n\n"
        f'{TAB}{TAB}extendedProperty ParameterMetadata =\n'
        f"{TAB}{TAB}{TAB}{meta}\n\n"
        f"{TAB}{TAB}annotation SummarizationSetBy = Automatic"
    )


def _fields_column(name: str, seq: int) -> str:
    col = name + " Fields"
    return (
        f"{TAB}column {_q(col)}\n"
        f"{TAB}{TAB}lineageTag: {_lineage(seq)}\n"
        f"{TAB}{TAB}summarizeBy: none\n"
        f"{TAB}{TAB}sourceColumn: [Value2]\n"
        f"{TAB}{TAB}sortByColumn: {_q(name + ' Order')}\n"
        f"{TAB}{TAB}isHidden\n\n"
        f'{TAB}{TAB}extendedProperty ParameterMetadata =\n'
        f'{TAB}{TAB}{TAB}{{"version":3,"kind":2}}\n\n'
        f"{TAB}{TAB}annotation SummarizationSetBy = Automatic"
    )


def _order_column(name: str, seq: int) -> str:
    col = name + " Order"
    return (
        f"{TAB}column {_q(col)}\n"
        f"{TAB}{TAB}dataType: int64\n"
        f"{TAB}{TAB}formatString: 0\n"
        f"{TAB}{TAB}lineageTag: {_lineage(seq)}\n"
        f"{TAB}{TAB}summarizeBy: sum\n"
        f"{TAB}{TAB}sourceColumn: [Value3]\n"
        f"{TAB}{TAB}isHidden\n\n"
        f"{TAB}{TAB}annotation SummarizationSetBy = Automatic"
    )
