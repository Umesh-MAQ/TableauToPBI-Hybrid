"""validate_bindings.py — Report binding-integrity guard.

Cross-checks every visual field binding in a generated PBIP report against the
columns and measures that actually exist in the semantic model (parsed from the
emitted TMDL). Catches the class of bug where a slicer/chart field is bound to
the wrong table — e.g. a fact column (Hospital, Unit) bound to a synthetic
DimDate entity that has no such column. Such bindings produce empty/broken
visuals in Power BI Desktop but pass tmdl-validate and validate_pbip.

Usage:
    python validate_bindings.py <output_dir>
    python validate_bindings.py Output/MidnightCensusDashboard

Exit codes: 0 = clean, 2 = one or more broken bindings (errors).
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, Set, Tuple

_TABLE_RE = re.compile(r"^table\s+(.+?)\s*$")
_COL_RE = re.compile(r"^(?:column|measure)\s+(.+?)\s*(?:=.*)?$")


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name.endswith("'"):
        return name[1:-1]
    return name


def parse_model(sm_def: str) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """Return ({entity: {member names}}, {all measure names}) from TMDL tables."""
    entities: Dict[str, Set[str]] = {}
    measures: Set[str] = set()
    tdir = os.path.join(sm_def, "tables")
    if not os.path.isdir(tdir):
        return entities, measures
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".tmdl"):
            continue
        cur = None
        with open(os.path.join(tdir, fn), encoding="utf-8-sig") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped or stripped.startswith("///"):
                    continue
                # table declaration lives at column 0 (no leading whitespace)
                if not (raw[:1] in (" ", "\t")):
                    m = _TABLE_RE.match(stripped)
                    if m:
                        cur = _unquote(m.group(1))
                        entities.setdefault(cur, set())
                    continue
                if cur is None:
                    continue
                kw = stripped.split(None, 1)[0]
                if kw in ("column", "measure"):
                    m = _COL_RE.match(stripped)
                    if m:
                        nm = _unquote(m.group(1))
                        entities[cur].add(nm)
                        if kw == "measure":
                            measures.add(nm)
    return entities, measures


def _iter_bindings(report_def: str):
    """Yield (visual_folder, kind, entity, prop) for every visual field binding."""
    pages = os.path.join(report_def, "pages")
    if not os.path.isdir(pages):
        return
    for page in sorted(os.listdir(pages)):
        vdir = os.path.join(pages, page, "visuals")
        if not os.path.isdir(vdir):
            continue
        for folder in sorted(os.listdir(vdir)):
            path = os.path.join(vdir, folder, "visual.json")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8-sig") as fh:
                v = json.load(fh)
            qs = (v.get("visual", {}).get("query", {})
                  .get("queryState", {}))
            for _role, state in qs.items():
                for p in state.get("projections", []):
                    f = p.get("field", {})
                    if "Column" in f:
                        kind, cm = "column", f["Column"]
                    elif "Measure" in f:
                        kind, cm = "measure", f["Measure"]
                    else:
                        continue
                    ent = (cm.get("Expression", {})
                           .get("SourceRef", {}).get("Entity"))
                    prop = cm.get("Property")
                    if ent and prop:
                        yield folder, kind, ent, prop


def validate(output_dir: str) -> int:
    model_name = os.path.basename(os.path.normpath(output_dir))
    sm_def = os.path.join(output_dir, f"{model_name}.SemanticModel", "definition")
    report_def = os.path.join(output_dir, f"{model_name}.Report", "definition")
    entities, measures = parse_model(sm_def)
    if not entities:
        print(f"  validate_bindings: no TMDL tables under {sm_def} — skipped")
        return 0

    errors = []
    warnings = []
    for folder, kind, ent, prop in _iter_bindings(report_def):
        members = entities.get(ent)
        if members is None:
            errors.append(f"{folder}: '{ent}' is not a model table "
                          f"(field '{prop}')")
            continue
        if prop in members:
            continue
        if kind == "measure" and prop in measures:
            warnings.append(f"{folder}: measure '{prop}' bound to '{ent}' but "
                            f"lives on another table")
            continue
        errors.append(f"{folder}: column '{prop}' does not exist on table "
                      f"'{ent}'")

    for w in warnings:
        print(f"  warn  [binding] {w}")
    for e in errors:
        print(f"  error [binding] {e}")
    print(f"Binding integrity: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 2 if errors else 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_bindings.py <output_dir>", file=sys.stderr)
        return 3
    return validate(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
