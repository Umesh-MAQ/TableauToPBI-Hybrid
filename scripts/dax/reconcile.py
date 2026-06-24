"""reconcile.py — guard that no measure-kind calc is silently dropped.

The prepare stage writes every untranslatable calc to ``dax-partial.json`` under
``pending`` so the agent can author its DAX in ``decisions.json``. ``emit_tmdl``
only emits measures that exist in ``decisions.json`` — so if the agent forgets a
``pending`` measure, it vanishes from the model with no error.

This module cross-checks the two artifacts. Every ``pending`` calc whose
``suggestedDaxKind == "measure"`` must be accounted for in ``decisions.json`` (as a
measure, calculated column, field parameter, or parameter table). Anything missing
is written to ``measures-todo.json`` and reported as an AGENT ACTION so the agent
re-authors it before generation proceeds.

Exit codes: 0 = all accounted for, 4 = missing measures (agent must act),
2 = usage / file error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Set


def _norm(name: str) -> str:
    """Case/punctuation-insensitive key for matching captions to names."""
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


def _load(path: str) -> Dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def accounted_names(decisions: Dict) -> Set[str]:
    """Normalized names the agent has already resolved, across every channel."""
    names: Set[str] = set()
    for m in decisions.get("measures", []):
        names.add(_norm(m.get("name", "")))
    for c in decisions.get("calculatedColumns", []) or []:
        names.add(_norm(c.get("name", "")))
    for fp in decisions.get("fieldParameters", []) or []:
        names.add(_norm(fp.get("name", "")))
    for t in decisions.get("tables", []) or []:
        if t.get("role") == "param":
            names.add(_norm(t.get("name", "")))
        for c in t.get("calculatedColumns", []) or []:
            names.add(_norm(c.get("name", "")))
    return names


def find_missing(partial: Dict, decisions: Dict) -> List[Dict]:
    """Pending measure-kind calcs not present in any decisions channel."""
    done = accounted_names(decisions)
    # deterministic measures are folded in by emit_tmdl, so treat them as done too
    for m in partial.get("measures", []):
        done.add(_norm(m.get("name", "")))
    missing: List[Dict] = []
    for p in partial.get("pending", []):
        if p.get("suggestedDaxKind", "measure") != "measure":
            continue
        if _norm(p.get("caption", "")) in done:
            continue
        missing.append(p)
    return missing


def reconcile(analysis_path: str, decisions_path: str) -> int:
    out_dir = os.path.dirname(os.path.abspath(analysis_path))
    partial_path = os.path.join(out_dir, "dax-partial.json")
    if not os.path.isfile(partial_path):
        print(f"  reconcile: no dax-partial.json in {out_dir}; nothing to check")
        return 0
    partial, decisions = _load(partial_path), _load(decisions_path)
    missing = find_missing(partial, decisions)
    todo_path = os.path.join(out_dir, "measures-todo.json")
    if not missing:
        if os.path.exists(todo_path):
            os.remove(todo_path)  # previous gap now resolved
        print("  reconcile: all pending measures accounted for in decisions.json")
        return 0
    with open(todo_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"missingMeasures": missing}, fh, indent=2, ensure_ascii=False)
    print("\n=== AGENT ACTION REQUIRED: missing measures ===")
    print(f"  {len(missing)} pending measure(s) are NOT in decisions.json and would")
    print("  be silently dropped from the model. Author DAX for each in decisions.json")
    print(f"  ('measures' array), then re-run generate. Details: {todo_path}")
    for p in missing:
        print(f"    - {p.get('caption')}  ({p.get('complexity')})  <= {p.get('formula')}")
    return 4


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every pending measure is authored in decisions.json.")
    parser.add_argument("analysis", help="path to analysis.json")
    parser.add_argument("--decisions", required=True, help="path to decisions.json")
    args = parser.parse_args(argv)
    for p in (args.analysis, args.decisions):
        if not os.path.isfile(p):
            print(f"ERROR: file not found: {p}", file=sys.stderr)
            return 2
    return reconcile(args.analysis, args.decisions)


if __name__ == "__main__":
    raise SystemExit(main())
