"""map_dax.py — Stage 6 hybrid Tableau-calc -> DAX translator.

Deterministically translates the *trivial* majority of Tableau calculated fields
into DAX measures via an ordered handler registry (PATTERN_REGISTRY):

  * single aggregations          SUM([x])                 -> SUM ( T[x] )
  * population stats              STDEVP([x]) / VARP([x])  -> STDEV.P / VAR.P
  * single-value display         ATTR([x])                -> SELECTEDVALUE ( T[x] ) (gated)
  * SUM/SUM ratios               SUM([a]) / SUM([b])      -> DIVIDE(...)
  * any-aggregation ratios       COUNTD([a]) / SUM([b])   -> DIVIDE(...)        (gated)
  * aggregation arithmetic       (SUM([a])-SUM([b]))/SUM([b])                   (gated)
  * column pass-through          [Field]                  -> T[Field]
  * safe expressions             IF/IIF/CASE, AND/OR/NOT, comparisons, and a
                                 whitelist of scalar/date/string/agg functions
                                 over base columns                             (gated)

The remainder (LOD, table-calcs, parameter-driven logic, calc-field/param references)
is reported as "complex" so the agent translates only those. Column-gated handlers
only fire when every referenced field is a real base column, so they never emit DAX
referencing a non-existent column. Output is decisions-ready measure fragments
written to dax-partial.json.

Usage:
    python map_dax.py Output/MidnightCensus/analysis.json --table MidnightCensus
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dax_expr as E  # noqa: E402
import dax_cache  # noqa: E402  # learned formula->DAX "notepad" (graduation layer 1)

# Single-aggregation patterns: Tableau func -> DAX func
AGG_MAP = {
    "SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
    "MEDIAN": "MEDIAN", "COUNT": "COUNT", "COUNTD": "DISTINCTCOUNT",
    "STDEV": "STDEV.S", "STDEVP": "STDEV.P",
    "VAR": "VAR.S", "VARP": "VAR.P",
}

FORMAT_BY_AGG = {
    "COUNT": "#,0", "DISTINCTCOUNT": "#,0", "SUM": "#,0",
    "AVERAGE": "#,0.00", "MEDIAN": "#,0.00",
}

# Aggregation-keyword alternation, shared by every aggregation regex below.
# Longer keywords come first so e.g. STDEVP wins over STDEV and COUNTD over COUNT.
_AGG_ALT = r"SUM|AVG|MIN|MAX|MEDIAN|COUNTD|COUNT|STDEVP|STDEV|VARP|VAR"

# Anything containing these tokens is NOT trivially translatable; it is routed to
# the agent. CASE / ELSEIF are intentionally NOT here -- the gated expression
# handler (_h_expression) translates them when they reference only base columns,
# and bails (-> agent) otherwise.
COMPLEX_TOKENS = re.compile(
    r"\{|\bFIXED\b|\bINCLUDE\b|\bEXCLUDE\b|WINDOW_|RUNNING_|\bINDEX\b|\bRANK\b|"
    r"\bLOOKUP\b|\bPREVIOUS_VALUE\b|DATEPARSE|DATEADD|DATETRUNC",
    re.IGNORECASE,
)

SINGLE_AGG_RE = re.compile(
    rf"^\s*({_AGG_ALT})\s*\(\s*\[([^\]]+)\]\s*\)\s*$",
    re.IGNORECASE,
)
RATIO_RE = re.compile(
    r"^\s*SUM\s*\(\s*\[([^\]]+)\]\s*\)\s*/\s*SUM\s*\(\s*\[([^\]]+)\]\s*\)\s*$",
    re.IGNORECASE,
)
PASSTHROUGH_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# ATTR([Field]) -> SELECTEDVALUE ( T[Field] ): show the single value of a dimension.
ATTR_RE = re.compile(r"^\s*ATTR\s*\(\s*\[([^\]]+)\]\s*\)\s*$", re.IGNORECASE)
# A lone parameter reference, optionally with trailing scalar arithmetic against a
# constant: [Select Year], [Select Year]-1, [Select Year] + 2, [Select Year]*100.
PARAM_REF_RE = re.compile(
    r"^\s*\[([^\]]+)\]\s*(?:([+\-*/])\s*(\d+(?:\.\d+)?))?\s*$")
# A bare positive integer constant -- the Tableau "Number of Records" row-count
# idiom (the legacy field is literally defined as 1 and aggregated with SUM).
CONST_INT_RE = re.compile(r"^\s*(\d+)\s*$")

# Any single "AGG([Field])" token anywhere in a formula (used by the generalized
# ratio / arithmetic handlers below).
AGG_TOKEN_RE = re.compile(
    rf"\b({_AGG_ALT})\s*\(\s*\[([^\]]+)\]\s*\)",
    re.IGNORECASE,
)
# Exactly "AGG([a]) / AGG([b])" with possibly different aggregations.
ANY_RATIO_RE = re.compile(
    rf"^\s*({_AGG_ALT})\s*\(\s*\[([^\]]+)\]\s*\)"
    r"\s*/\s*"
    rf"({_AGG_ALT})\s*\(\s*\[([^\]]+)\]\s*\)\s*$",
    re.IGNORECASE,
)
# After agg tokens are stripped, a pure arithmetic residual contains only these.
_ARITH_RESIDUAL_RE = re.compile(r"^[\s0-9.+\-*/()]*$")


def _h_single_agg(formula, table, columns, measures):
    """SUM([x]) -> SUM ( T[x] ).

    Column-gated when a column set is supplied: aggregating a token that is not a
    real base column (a sibling calc-field *measure* or a parameter) is invalid
    DAX -- e.g. SUM([Normalized Amount]) where ``Normalized Amount`` is itself a
    measure becomes SUM ( T[Normalized Amount] ), a reference to a non-existent
    column. Such formulas defer to the agent instead. Stays ungated when no column
    set is supplied (back-compat).
    """
    m = SINGLE_AGG_RE.match(formula)
    if not m:
        return None
    if columns and m.group(2) not in columns:
        return None
    func = AGG_MAP[m.group(1).upper()]
    return f"{func} ( {table}[{m.group(2)}] )", FORMAT_BY_AGG.get(func)


def _h_ratio_sum(formula, table, columns, measures):
    """SUM([a]) / SUM([b]) -> DIVIDE(...).

    Column-gated when a column set is supplied: both operands must be real base
    columns, so a ratio over a sibling calc-field *measure* (e.g.
    SUM([profit]) / SUM([Normalized Amount])) never emits
    SUM ( T[Normalized Amount] ) against a non-existent column -- it defers to the
    agent, which references the measure as a bare [Normalized Amount]. Stays
    ungated when no column set is supplied (back-compat).
    """
    m = RATIO_RE.match(formula)
    if not m:
        return None
    if columns and (m.group(1) not in columns or m.group(2) not in columns):
        return None
    dax = f"DIVIDE ( SUM ( {table}[{m.group(1)}] ), SUM ( {table}[{m.group(2)}] ) )"
    return dax, "#,0.00"


def _h_ratio_general(formula, table, columns, measures):
    """AGG([a]) / AGG([b]) (any aggregations) -> DIVIDE(...).

    Column-gated: only fires when both fields are real base columns, so it never
    emits DAX referencing a non-existent column (e.g. a Tableau calc-field token).
    """
    if not columns:
        return None
    m = ANY_RATIO_RE.match(formula)
    if not m:
        return None
    a_field, b_field = m.group(2), m.group(4)
    if a_field not in columns or b_field not in columns:
        return None
    fa = AGG_MAP[m.group(1).upper()]
    fb = AGG_MAP[m.group(3).upper()]
    dax = f"DIVIDE ( {fa} ( {table}[{a_field}] ), {fb} ( {table}[{b_field}] ) )"
    return dax, "#,0.00"


def _h_agg_arithmetic(formula, table, columns, measures):
    """Linear arithmetic over base-column aggregations, e.g.
    (SUM([a]) - SUM([b])) / SUM([b]) -> ( SUM ( T[a] ) - SUM ( T[b] ) ) / SUM ( T[b] ).

    Column-gated and structurally guarded: every aggregated field must be a real
    base column AND the residual (formula minus agg tokens) must be pure arithmetic
    (digits / operators / parens only). Any stray identifier, function call, or
    calc-field reference makes the residual non-arithmetic, so the handler bails to
    the agent rather than emitting broken DAX.
    """
    if not columns:
        return None
    aggs = list(AGG_TOKEN_RE.finditer(formula))
    if not aggs:
        return None
    for m in aggs:
        if m.group(2) not in columns:
            return None
    residual = AGG_TOKEN_RE.sub("", formula)
    if residual.strip() == "" or not _ARITH_RESIDUAL_RE.match(residual):
        # Empty residual is a bare aggregation (handled by _h_single_agg); a
        # non-arithmetic residual means there's logic we can't safely translate.
        return None

    def _repl(m):
        func = AGG_MAP[m.group(1).upper()]
        return f"{func} ( {table}[{m.group(2)}] )"

    dax = re.sub(r"\s+", " ", AGG_TOKEN_RE.sub(_repl, formula)).strip()
    fmt = "#,0.00" if "/" in formula else None
    return dax, fmt


def _h_attr(formula, table, columns, measures):
    """ATTR([Field]) -> SELECTEDVALUE ( T[Field] ).

    Column-gated when a column set is supplied so it never references a Tableau
    calc-field token or parameter.
    """
    m = ATTR_RE.match(formula)
    if not m:
        return None
    field = m.group(1)
    if columns and field not in columns:
        return None
    return f"SELECTEDVALUE ( {table}[{field}] )", None


def _h_passthrough(formula, table, columns, measures):
    """[Field] -> T[Field].

    Column-gated when a column set is supplied: a bare ``[token]`` that is NOT a
    real base column (a parameter or a calc-field alias) must never become a
    bare-column measure -- that is invalid DAX (Power BI cannot resolve a single
    value) and fails the model load. Such tokens defer to the parameter handler or
    the agent instead.
    """
    m = PASSTHROUGH_RE.match(formula)
    if not m:
        return None
    if columns and m.group(1) not in columns:
        return None
    return f"{table}[{m.group(1)}]", None


def _h_row_count(formula, table, columns, measures):
    """Bare integer constant (e.g. ``1``) -> COUNTROWS row-count measure.

    Tableau's legacy 'Number of Records' calc field is defined as the constant 1
    and aggregated with SUM, which yields the datasource row count. SUM of a
    constant N equals N * COUNTROWS(table); for the canonical N==1 this is just
    COUNTROWS(table). Homed on the host (fact) table so the row-count measure is
    valid, deterministic DAX instead of being routed to the agent. Constants never
    reference a column, so this is independent of the column set.
    """
    m = CONST_INT_RE.match(formula)
    if not m:
        return None
    tok = table if re.fullmatch(r"\w+", table) else f"'{table}'"
    n = int(m.group(1))
    dax = f"COUNTROWS ( {tok} )" if n == 1 else f"{n} * COUNTROWS ( {tok} )"
    return dax, "#,0"


def _param_default_literal(param: Dict) -> str:
    """DAX literal for a parameter's default value (numeric bare, else quoted)."""
    default = param.get("default")
    dtype = (param.get("dataType") or "").lower()
    numeric = dtype in ("integer", "real", "float", "number")
    if default in (None, ""):
        return "0" if numeric else '""'
    if numeric:
        try:
            f = float(default)
            return str(int(f)) if f.is_integer() else str(f)
        except (TypeError, ValueError):
            return f'"{default}"'
    return f'"{default}"'


