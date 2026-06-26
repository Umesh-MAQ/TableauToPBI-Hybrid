"""dax_cache.py — the learned formula->DAX "notepad" (graduation layer 1).

When the gap-fill agent translates a Tableau calc the deterministic registry could
not handle, the validated result is recorded here keyed by the *normalized Tableau
formula*. On a later run (the same workbook re-migrated, or a different workbook
that contains an identical calc), ``lookup`` returns that DAX so the measure routes
as deterministic and the AI is never invoked for it again.

Design goals (deliberately conservative — correctness over coverage):
  * EXACT match only. The key is the whitespace-normalized formula; no fuzzy
    matching, so a hit means the agent literally solved this exact calc before.
  * SAFE reuse across workbooks. The stored DAX is templatized: the host/fact
    table name is replaced with the ``{T}`` placeholder and substituted back at
    lookup time. A formula is cached ONLY when every ``[column]`` reference in its
    DAX is a column of the host table (no sibling-measure or multi-table deps), and
    it is reused ONLY when every one of those columns exists in the new workbook.
    Anything outside that single-table shape is skipped, never guessed.
  * NEVER breaks a run. Every operation is wrapped by the callers in try/except and
    a corrupt/missing cache simply behaves as empty.

This is the runtime memoization tier. Promoting a *pattern* (a templated formula
shape) into the ``map_dax`` registry remains a separate, human-reviewed step.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple

# Shared, repo-local store so the learned cache is versioned with the engine and
# benefits every migration. Override with TABLEAU_PBI_DAX_CACHE for tests/CI.
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "learned_dax.json")

_PLACEHOLDER = "{T}"
# A column reference is ``Table[Col]`` (unquoted) or ``'Table Name'[Col]`` (quoted).
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_PRECEDING_TABLE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*|'[^']+')\s*$")
# Numeric field placeholders inside a stored pattern template: ``{1}``, ``{2}`` ...
_NUM_PH_RE = re.compile(r"\{(\d+)\}")

# Coarse data-type families. A pattern recorded over a numeric field is only
# replayed onto another numeric field, never (say) a string column, so a generalized
# shape can't silently produce valid-but-wrong DAX across incompatible types.
_TYPE_GROUP = {
    "integer": "num", "int": "num", "real": "num", "number": "num",
    "float": "num", "decimal": "num",
    "string": "str", "text": "str",
    "date": "date", "datetime": "date",
    "boolean": "bool", "bool": "bool",
}


def cache_path() -> str:
    return os.environ.get("TABLEAU_PBI_DAX_CACHE") or _DEFAULT_PATH


def normalize_formula(formula: str) -> str:
    """Collapse whitespace so trivially-different spellings of the same Tableau
    formula share one key. Field names / operators are preserved verbatim."""
    return re.sub(r"\s+", " ", (formula or "").strip())


def _type_group(t: Optional[str]) -> Optional[str]:
    return _TYPE_GROUP.get((t or "").strip().lower())


def _types_compatible(a: Optional[str], b: Optional[str]) -> bool:
    ga, gb = _type_group(a), _type_group(b)
    return ga is None or gb is None or ga == gb


def _load_doc(path: Optional[str] = None) -> Dict[str, Dict]:
    """Load the whole cache document: exact ``entries`` + generalized ``patterns``."""
    p = path or cache_path()
    try:
        with open(p, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"entries": {}, "patterns": {}}
        data.setdefault("entries", {})
        data.setdefault("patterns", {})
        return data
    except (OSError, ValueError):
        return {"entries": {}, "patterns": {}}


def _save_doc(doc: Dict[str, Dict], path: Optional[str] = None) -> None:
    p = path or cache_path()
    payload = {
        "cacheVersion": "2.0",
        "entries": doc.get("entries", {}),
        "patterns": doc.get("patterns", {}),
    }
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, p)


def _host_table_columns(dax: str, table: str) -> Optional[Set[str]]:
    """Return the set of columns the DAX references IF every bracket reference is a
    column of ``table``; otherwise None (signals 'not a single-host-table calc').

    A bare ``[Measure]`` reference (no preceding table token) or a reference to any
    other table (a dimension, a parameter table) yields None — such DAX is unsafe
    to replay verbatim in a different workbook, so it is never cached.
    """
    cols: Set[str] = set()
    for m in _BRACKET_RE.finditer(dax):
        before = dax[:m.start()]
        tm = _PRECEDING_TABLE_RE.search(before)
        if not tm:
            return None  # bare [X] reference (sibling measure / unqualified)
        owner = tm.group(1).strip("'")
        if owner != table:
            return None  # references a different table
        cols.add(m.group(1).strip())
    if not cols:
        return None  # no host-table column at all -> nothing safe to template
    return cols


def _templatize(dax: str, table: str) -> str:
    """Replace host-table references with the ``{T}`` placeholder."""
    dax = re.sub(rf"'{re.escape(table)}'\s*\[", f"{_PLACEHOLDER}[", dax)
    dax = re.sub(rf"\b{re.escape(table)}\s*\[", f"{_PLACEHOLDER}[", dax)
    return dax


def record(formula: str, dax: str, format_string: Optional[str],
           table: str, columns: Set[str], source: str = "llm",
           column_types: Optional[Dict[str, str]] = None,
           path: Optional[str] = None) -> bool:
    """Record a validated formula->DAX mapping. Returns True if anything was stored.

    Stores two tiers from one validated calc:
      * an EXACT entry (layer 1) — verbatim formula -> DAX, host table templatized;
      * a generalized PATTERN (layer 2) when the calc has the safe shape below.

    Skips (returns False) when the calc is not a safe single-host-table shape, when
    inputs are missing, or when nothing new was added. Never raises.
    """
    try:
        norm = normalize_formula(formula)
        if not norm or not dax or not table:
            return False
        used = _host_table_columns(dax, table)
        if used is None:
            return False
        # Defensive: every column used must be a real host-table column.
        if columns and not used.issubset(set(columns)):
            return False
        doc = _load_doc(path)
        entries = doc.setdefault("entries", {})
        patterns = doc.setdefault("patterns", {})
        changed = False

        tmpl = _templatize(dax, table)
        existing = entries.get(norm)
        if not (existing and existing.get("daxTemplate") == tmpl):
            entries[norm] = {
                "formula": norm,
                "daxTemplate": tmpl,
                "formatString": format_string,
                "columnsUsed": sorted(used),
                "source": source,
                "hits": 0,
            }
            changed = True

        if _record_pattern_into(patterns, norm, dax, table, set(columns or []),
                                format_string, source, column_types):
            changed = True

        if changed:
            _save_doc(doc, path)
        return changed
    except Exception:
        return False  # advisory tier — a cache write must never break a run


def lookup(formula: str, table: str,
           columns: Optional[Set[str]] = None,
           column_types: Optional[Dict[str, str]] = None,
           path: Optional[str] = None) -> Optional[Tuple[str, Optional[str]]]:
    """Return (dax, formatString) for a learned formula, else None.

    Tries the EXACT entry first, then the generalized PATTERN store. Reuse is gated:
    every column the resulting DAX references must exist in the current workbook's
    ``columns`` (when provided), and — for patterns — each bound column's data type
    must be compatible with the type the pattern was learned over.
    """
    try:
        doc = _load_doc(path)
        entry = doc.get("entries", {}).get(normalize_formula(formula))
        if entry:
            used = set(entry.get("columnsUsed") or [])
            if columns is None or used.issubset(set(columns)):
                dax = (entry.get("daxTemplate") or "").replace(_PLACEHOLDER, table)
                if dax:
                    return dax, entry.get("formatString")
        return _lookup_pattern(doc.get("patterns", {}), formula, table,
                               columns, column_types)
    except Exception:
        return None


def _record_pattern_into(patterns: Dict[str, Dict], norm_formula: str, dax: str,
                         table: str, columns: Set[str],
                         format_string: Optional[str], source: str,
                         column_types: Optional[Dict[str, str]]) -> bool:
    """Generalize one validated calc into a reusable shape, if it is safe to do so.

    Abstracts each ``[Field]`` into an ordered placeholder (``{1}``, ``{2}`` ...) in
    BOTH the formula and the DAX, so the same shape fires for other columns later.
    Refuses (returns False) unless every bracket reference is a real base column AND
    appears verbatim as ``{T}[Field]`` in the DAX — i.e. the Tableau caption equals
    the model column name. When that identity does not hold the safe column mapping
    is unknown, so only the exact entry is kept and the pattern is skipped.
    """
    try:
        fields = _BRACKET_RE.findall(norm_formula)
        if not fields:
            return False
        distinct: List[str] = []
        for f in fields:
            if f not in distinct:
                distinct.append(f)
        # Field names must not themselves contain placeholder braces.
        if any("{" in f or "}" in f for f in distinct):
            return False
        # Every referenced field must be a real base column (not a calc/parameter ref).
        if not all(f in columns for f in distinct):
            return False
        tmpl_dax = _templatize(dax, table)
        # Identity gate: each field must appear as a host-table column of the SAME name.
        for f in distinct:
            if f"{_PLACEHOLDER}[{f}]" not in tmpl_dax:
                return False
        pmap = {f: i + 1 for i, f in enumerate(distinct)}
        abstracted = _BRACKET_RE.sub(
            lambda m: "[{%d}]" % pmap[m.group(1)] if m.group(1) in pmap
            else m.group(0), norm_formula)
        dax_template = tmpl_dax
        for f, i in pmap.items():
            dax_template = dax_template.replace(
                f"{_PLACEHOLDER}[{f}]", f"{_PLACEHOLDER}[{{{i}}}]")
        ptypes: Dict[str, str] = {}
        if column_types:
            for f, i in pmap.items():
                t = column_types.get(f)
                if t:
                    ptypes[str(i)] = t
        existing = patterns.get(abstracted)
        if existing and existing.get("daxTemplate") == dax_template:
            return False
        patterns[abstracted] = {
            "formula": abstracted,
            "daxTemplate": dax_template,
            "formatString": format_string,
            "arity": len(distinct),
            "placeholderTypes": ptypes,
            "example": {"fields": distinct},
            "source": source,
            "hits": 0,
        }
        return True
    except Exception:
        return False


def _abstract(norm_formula: str) -> Tuple[Optional[str], List[str]]:
    """Turn a normalized formula into its placeholder *shape* + ordered field list.

    ``SUM([Sales]) - SUM([Cost])`` -> ("SUM([{1}]) - SUM([{2}])", ["Sales","Cost"]).
    Returns (None, []) if a field name contains placeholder braces (never abstractable).
    """
    fields = _BRACKET_RE.findall(norm_formula)
    distinct: List[str] = []
    for f in fields:
        if f not in distinct:
            distinct.append(f)
    if any("{" in f or "}" in f for f in distinct):
        return None, []
    if not distinct:
        return norm_formula, []
    pmap = {f: i + 1 for i, f in enumerate(distinct)}
    abstracted = _BRACKET_RE.sub(lambda m: "[{%d}]" % pmap[m.group(1)], norm_formula)
    return abstracted, distinct


def _lookup_pattern(patterns: Dict[str, Dict], formula: str, table: str,
                    columns: Optional[Set[str]],
                    column_types: Optional[Dict[str, str]]
                    ) -> Optional[Tuple[str, Optional[str]]]:
    """Match ``formula`` against a stored shape and bind it to this workbook."""
    if not patterns:
        return None
    abstracted, distinct = _abstract(normalize_formula(formula))
    if not abstracted or not distinct:
        return None
    pmap = {f: i + 1 for i, f in enumerate(distinct)}
    pat = patterns.get(abstracted)
    if not pat:
        return None
    # Every bound field must be a real base column here.
    if columns is not None and not all(f in columns for f in distinct):
        return None
    # Data-type gate: a bound column's type must be compatible with the learned one.
    ptypes = pat.get("placeholderTypes") or {}
    if column_types and ptypes:
        for f, i in pmap.items():
            if not _types_compatible(ptypes.get(str(i)), column_types.get(f)):
                return None
    inv = {i: f for f, i in pmap.items()}
    dax = _NUM_PH_RE.sub(lambda m: inv.get(int(m.group(1)), m.group(0)),
                         pat.get("daxTemplate") or "")
    dax = dax.replace(_PLACEHOLDER, table)
    if not dax or "{" in dax:  # any unresolved placeholder -> refuse
        return None
    return dax, pat.get("formatString")


def record_from_run(odir: str, path: Optional[str] = None) -> List[str]:
    """Learn from a just-completed, validated migration.

    Joins the agent's work list (agent-todo.json: caption -> Tableau formula) with
    the final build spec (decisions.json: measure name -> validated DAX) and records
    every agent-authored *measure* whose DAX is a safe single-host-table shape.
    Returns the list of measure names newly learned. Never raises.
    """
    learned: List[str] = []
    try:
        todo = _read_json(os.path.join(odir, "agent-todo.json"))
        decisions = _read_json(os.path.join(odir, "decisions.json"))
        analysis = _read_json(os.path.join(odir, "analysis.json"))
        if not (todo and decisions):
            return learned

        # Agent-owned MEASURES only (calculated columns are a different artifact).
        todo_formula: Dict[str, str] = {}
        for m in todo.get("measures", []):
            if (m.get("targetKind") or "measure") != "measure":
                continue
            name = m.get("name") or m.get("caption")
            if name and m.get("formula"):
                todo_formula[name] = m["formula"]
        if not todo_formula:
            return learned

        host = _fact_table(decisions)
        columns = {c.get("name") for c in (analysis or {}).get("columns", [])
                   if c.get("name")}
        column_types = {c.get("name"): c.get("dataType")
                        for c in (analysis or {}).get("columns", [])
                        if c.get("name")}
        dax_by_name = {m.get("name"): m for m in decisions.get("measures", [])}

        for name, formula in todo_formula.items():
            md = dax_by_name.get(name)
            if not md or not md.get("dax"):
                continue
            # Only learn what the agent actually authored (skip anything the
            # deterministic template or a prior cache hit already produced).
            if md.get("source") in ("template", "cache"):
                continue
            if record(formula, md["dax"], md.get("formatString"), host,
                      columns, source=md.get("source") or "llm",
                      column_types=column_types, path=path):
                learned.append(name)
    except Exception:
        return learned
    return learned


def bump_pattern_hits(odir: str, path: Optional[str] = None) -> int:
    """Count generalized-pattern reuse in a just-completed, validated migration.

    Each cache-sourced measure in the final ``decisions.json`` that came from a
    *pattern* (not an exact entry) increments that pattern's ``hits``. Counting only
    after validation, once per completed migration, keeps ``hits`` an honest signal
    of which shapes are worth promoting into the deterministic ``map_dax`` registry.
    Returns the number of measures attributed to patterns. Never raises.
    """
    try:
        analysis = _read_json(os.path.join(odir, "analysis.json"))
        decisions = _read_json(os.path.join(odir, "decisions.json"))
        if not (analysis and decisions):
            return 0
        # decisions measure name == calc-field caption.strip() (see classify).
        formula_by_name: Dict[str, str] = {}
        for f in analysis.get("calculatedFields", []):
            cap = (f.get("caption") or "").strip()
            if cap and f.get("formula"):
                formula_by_name.setdefault(cap, f["formula"])
        cache_names = [m.get("name") for m in decisions.get("measures", [])
                       if m.get("source") == "cache" and m.get("name")]
        if not cache_names:
            return 0
        doc = _load_doc(path)
        entries = doc.get("entries", {})
        patterns = doc.get("patterns", {})
        bumped = 0
        for name in cache_names:
            formula = formula_by_name.get(name)
            if not formula:
                continue
            norm = normalize_formula(formula)
            if norm in entries:
                continue  # exact-entry reuse, not a pattern reuse
            abstracted, _fields = _abstract(norm)
            pat = patterns.get(abstracted) if abstracted else None
            if pat:
                pat["hits"] = int(pat.get("hits", 0)) + 1
                bumped += 1
        if bumped:
            _save_doc(doc, path)
        return bumped
    except Exception:
        return 0


def _fact_table(decisions: Dict) -> str:
    for t in decisions.get("tables", []):
        if t.get("role") == "fact":
            return t.get("name")
    tables = decisions.get("tables", [])
    return tables[0].get("name") if tables else "Model"


def _read_json(p: str) -> Optional[Dict]:
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
