"""merge_decisions.py — assemble fragments into the complete decisions.json (Stage 6.9).

This is the deterministic "merge" step the user asked to be done by script, not the
agent. It folds three deterministic/agent fragments into the single artifact that
emit_tmdl.py and emit_pbir.py consume:

  * dax-partial.json   — deterministic measures (source="template")
  * schema-easy.json   — the single-flat schema fragment (when star_det built it),
                         else the agent's schema comes from agent-fragment.json
  * agent-fragment.json — the one batched agent call's output: remaining measures
                         (source="llm"), any calculated columns, and (for the
                         non-single-flat case) the star schema

Guarantees the emitted decisions.json is COMPLETE before generation:
  * measure ``source`` is normalized to the contract enum (template|llm)
  * duplicate measures are de-duped with template (deterministic) precedence
  * any measure/column homed to a non-existent table is re-routed to the fact
    (mirrors emit_tmdl's reassign_orphan_measures defense, applied up-front)
  * the result is schema-validated, then reconcile.py confirms no pending measure
    was dropped (exit 4 signals the orchestrator to escalate to the Opus fallback)

Exit codes: 0 = clean, 2 = validation/usage error, 4 = pending measures missing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONTRACTS = os.path.join(_HERE, os.pardir, "contracts")
_RECONCILE = os.path.join(_HERE, os.pardir, "dax", "reconcile.py")

# Reuse the report binder's field->owning-table resolver so a synthesized measure
# is homed on the SAME table emit_pbir would bind the pill's column to. This keeps
# the measure's DAX referencing a table that actually holds the column even for
# star schemas (where the column is not necessarily on the fact).
sys.path.insert(0, os.path.join(_HERE, os.pardir, "emit"))
import pbir_bind as PB  # noqa: E402
import screenshot_overlay as SO  # noqa: E402  (screenshot visual-intent overlay)


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


def _load(path: str) -> Optional[Dict]:
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _fact_table(tables: List[Dict]) -> Optional[str]:
    for t in tables:
        if t.get("role") == "fact":
            return t.get("name")
    return tables[0].get("name") if tables else None


def _repoint_dax_table(dax: str, old: str, new: str) -> str:
    """Rewrite the table qualifier inside DAX from ``old`` to ``new``.

    Deterministic measures (dax-partial.json) are translated against the single-
    flat placeholder table token (the workbook/model name). When the schema is a
    star with a differently-named fact table, re-homing the measure is not enough:
    the column references inside the DAX (``Model[col]``) still target a table that
    does not exist, so the measure errors in Power BI. Rewrite ``Old[`` and
    ``'Old'[`` qualifiers to the resolved fact table so the DAX resolves.
    """
    if not dax or not old or old == new:
        return dax
    new_tok = new if re.fullmatch(r"\w+", new) else f"'{new}'"
    dax = re.sub(rf"'{re.escape(old)}'\s*\[", f"{new_tok}[", dax)
    dax = re.sub(rf"(?<![\w'])({re.escape(old)})\s*\[", f"{new_tok}[", dax)
    # Bare table references (no trailing column qualifier), e.g.
    # ``COUNTROWS ( Old )`` or ``COUNTROWS('Old')`` — the row-count template
    # passes the placeholder model name as a whole-table argument. Rewrite the
    # quoted and unquoted forms so the table arg resolves to the real fact table.
    dax = re.sub(rf"'{re.escape(old)}'(?!\s*\[)", new_tok, dax)
    dax = re.sub(rf"(?<![\w'\[]){re.escape(old)}(?![\w]|\s*\[)", new_tok, dax)
    return dax


# --- CALCULATE boolean-filter measure hoisting --------------------------------
# Power BI rejects a measure reference inside a CALCULATE *boolean* filter
# predicate (the compact ``Table[Col] = <expr>`` syntax) with:
#   "A function 'CALCULATE' has been used in a True/False expression that is used
#    as a table filter expression. This is not allowed."
# because the measure carries an implicit CALCULATE (context transition). The
# agent legitimately emits e.g. ``CALCULATE(SUM(Orders[Sales]),
# Orders[Order Date (Year)] = [Selected Year])`` which trips this. The fix is to
# pre-evaluate each such measure into a VAR (a plain scalar in the outer context)
# and reference the VAR inside the predicate. This is a deterministic, semantics-
# preserving rewrite applied to every measure so no report (now or future) ships
# this error.

def _extract_paren(s: str, open_idx: int):
    """Given index of '(' in s, return (inner_text, index_after_matching ')').

    Treats ``[...]`` column refs as atomic so parens inside a bracketed column
    name (e.g. ``[Order Date (Year)]``) do not corrupt the depth count.
    """
    depth = 0
    in_brkt = False
    i = open_idx
    n = len(s)
    while i < n:
        ch = s[i]
        if in_brkt:
            if ch == "]":
                in_brkt = False
        elif ch == "[":
            in_brkt = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[open_idx + 1:i], i + 1
        i += 1
    return s[open_idx + 1:], n


def _split_top_args(inner: str) -> List[str]:
    """Split a CALCULATE argument list on top-level commas (bracket-aware)."""
    args: List[str] = []
    depth = 0
    in_brkt = False
    buf: List[str] = []
    for ch in inner:
        if in_brkt:
            buf.append(ch)
            if ch == "]":
                in_brkt = False
            continue
        if ch == "[":
            in_brkt = True
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf))
    return args


_CALC_RE = re.compile(r"CALCULATE\s*\(", re.IGNORECASE)
_BRKT_REF_RE = re.compile(r"\[([^\]]+)\]")


def _is_bare_bool_predicate(arg: str) -> bool:
    """True if ``arg`` is a bare boolean predicate (a top-level comparison), i.e.
    the compact ``<scalar> <op> <scalar>`` form Power BI forbids inside a
    CALCULATE filter when a side references a measure.

    A comparison operator is only "bare" when it sits at bracket/paren depth 0:
    operators inside a table-expression filter (``FILTER(t, [m] > 5)``) or inside
    a bracketed column name (``[Order Date (Year)]``) are nested and so a measure
    ref there is legal — those args are left untouched.
    """
    depth = 0
    in_brkt = False
    i = 0
    n = len(arg)
    while i < n:
        ch = arg[i]
        if in_brkt:
            if ch == "]":
                in_brkt = False
            i += 1
            continue
        if ch == "[":
            in_brkt = True
        elif ch == "'":  # quoted 'Table Name' — skip to the closing quote
            j = arg.find("'", i + 1)
            i = (j + 1) if j != -1 else n
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch in "<>=":
            return True
        i += 1
    return False


def _hoist_filter_predicate(arg: str, measures: set, counter: List[int],
                            hoisted: Dict[str, str]) -> str:
    """If ``arg`` is a bare boolean predicate, hoist its measure refs into VARs.

    Table-expression filters (FILTER/ALL/ALLEXCEPT/…) are left untouched — a
    measure ref there is legal. Only the compact ``<scalar> <op> <scalar>`` form
    (e.g. ``Col = [Measure]`` OR ``[Measure] > 5``) is the one Power BI forbids
    when a side contains a measure.
    """
    if not _is_bare_bool_predicate(arg):
        return arg

    def repl(mm: "re.Match") -> str:
        name = mm.group(1)
        if name not in measures:
            return mm.group(0)  # a column ref, not a measure — leave it
        ref = mm.group(0)
        if ref not in hoisted:
            counter[0] += 1
            hoisted[ref] = f"__cf{counter[0]}"
        return hoisted[ref]

    return _BRKT_REF_RE.sub(repl, arg)


def _rewrite_calculate(dax: str, measures: set, counter: List[int],
                       hoisted: Dict[str, str]) -> str:
    """Recursively rewrite every CALCULATE call, hoisting filter-predicate measures."""
    out: List[str] = []
    i = 0
    n = len(dax)
    while i < n:
        m = _CALC_RE.match(dax, i)
        if m:
            open_idx = m.end() - 1
            inner, after = _extract_paren(dax, open_idx)
            args = _split_top_args(inner)
            new_args = []
            for idx, a in enumerate(args):
                a = _rewrite_calculate(a, measures, counter, hoisted)  # nested CALCULATE
                if idx >= 1:
                    a = _hoist_filter_predicate(a, measures, counter, hoisted)
                new_args.append(a)
            out.append(dax[i:open_idx + 1])  # "CALCULATE(" verbatim
            out.append(",".join(new_args))
            out.append(")")
            i = after
            continue
        out.append(dax[i])
        i += 1
    return "".join(out)


def _sanitize_calculate_filters(dax: str, measure_names: set) -> str:
    """Return DAX with CALCULATE boolean-filter measure refs hoisted into VARs."""
    if not dax or "CALCULATE" not in dax.upper():
        return dax
    counter = [0]
    hoisted: Dict[str, str] = {}
    rewritten = _rewrite_calculate(dax, measure_names, counter, hoisted)
    if not hoisted:
        return dax
    var_lines = [f"VAR {var} = {ref}" for ref, var in hoisted.items()]
    body = rewritten.lstrip()
    if body[:4].upper() == "VAR ":
        # DAX already opens with a VAR block; splice the new VARs in before the
        # first top-level RETURN so we never produce ``RETURN VAR …``.
        idx = re.search(r"(?im)^\s*RETURN\b", rewritten)
        if idx:
            head = rewritten[:idx.start()].rstrip()
            tail = rewritten[idx.start():]
            return head + "\n" + "\n".join(var_lines) + "\n" + tail.lstrip("\n")
        # No locatable RETURN (shouldn't happen) — fall through to safe wrap.
    return "\n".join(var_lines) + "\nRETURN " + rewritten


def _residual_measure_in_filter(dax: str, measures: set) -> bool:
    """True if ``dax`` STILL has a measure ref inside a CALCULATE *boolean*
    filter predicate — i.e. the deterministic sanitizer could not hoist it.

    This is the safety net behind the auto-fix: any shape the rewriter does not
    cover is detected here so the orchestrator can escalate the measure to the
    agent rather than emit DAX Power BI Desktop will reject at runtime.
    """
    if not dax or "CALCULATE" not in dax.upper():
        return False
    i = 0
    n = len(dax)
    while i < n:
        m = _CALC_RE.match(dax, i)
        if not m:
            i += 1
            continue
        open_idx = m.end() - 1
        inner, after = _extract_paren(dax, open_idx)
        for idx, arg in enumerate(_split_top_args(inner)):
            # Recurse into every arg so nested CALCULATEs are checked too.
            if _residual_measure_in_filter(arg, measures):
                return True
            if idx >= 1 and _is_bare_bool_predicate(arg):
                for ref in _BRKT_REF_RE.findall(arg):
                    if ref in measures:
                        return True
        i = after
    return False


def unsafe_calculate_filter_measures(measures: List[Dict]) -> List[str]:
    """Names of measures whose DAX still embeds a measure ref in a CALCULATE
    boolean filter after sanitization — the deterministic engine cannot safely
    emit these, so they must be escalated to the agent. Empty list = all safe."""
    names = {m.get("name") for m in measures if m.get("name")}
    return [m.get("name") for m in measures
            if _residual_measure_in_filter(m.get("dax", ""), names)]


# --- Calculated-column measure-reference guard --------------------------------
# A CALCULATED COLUMN that references a MEASURE makes Power BI wrap the measure in
# an implicit CALCULATE (context transition). Context transition takes the current
# row and filters EVERY column of the host table — including the column being
# defined and its sibling calculated columns — so the column depends on itself and
# the engine fails the whole table refresh with:
#     "A cyclic reference was encountered during evaluation."
# It is also semantically wrong: a calc column is materialised at refresh and so
# can never react to a slicer-driven measure (e.g. a [Selected Year] parameter).
# The correct home for that logic is a measure. We therefore DROP any calc column
# whose DAX references a measure, deterministically, so no migration (now or
# future) can ever ship this cyclic-reference error.

# A *bare* [Name] ref that is NOT preceded by a table qualifier (' , ] or word
# char). ``Orders[Sales]`` is a column ref (qualified); a lone ``[Selected Year]``
# is a measure-or-column ref. We resolve it against the known measure-name set.
_BARE_BRKT_REF_RE = re.compile(r"(?<![')\w])\[([^\]]+)\]")


def _calc_column_measure_ref(dax: str, measure_names: set) -> Optional[str]:
    """Return the name of the first MEASURE a calc column's DAX references via a
    bare ``[Name]`` (not a ``Table[Col]`` qualifier), else None."""
    if not dax:
        return None
    # Qualified columns inside the same DAX share the [Col] token shape; exclude
    # them so only genuinely bare references are considered.
    qualified = {c for _, _, c in _QUALIFIED_REF_RE.findall(dax)}
    for name in _BARE_BRKT_REF_RE.findall(dax):
        if name in qualified:
            continue
        if name in measure_names:
            return name
    return None


# Matches a qualified ``'Table'[Col]`` / ``Table[Col]`` reference (table side
# either quoted or a bare identifier), mirroring validate_semantics._QUALIFIED.
_QUALIFIED_REF_RE = re.compile(r"(?:'([^']+)'|(\b[A-Za-z_][\w ]*?))\s*\[([^\]]+)\]")


def strip_measure_referencing_calc_columns(
        calc_cols: List[Dict], measure_names: set) -> List[Dict]:
    """Drop calc columns whose DAX references a measure (cyclic-reference trap).

    Returns the kept columns; emits a warning to stderr for each dropped one so
    the reason is visible in the finish log."""
    kept: List[Dict] = []
    for c in calc_cols:
        ref = _calc_column_measure_ref(c.get("dax", ""), measure_names)
        if ref:
            sys.stderr.write(
                f"merge: dropping calculated column "
                f"'{c.get('table')}'[{c.get('name')}] — its DAX references measure "
                f"[{ref}]; a calc column referencing a measure causes a cyclic "
                f"reference at refresh and cannot react to a slicer. Use a measure "
                f"for that logic instead.\n")
            continue
        kept.append(c)
    return kept


def _normalize_measure(m: Dict, default_source: str) -> Dict:
    src = m.get("source", default_source)
    if src not in ("template", "llm", "cache"):
        src = "template" if src == "template" else "llm"
    return {
        "table": m.get("table"),
        "name": m.get("name"),
        "dax": m.get("dax"),
        "formatString": m.get("formatString"),
        "displayFolder": m.get("displayFolder"),
        "description": m.get("description"),
        "source": src,
    }


def _dedup_measures(det: List[Dict], agent: List[Dict]) -> List[Dict]:
    """Combine with deterministic (template) precedence over agent (llm) duplicates."""
    out: List[Dict] = []
    seen = set()
    for m in det:
        key = _norm(m.get("name", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    for m in agent:
        key = _norm(m.get("name", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


# Tableau aggregation -> (DAX function, human label) for a synthesized measure.
# COUNTD -> DISTINCTCOUNT; the label seeds the measure name (e.g. "Count of show_id").
_SYNTH_AGG = {
    "SUM": ("SUM", "Sum"), "COUNT": ("COUNT", "Count"),
    "COUNTD": ("DISTINCTCOUNT", "Count"), "DISTINCTCOUNT": ("DISTINCTCOUNT", "Count"),
    "AVG": ("AVERAGE", "Average"), "AVERAGE": ("AVERAGE", "Average"),
    "MIN": ("MIN", "Min"), "MAX": ("MAX", "Max"), "MEDIAN": ("MEDIAN", "Median"),
}


def _measure_expresses_pill(dax: str, fn: str, column: str) -> bool:
    """True when ``dax`` already aggregates ``column`` with DAX function ``fn``
    (any table qualifier). Mirrors pbir_bind.measure_for_pill's match so synthesis
    never duplicates a measure the deterministic translator already produced."""
    if not dax or not fn or not column:
        return False
    pat = re.compile(
        rf"\b{fn}\s*\(\s*[^()\[\]]*\[\s*{re.escape(column)}\s*\]\s*\)", re.I)
    return bool(pat.search(dax))


def synthesize_pill_measures(ir: Dict, tables: List[Dict], measures: List[Dict],
                             fact: Optional[str]) -> List[Dict]:
    """Synthesize a deterministic model measure for every worksheet measure pill
    that aggregates a plain column which NO existing measure expresses.

    Power BI Desktop renders a chart/card value reliably only when it is bound to a
    real model measure. An inline visual-query aggregation over a column (e.g.
    DISTINCTCOUNT(show_id) on a fact with no measures, or AVG(int_rate) on a star
    schema) is dropped by Desktop and the visual renders EMPTY — the exact symptom
    seen on the Netflix charts and the Loan 'Avg Interest Rate' card. Materialising
    the aggregation as a named measure here gives every such visual a measure to
    bind (emit_pbir's measure_for_pill resolves it by DAX pattern), matching how a
    hand-built report would model the same value.

    Scope is constrained so it can never emit a measure that references the wrong
    table or a non-existent column:
      * the pill column must be a real physical column — excludes calc-field pills
        (e.g. ``Calculation_1872…``) whose internal name is not a model column and
        which already own a named measure from the translator.
      * the measure is homed on the column's OWNING table, resolved the same way the
        report binder resolves it (pbir_bind.entity_for_field): a dim/date table
        that owns the column, else the fact. This works for single-flat AND star
        schemas; a single-flat model trivially resolves every column to its one
        table, so prior single-flat behaviour is preserved.

    Idempotent: when a matching measure already exists (the DAX translator built one
    from a named calc), nothing is added.
    """
    valid_tables = {t.get("name") for t in tables if t.get("name")}
    if not valid_tables or not fact:
        return []
    physical_cols = {c.get("name") for c in ir.get("columns", []) if c.get("name")}
    # entity_for_field reads tables[].role/sourceDatasource to find a column's owner.
    dec_like = {"tables": tables}

    synthesized: List[Dict] = []
    for ws in ir.get("worksheets", []):
        # Every measure pill the worksheet plots (not just the first) needs a model
        # measure to bind to; a combo/scatter/detail visual carries several, and a
        # later pill's inline aggregation is dropped by Desktop just like the first.
        for p in (ws.get("measures") or []):
            agg = (p.get("agg") or "").upper()
            col = p.get("column") or p.get("field")
            spec = _SYNTH_AGG.get(agg)
            # Physical-column pills only: a calc-field pill (Calculation_<id>) is not
            # a model column and already owns a named measure from the translator.
            if not spec or not col or col not in physical_cols:
                continue
            fn, label = spec
            if any(_measure_expresses_pill(m.get("dax"), fn, col)
                   for m in measures + synthesized):
                continue
            # Home the measure on the column's owning table (the same resolution the
            # report binder uses), so the DAX references a table that holds the
            # column even when the schema is a star. Falls back to the fact.
            home = PB.entity_for_field(col, fact, dec_like, ir)
            if home not in valid_tables:
                home = fact
            tok = home if re.fullmatch(r"\w+", home) else f"'{home}'"
            synthesized.append({
                "table": home,
                "name": f"{label} of {col}",
                "dax": f"{fn}({tok}[{col}])",
                "formatString": "#,0" if label == "Count" else None,
                "displayFolder": None,
                "description": None,
                "source": "template",
            })
    return synthesized


# Tableau FIXED-LOD aggregation -> DAX function for a synthesized bin column.
_LOD_BIN_AGG = {
    "COUNTD": "DISTINCTCOUNT", "DISTINCTCOUNT": "DISTINCTCOUNT",
    "COUNT": "COUNT", "SUM": "SUM",
    "AVG": "AVERAGE", "AVERAGE": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
}

# { FIXED [A] : AGG([B]) } -- a per-entity grouping value (a histogram bin axis):
# for each member of A, aggregate B. Whitespace/newlines inside the braces vary.
_FIXED_LOD_RE = re.compile(
    r"^\s*\{\s*FIXED\s+\[(?P<a>[^\]]+)\]\s*:\s*"
    r"(?P<agg>COUNTD|DISTINCTCOUNT|COUNT|SUM|AVG|AVERAGE|MIN|MAX)\s*"
    r"\(\s*\[(?P<b>[^\]]+)\]\s*\)\s*\}\s*$", re.IGNORECASE | re.DOTALL)


def _physical_column_for(caption: Optional[str], ir: Dict,
                         physical_cols: set, _seen: Optional[set] = None) -> Optional[str]:
    """Resolve a Tableau field caption to its underlying PHYSICAL column name.

    A field may be physical itself, or a calc whose result is a physical column
    (e.g. ``CY Customers`` = ``IF YEAR([Order Date]) = [Select Year] THEN
    [Customer ID] END`` -> ``Customer ID``). The IF/CASE condition columns (the
    date, the year parameter) are ALSO listed in ``dependsOn``, so the returned
    column is taken from the THEN branch first -- never the first dependsOn entry,
    which would wrongly pick ``Order Date``."""
    if not caption:
        return None
    if caption in physical_cols:
        return caption
    _seen = _seen or set()
    if caption in _seen:
        return None
    _seen.add(caption)
    for f in ir.get("calculatedFields", []):
        if f.get("caption") != caption:
            continue
        formula = f.get("formula") or ""
        m = re.search(r"\bTHEN\s*\[([^\]]+)\]", formula, re.IGNORECASE)
        if m and m.group(1) in physical_cols:
            return m.group(1)
        deps = f.get("dependsOn") or []
        for dep in deps:
            if dep in physical_cols:
                return dep
        for dep in deps:
            r = _physical_column_for(dep, ir, physical_cols, _seen)
            if r:
                return r
        return None
    return None


def synthesize_lod_bin_columns(ir: Dict, tables: List[Dict],
                               fact: Optional[str]) -> List[Dict]:
    """Author a calculated COLUMN for every ``{ FIXED [A]: AGG([B]) }`` dimension-
    role LOD field (a per-entity grouping value plotted as a histogram bin axis).

    Tableau's ``{ FIXED [Customer]: COUNTD([Order]) }`` gives each row the number
    of distinct orders that customer placed -- a discrete value shown on the
    category axis, with the chart counting customers per bin. In Power BI a
    category axis MUST be a column (a measure cannot group), so this materialises
    the LOD as a calculated column ``CALCULATE(<agg>(T[B]), ALLEXCEPT(T, T[A]))``.

    The agent frequently mis-authors this LOD as an averaging MEASURE
    (``DIVIDE([CY Orders],[CY Customers])``), which leaves the histogram with no
    groupable axis -- the emitter then falls back to a high-cardinality key (e.g.
    ``Order ID``) and plots one bar per order. Emitting the column deterministically
    here (and dropping the shadow measure in ``merge``) makes the histogram bind
    correctly regardless of what the agent produced. Generic for any workbook.
    """
    valid = {t.get("name") for t in tables if t.get("name")}
    if not valid or not fact:
        return []
    physical_cols = {c.get("name") for c in ir.get("columns", []) if c.get("name")}
    dec_like = {"tables": tables}
    out: List[Dict] = []
    for f in ir.get("calculatedFields", []):
        if (f.get("role") or "").strip().lower() != "dimension":
            continue
        caption = f.get("caption")
        m = _FIXED_LOD_RE.match(f.get("formula") or "")
        if not caption or not m:
            continue
        a_phys = _physical_column_for(m.group("a"), ir, physical_cols)
        b_phys = _physical_column_for(m.group("b"), ir, physical_cols)
        fn = _LOD_BIN_AGG.get(m.group("agg").upper())
        if not (a_phys and b_phys and fn):
            continue
        # ALLEXCEPT needs the grouping key and the aggregated column on the SAME
        # table. Resolve each to its owning table the way the binder does; only
        # emit when they agree (else fall back to the fact, which holds both for a
        # denormalised source).
        home_a = PB.entity_for_field(a_phys, fact, dec_like, ir)
        home_b = PB.entity_for_field(b_phys, fact, dec_like, ir)
        home = home_a if (home_a == home_b and home_a in valid) else fact
        tok = home if re.fullmatch(r"\w+", home) else f"'{home}'"
        agg_label = m.group("agg").upper()
        out.append({
            "table": home,
            "name": caption,
            "dax": (f"CALCULATE({fn}({tok}[{b_phys}]), "
                    f"ALLEXCEPT({tok}, {tok}[{a_phys}]))"),
            "dataType": "int64" if fn in ("DISTINCTCOUNT", "COUNT") else "double",
            "formatString": "0" if fn in ("DISTINCTCOUNT", "COUNT") else None,
            "description": (
                f"Per-{a_phys} {agg_label} of {b_phys}, materialised as a "
                f"calculated column so it can be a histogram category axis "
                f"(Tableau LOD {{ FIXED [{m.group('a')}]: "
                f"{agg_label}([{m.group('b')}]) }})."),
        })
    return out


def merge(ir: Dict,
          dax_partial: Optional[Dict],
          schema_easy: Optional[Dict],
          agent_fragment: Optional[Dict]) -> Dict:
    """Pure assembly of the complete decisions dict from the fragments."""
    model = ir.get("workbook", {}).get("pascalName", "Model")
    dax_partial = dax_partial or {}
    agent_fragment = agent_fragment or {}

    # Schema: prefer the deterministic single-flat fragment; else the agent's design.
    schema_src = schema_easy if schema_easy is not None else agent_fragment
    strategy = schema_src.get("tableStrategy", "single-flat")
    tables = list(schema_src.get("tables", []))
    relationships = list(schema_src.get("relationships", []))
    fact = _fact_table(tables) or model

    det = [_normalize_measure(m, "template") for m in dax_partial.get("measures", [])]
    agent_m = [_normalize_measure(m, "llm") for m in agent_fragment.get("measures", [])]
    measures = _dedup_measures(det, agent_m)

    # Materialise implicit visual aggregations (e.g. COUNTD(show_id) on a fact with
    # no named measures, or AVG(int_rate) on a star-schema card) as real model
    # measures so Power BI Desktop renders the bound charts/cards instead of dropping
    # an inline column aggregation (which leaves the visual empty). Covers every
    # measure pill on every worksheet, single-flat or star. No-op when an equivalent
    # measure already exists.
    measures = _dedup_measures(
        measures, synthesize_pill_measures(ir, tables, measures, fact))

    valid = {t.get("name") for t in tables}
    for m in measures:
        if m.get("table") not in valid:
            old = m.get("table")
            m["table"] = fact
            # Re-point the DAX column qualifier too: a deterministic measure carries
            # the single-flat placeholder table token inside its DAX, which must now
            # resolve to the real fact table (else it references a missing table).
            m["dax"] = _repoint_dax_table(m.get("dax", ""), old, fact)

    calc_cols = list(agent_fragment.get("calculatedColumns", []))
    for c in calc_cols:
        if c.get("table") not in valid:
            c["table"] = fact

    # Materialise FIXED-LOD per-entity grouping fields (e.g. { FIXED [Customer]:
    # COUNTD([Order]) }) as deterministic calculated columns so a Tableau histogram
    # binds to a real groupable category axis. The agent often mis-authors these as
    # an averaging measure, which leaves the chart with no axis and mis-binds to a
    # high-cardinality key. Add the column (if not already present) and DROP any
    # measure of the same name so the field is a column only, never a duplicate Y.
    lod_bins = synthesize_lod_bin_columns(ir, tables, fact)
    if lod_bins:
        existing = {_norm(c.get("name")) for c in calc_cols}
        bin_names = {_norm(col.get("name")) for col in lod_bins}
        for col in lod_bins:
            if _norm(col.get("name")) not in existing:
                calc_cols.append(col)
        measures = [m for m in measures if _norm(m.get("name")) not in bin_names]

    # Harden every measure against the "CALCULATE in a True/False filter" error:
    # a measure reference inside a CALCULATE boolean-filter predicate is hoisted
    # into a VAR (plain scalar in the outer context). Done after re-homing so the
    # measure-name set is final and the rewrite sees resolved table qualifiers.
    measure_names = {m.get("name") for m in measures if m.get("name")}
    for m in measures:
        m["dax"] = _sanitize_calculate_filters(m.get("dax", ""), measure_names)

    # Harden against the "cyclic reference" refresh error: drop any calculated
    # column whose DAX references a measure (context transition makes the column
    # depend on itself). The CY/PY-style logic such a column tried to express
    # already lives in measures, which correctly react to the slicer.
    calc_cols = strip_measure_referencing_calc_columns(calc_cols, measure_names)

    # A field materialised as a calculated column (an agent-authored Tableau bin /
    # grouping field, or a synthesized LOD bin) must NOT also exist as a like-named
    # measure. The deterministic translator can pass a dimension-role bin field
    # through as a bare-column measure (invalid DAX referencing a column on the
    # wrong table); the calculated column is the resolved artifact and wins. Drop
    # any measure whose name collides with a calculated column.
    calc_col_names = {_norm(c.get("name")) for c in calc_cols if c.get("name")}
    measures = [m for m in measures if _norm(m.get("name")) not in calc_col_names]

    measures.sort(key=lambda m: (0 if m["source"] == "template" else 1,
                                 (m.get("name") or "").lower()))
    calc_cols.sort(key=lambda c: (c.get("name") or "").lower())

    # Carry the agent's report-fidelity decisions through to the emitter. These were
    # previously dropped (hard-coded empty), which forced every ambiguous worksheet to
    # fall back to a table. The agent authors them in agent-fragment.json; the
    # single-flat schema fragment never has them.
    visual_decisions = list(agent_fragment.get("visualDecisions", []))
    field_parameters = list(agent_fragment.get("fieldParameters", []))

    # Overlay the screenshot (vision) layer: ``visualHints`` upgrade the visual
    # INTENT (e.g. a KPI worksheet the agent guessed as 'card' is really a kpiStack
    # tile the screenshot shows with a sparkline + ▲% vs PY) by PRECEDENCE — one
    # decision per worksheet, never a duplicate visual. Bindings/data stay
    # deterministic. Mismatches are recorded so the user can audit fidelity.
    visual_decisions, discrepancies = SO.apply_visual_hints(
        visual_decisions, agent_fragment.get("visualHints"), ir)

    decisions = {
        "decisionsVersion": "1.0",
        "modelName": model,
        "tableStrategy": strategy,
        "tables": tables,
        "relationships": relationships,
        "measures": measures,
        "calculatedColumns": calc_cols,
        "visualDecisions": visual_decisions,
        "fieldParameters": field_parameters,
    }
    if discrepancies:
        decisions["visualFidelity"] = {"discrepancies": discrepancies}
    return decisions


def validate(decisions: Dict) -> List[str]:
    """Schema-validate the decisions dict. Returns a list of error strings (empty=ok).

    Uses jsonschema when available; otherwise falls back to a minimal required-key
    check so the merge still hard-fails on a structurally broken artifact.
    """
    schema_path = os.path.join(_CONTRACTS, "decisions_schema.json")
    schema = _load(schema_path)
    try:
        import jsonschema  # type: ignore
    except Exception:
        errors: List[str] = []
        for key in ("modelName", "tableStrategy", "tables", "measures"):
            if key not in decisions:
                errors.append(f"missing required key: {key}")
        for m in decisions.get("measures", []):
            for key in ("table", "name", "dax"):
                if not m.get(key):
                    errors.append(f"measure missing {key}: {m.get('name')}")
        return errors
    validator = jsonschema.Draft7Validator(schema)
    return [f"{'/'.join(str(p) for p in e.path)}: {e.message}"
            for e in validator.iter_errors(decisions)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble fragments into the complete decisions.json and validate.")
    parser.add_argument("analysis", help="path to analysis.json (IR)")
    parser.add_argument("--agent-fragment",
                        help="path to agent-fragment.json (defaults to alongside analysis.json)")
    parser.add_argument("--out", help="output decisions.json path (defaults alongside analysis.json)")
    parser.add_argument("--skip-reconcile", action="store_true",
                        help="skip the reconcile cross-check (tests only)")
    args = parser.parse_args(argv)

    ir = _load(args.analysis)
    if ir is None:
        print(f"ERROR: file not found: {args.analysis}", file=sys.stderr)
        return 2

    out_dir = os.path.dirname(os.path.abspath(args.analysis))
    dax_partial = _load(os.path.join(out_dir, "dax-partial.json"))
    schema_easy = _load(os.path.join(out_dir, "schema-easy.json"))
    frag_path = args.agent_fragment or os.path.join(out_dir, "agent-fragment.json")
    agent_fragment = _load(frag_path)

    decisions = merge(ir, dax_partial, schema_easy, agent_fragment)

    errors = validate(decisions)
    if errors:
        print("ERROR: decisions.json failed schema validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    out_path = args.out or os.path.join(out_dir, "decisions.json")
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(decisions, fh, indent=2, ensure_ascii=False)
    print(f"merge: wrote {out_path} "
          f"(tables={len(decisions['tables'])} measures={len(decisions['measures'])})")
    vf = decisions.get("visualFidelity", {}).get("discrepancies") if isinstance(
        decisions.get("visualFidelity"), dict) else None
    if vf:
        print(f"  screenshot overlay: {len(vf)} visual-intent change(s)")
        for d in vf:
            arrow = f"{d.get('from')} -> {d.get('to')}" if d.get("to") else "skipped"
            # Console-safe: a screenshot note can carry non-cp1252 glyphs (▲, $, …);
            # the full note is preserved in decisions.json visualFidelity, so the
            # console line stays ASCII to never crash a Windows cp1252 pipe.
            line = f"    - {d.get('worksheet')}: {arrow}"
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode())

    if args.skip_reconcile:
        return 0

    rc = subprocess.call([sys.executable, _RECONCILE, args.analysis,
                          "--decisions", out_path])
    return 4 if rc == 4 else 0


if __name__ == "__main__":
    raise SystemExit(main())