def _h_parameter_ref(formula, table, parameters):
    """[Param] (optionally <op> N) -> SELECTEDVALUE('Param'[Param], default) <op> N.

    Tableau parameter-alias calc fields (e.g. a 'Current Year' calc = [Select Year]
    or 'Previous Year' = [Select Year]-1) must become a scalar measure, not a bare
    column reference. SELECTEDVALUE resolves the parameter table's single selected
    value, falling back to the parameter's default so the measure stays valid with
    no slicer selection. This keeps such fields deterministic so they are never
    skipped or routed to the agent.
    """
    if not parameters:
        return None
    m = PARAM_REF_RE.match(formula)
    if not m:
        return None
    param = parameters.get(m.group(1))
    if param is None:
        return None
    name = m.group(1)
    dax = f"SELECTEDVALUE ( '{name}'[{name}], {_param_default_literal(param)} )"
    if m.group(2):  # trailing scalar arithmetic against a constant
        dax = f"{dax} {m.group(2)} {m.group(3)}"
    fmt = "0" if (param.get("dataType") or "").lower() == "integer" else None
    return dax, fmt


def _h_expression(formula, table, columns, measures):
    """Safe IF/IIF/CASE + operators + whitelisted-function expressions -> DAX.

    Delegated to the fail-closed recursive translator in dax_expr. It only fires
    when every ``[field]`` is a real base column or a sibling measure (and at least
    one reference appears), so it never references a parameter, column-kind
    calc-field token, or non-existent column; anything outside the safe subset
    raises Untranslatable and defers to the agent.
    """
    if not columns and not measures:
        return None
    try:
        return E.translate_expression(formula, table, columns, measures), None
    except E.Untranslatable:
        return None


