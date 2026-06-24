#!/usr/bin/env python3
"""validate_semantics.py — semantic (meaning) validation for a generated PBIP.

The syntax validators (tmdl-validate, validate_pbip.py) only check that the TMDL
and JSON are well-formed.  They do NOT check that the things the files REFER to
actually exist or line up.  That gap is where almost every "the project won't
open in Power BI Desktop" error comes from:

  * a relationship points at a column that was renamed or does not exist
  * a relationship joins two columns of DIFFERENT data types
  * a measure's DAX references a column/measure that is not in the model
  * a measure uses a reserved DAX word (avg, date, ...) as a VAR name
  * a PBIR visual.json carries a property the schema forbids

This script reads the GENERATED model + report and reports every such mismatch
in plain language, BEFORE you open Desktop.

Usage:
    python scripts/validate_semantics.py "Output/SalesCustomerDashboards"

Exit codes: 0 = clean, 2 = errors found, 3 = usage/structure error.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from typing import Dict, List, Set, Tuple

# DAX reserved words that break the model load if used as a VAR name.
RESERVED_VARS = {
    "abs", "all", "and", "as", "avg", "blank", "by", "count", "date", "day",
    "false", "filter", "hour", "if", "in", "max", "measure", "min", "minute",
    "month", "not", "or", "order", "rank", "row", "second", "sum", "table",
    "time", "true", "value", "var", "week", "year",
}

# ---------------------------------------------------------------------------
# Tiny TMDL reader (line-oriented; matches the emitter's exact layout)
# ---------------------------------------------------------------------------


def _name_after(keyword_rest: str) -> str:
    """Return the object name after a `column`/`measure`/`table` keyword.

    Handles both quoted ('Customer ID') and bare (Sales) names and stops before
    a ` =` calculated-object assignment.
    """
    s = keyword_rest.strip()
    if s.startswith("'"):
        end = s.find("'", 1)
        return s[1:end] if end > 0 else s[1:]
    # bare name: up to ' =' or end
    return re.split(r"\s*=", s, maxsplit=1)[0].strip()


def parse_semantic_model(sm_dir: str) -> Dict:
    """Parse a .SemanticModel/definition folder into a lightweight model map.

    Returns dict with: columns {table: set(col)}, colTypes {(table,col): type},
    measures set(name), measureHost {name: table}, measureDax {name: dax}.
    """
    tables_dir = os.path.join(sm_dir, "definition", "tables")
    model: Dict = {
        "columns": {}, "colTypes": {}, "measures": set(),
        "measureHost": {}, "measureDax": {}, "calcColDax": {},
    }
    for path in sorted(glob.glob(os.path.join(tables_dir, "*.tmdl"))):
        _parse_table_file(path, model)
    return model


def _parse_table_file(path: str, model: Dict) -> None:
    with open(path, encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    table = None
    cols: Set[str] = set()
    cur_col: str = ""           # column awaiting its dataType line
    cur_measure: str = ""       # measure collecting multi-line DAX
    measure_dax: List[str] = []

    def flush_measure() -> None:
        if cur_measure:
            model["measureDax"][cur_measure] = "\n".join(measure_dax).strip()

    for ln in lines:
        if ln.startswith("table "):
            table = _name_after(ln[6:])
            model["columns"].setdefault(table, set())
            continue
        if table is None:
            continue
        # one-tab declarations
        if ln.startswith("\tcolumn "):
            flush_measure(); cur_measure = ""; measure_dax = []
            rest = ln[len("\tcolumn "):]
            cur_col = _name_after(rest)
            model["columns"][table].add(cur_col)
            cols.add(cur_col)
            # A calculated column carries inline DAX after ` = `; record it so the
            # measure-reference (cyclic-reference) check can inspect it.
            if "=" in rest:
                model["calcColDax"][(table, cur_col)] = rest.split("=", 1)[1].strip()
            continue
        if ln.startswith("\tmeasure "):
            flush_measure()
            rest = ln[len("\tmeasure "):]
            cur_measure = _name_after(rest)
            model["measures"].add(cur_measure)
            model["measureHost"][cur_measure] = table
            inline = rest.split("=", 1)[1].strip() if "=" in rest else ""
            measure_dax = [inline] if inline else []
            cur_col = ""
            continue
        # two-tab property of the current column
        if ln.startswith("\t\tdataType:") and cur_col:
            model["colTypes"][(table, cur_col)] = ln.split(":", 1)[1].strip()
            continue
        # three-tab measure DAX continuation
        if cur_measure and ln.startswith("\t\t\t"):
            measure_dax.append(ln.strip())
            continue
        # two-tab measure property ends the DAX body
        if cur_measure and ln.startswith("\t\t"):
            flush_measure(); cur_measure = ""; measure_dax = []
            continue
    flush_measure()


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------


def parse_ref(s: str) -> Tuple[str, str]:
    """Split a `Table.Column` TMDL reference, honouring quotes on either side."""
    s = s.strip()
    if s.startswith("'"):
        end = s.find("'", 1)
        table = s[1:end]
        rest = s[end + 1:]
    else:
        dot = s.find(".")
        table = s[:dot] if dot >= 0 else s
        rest = s[dot:] if dot >= 0 else ""
    rest = rest.lstrip(".").strip()
    if rest.startswith("'"):
        end = rest.find("'", 1)
        col = rest[1:end] if end > 0 else rest[1:]
    else:
        col = rest
    return table, col


def parse_relationships(sm_dir: str) -> List[Dict]:
    path = os.path.join(sm_dir, "definition", "relationships.tmdl")
    if not os.path.isfile(path):
        return []
    rels: List[Dict] = []
    cur: Dict = {}
    with open(path, encoding="utf-8-sig") as fh:
        for ln in fh.read().splitlines():
            st = ln.strip()
            if st.startswith("relationship "):
                if cur:
                    rels.append(cur)
                cur = {"id": st.split(" ", 1)[1]}
            elif st.startswith("fromColumn:"):
                cur["from"] = parse_ref(st.split(":", 1)[1])
            elif st.startswith("toColumn:"):
                cur["to"] = parse_ref(st.split(":", 1)[1])
    if cur:
        rels.append(cur)
    return rels


_QUALIFIED = re.compile(r"(?:'([^']+)'|(\b[A-Za-z_][\w ]*?))\s*\[([^\]]+)\]")
_BARE = re.compile(r"(?<![')\w])\[([^\]]+)\]")
_VAR = re.compile(r"\bVAR\s+([A-Za-z_]\w*)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_relationships(model: Dict, rels: List[Dict], errors: List[str]) -> None:
    for r in rels:
        for side in ("from", "to"):
            if side not in r:
                continue
            t, c = r[side]
            if t not in model["columns"]:
                errors.append(f"relationship {r['id']}: {side}Column table '{t}' not found")
            elif c not in model["columns"][t]:
                errors.append(
                    f"relationship {r['id']}: {side}Column '{t}'[{c}] not found "
                    f"(table has: {sorted(model['columns'][t])})")
        if "from" in r and "to" in r:
            ft = model["colTypes"].get(r["from"])
            tt = model["colTypes"].get(r["to"])
            if ft and tt and ft != tt:
                errors.append(
                    f"relationship {r['id']}: data type mismatch "
                    f"{r['from'][0]}[{r['from'][1]}]={ft} vs "
                    f"{r['to'][0]}[{r['to'][1]}]={tt} (relationship will not form)")


def check_measures(model: Dict, errors: List[str], warnings: List[str]) -> None:
    all_cols = {c for cols in model["columns"].values() for c in cols}
    for name, dax in model["measureDax"].items():
        host = model["measureHost"].get(name, "")
        # reserved VAR names
        for var in _VAR.findall(dax):
            if var.lower() in RESERVED_VARS:
                errors.append(
                    f"measure '{name}': VAR '{var}' is a reserved DAX word "
                    f"(rename it, e.g. '{var}Value')")
        # qualified Table[Column] refs
        for q1, q2, col in _QUALIFIED.findall(dax):
            tbl = q1 or q2.strip()
            if tbl not in model["columns"]:
                errors.append(
                    f"measure '{name}': references table '{tbl}' which does not exist "
                    f"(in '{tbl}'[{col}]; known tables: {sorted(model['columns'])})")
            elif col not in model["columns"][tbl] and col not in model["measures"]:
                errors.append(
                    f"measure '{name}': references '{tbl}'[{col}] which does not exist")
        # bare [X] refs — must be a column (somewhere) or a measure
        qualified_cols = {c for _, _, c in _QUALIFIED.findall(dax)}
        for col in _BARE.findall(dax):
            if col in qualified_cols:
                continue
            if col in model["measures"] or col in all_cols:
                continue
            warnings.append(
                f"measure '{name}': bare [{col}] is neither a known measure nor column "
                f"(possible unresolved Tableau reference)")


def check_calc_columns(model: Dict, errors: List[str]) -> None:
    """Flag any calculated column whose DAX references a MEASURE.

    Power BI wraps a measure referenced from a calc column in an implicit
    CALCULATE (context transition), which makes the column depend on every column
    of its own table — including itself — so the model refresh dies with
    "A cyclic reference was encountered during evaluation." Such logic belongs in
    a measure, which can also react to slicers (a calc column cannot)."""
    for (table, col), dax in model.get("calcColDax", {}).items():
        qualified_cols = {c for _, _, c in _QUALIFIED.findall(dax)}
        for ref in _BARE.findall(dax):
            if ref in qualified_cols:
                continue
            if ref in model["measures"]:
                errors.append(
                    f"calculated column '{table}'[{col}]: references measure [{ref}] "
                    f"— a calc column cannot reference a measure (causes a cyclic "
                    f"reference at refresh). Express this logic as a measure instead.")


def check_pbir(report_dir: str, errors: List[str]) -> None:
    allowed = {"$schema", "name", "position", "visual", "visualGroup",
               "howCreated", "filterConfig"}
    for path in glob.glob(os.path.join(report_dir, "**", "visual.json"), recursive=True):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"visual.json unreadable: {path} ({exc})")
            continue
        extra = set(data) - allowed
        if extra:
            rel = os.path.relpath(path, report_dir)
            errors.append(
                f"{rel}: visual root has forbidden propertie(s) {sorted(extra)} "
                f"(Desktop rejects anything except {sorted(allowed)})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _find_dir(root: str, suffix: str) -> str:
    if root.endswith(suffix):
        return root
    hits = glob.glob(os.path.join(root, f"*{suffix}"))
    return hits[0] if hits else ""


def validate(root: str) -> int:
    sm_dir = _find_dir(root, ".SemanticModel")
    report_dir = _find_dir(root, ".Report")
    if not sm_dir:
        print(f"ERROR: no .SemanticModel found under {root}", file=sys.stderr)
        return 3

    model = parse_semantic_model(sm_dir)
    rels = parse_relationships(sm_dir)
    errors: List[str] = []
    warnings: List[str] = []

    check_relationships(model, rels, errors)
    check_measures(model, errors, warnings)
    check_calc_columns(model, errors)
    if report_dir:
        check_pbir(report_dir, errors)

    print(f"Semantic validation: {root}")
    print(f"  tables: {len(model['columns'])}  "
          f"measures: {len(model['measures'])}  relationships: {len(rels)}")
    for w in warnings:
        print(f"  warn  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    print(f"\nResult: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 2 if errors else 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python validate_semantics.py <Output/{WorkbookName}>", file=sys.stderr)
        return 3
    root = argv[0]
    if not os.path.isdir(root):
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 3
    return validate(root)


if __name__ == "__main__":
    raise SystemExit(main())