# Ordered registry of deterministic handlers. Each handler returns
# (dax, formatString) on a match or None to defer to the next handler. The
# specialized handlers run first so their tuned format strings win; the general
# expression translator is the last resort. A formula that no handler matches is
# routed to the agent ("pending").
PATTERN_REGISTRY = [
    _h_single_agg,
    _h_ratio_sum,
    _h_ratio_general,
    _h_agg_arithmetic,
    _h_attr,
    _h_passthrough,
    _h_row_count,
    _h_expression,
]


def translate(
    formula: str,
    table: str,
    columns: Optional[set] = None,
    measures: Optional[Dict[str, str]] = None,
    parameters: Optional[Dict[str, Dict]] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    """Return (dax, formatString) if deterministically translatable, else None.

    ``columns`` is the set of real base-column names; column-gated handlers use it
    to avoid emitting DAX that references Tableau calc-field tokens or parameters.
    ``measures`` maps a sibling calc field's internal Tableau name to the DAX
    measure name it becomes, so the expression translator can reference other
    measures (e.g. ``([CY Sales] - [PY Sales]) / [PY Sales]``). ``parameters`` maps
    a parameter name to its definition so a lone parameter-alias formula becomes a
    SELECTEDVALUE measure instead of an invalid bare-column reference.
    """
    if COMPLEX_TOKENS.search(formula):
        return None
    formula = formula.strip()
    # Parameter aliases first: a bare [Param] must become SELECTEDVALUE, never the
    # ungated passthrough's bare-column reference.
    pr = _h_parameter_ref(formula, table, parameters)
    if pr is not None:
        return pr
    for handler in PATTERN_REGISTRY:
        result = handler(formula, table, columns, measures)
        if result is not None:
            return result
    return None


def measure_name(caption: str) -> str:
    """Derive a Title Case measure name from a Tableau caption."""
    return caption.strip()


def _base_columns(ir: Dict) -> set:
    """Set of real base-column names from the IR (top-level ``columns``)."""
    return {c["name"] for c in ir.get("columns", []) if c.get("name")}


def _measure_refs(ir: Dict) -> Dict[str, str]:
    """Map a measure-kind calc field's internal Tableau name -> DAX measure name.

    Keyed by the field's ``fieldName`` with surrounding brackets stripped (the form
    in which sibling formulas reference it), so the expression translator can emit a
    bare ``[Measure Name]`` reference. Only measure-kind calc fields are included so
    we never reference a column-kind calc as if it were already aggregated.
    """
    refs: Dict[str, str] = {}
    for field in ir.get("calculatedFields", []):
        if field.get("suggestedDaxKind") != "measure":
            continue
        internal = field.get("fieldName")
        caption = field.get("caption")
        if not internal or not caption:
            continue
        refs[internal.strip("[]")] = measure_name(caption)
    return refs


def build_measures(ir: Dict, host_table: str) -> Dict[str, List[Dict]]:
    """Split calc fields into deterministically-translated vs LLM-pending."""
    translated: List[Dict] = []
    pending: List[Dict] = []
    columns = _base_columns(ir)
    col_types = {c["name"]: c.get("dataType") for c in ir.get("columns", [])
                 if c.get("name")}
    ref_all = _measure_refs(ir)
    params = {p["name"]: p for p in ir.get("parameters", []) if p.get("name")}
    for field in ir.get("calculatedFields", []):
        formula = field["formula"]
        # exclude the field's own name so a measure can never reference itself.
        self_internal = (field.get("fieldName") or "").strip("[]")
        refs = {k: v for k, v in ref_all.items() if k != self_internal}
        result = translate(formula, host_table, columns, refs, params)
        source = "template"
        if result is None and field.get("suggestedDaxKind", "measure") == "measure":
            # The registry could not handle it — consult the learned cache before
            # routing to the agent. A hit is a formula the agent already solved and
            # whose DAX was validated; reuse skips the AI for it entirely.
            cached = dax_cache.lookup(formula, host_table, set(columns or []),
                                      col_types)
            if cached is not None:
                result = cached
                source = "cache"
        if result and field.get("suggestedDaxKind", "measure") == "measure":
            dax, fmt = result
            translated.append({
                "table": host_table,
                "name": measure_name(field["caption"]),
                "dax": dax,
                "formatString": fmt,
                "displayFolder": "Base Measures",
                "description": None,
                "source": source,
            })
        else:
            pending.append({
                "caption": field["caption"],
                "formula": formula,
                "complexity": field.get("complexity", "complex"),
                "suggestedDaxKind": field.get("suggestedDaxKind", "measure"),
                "reason": "complex-token" if result is None else "non-measure",
            })
    return {"measures": translated, "pending": pending}


def write_output(payload: Dict, analysis_path: str) -> str:
    """Write dax-partial.json next to analysis.json."""
    out_dir = os.path.dirname(os.path.abspath(analysis_path))
    path = os.path.join(out_dir, "dax-partial.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return path


def _default_table(ir: Dict) -> str:
    for ds in ir.get("dataSources", []):
        if ds.get("active"):
            return ir["workbook"]["pascalName"]
    return ir["workbook"]["pascalName"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Translate trivial Tableau calcs to DAX.")
    parser.add_argument("analysis", help="path to analysis.json (IR)")
    parser.add_argument("--table", help="host table name for measures")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.analysis):
        print(f"ERROR: file not found: {args.analysis}", file=sys.stderr)
        return 2

    with open(args.analysis, encoding="utf-8-sig") as fh:
        ir = json.load(fh)

    host = args.table or _default_table(ir)
    payload = build_measures(ir, host)
    path = write_output(payload, args.analysis)
    print(
        f"Wrote {path}\n"
        f"  translated (deterministic): {len(payload['measures'])}\n"
        f"  pending (route to LLM)    : {len(payload['pending'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
