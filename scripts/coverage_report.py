"""coverage_report.py — interactivity & feature fidelity report for a migration.

Answers one question precisely: for a given Tableau workbook, WHAT did the
parser detect, and HOW MUCH of it does the emitter actually reproduce in the
Power BI (.pbip) output?  It reads the deterministic IR (analysis.json) and
scores every feature against a capability matrix that reflects the *current*
emitter behaviour (emit_pbir.py / emit_tmdl.py).

Status legend
-------------
  emitted   the feature is reproduced in the .pbip
  partial   reproduced with reduced fidelity / only under conditions
  gap       detected but NOT reproduced (lost in migration)
  n/a       not present in this workbook (nothing to migrate)

Usage
-----
  python scripts/coverage_report.py "Output/NetfixWorkbook"      # one workbook
  python scripts/coverage_report.py --all                        # every Output/*
  python scripts/coverage_report.py "Output/NetfixWorkbook" --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT = os.path.join(HERE, "..", "Output")

# Capability matrix — what the emitter reproduces today. Each entry maps an IR
# feature to (status, note). "gap" entries are detected but not emitted; closing
# them is tracked separately. Keep this in sync with emit_pbir.py / emit_tmdl.py.
EMITTED = "emitted"
PARTIAL = "partial"
GAP = "gap"

CAPABILITY = {
    "worksheets":     (EMITTED, "each worksheet -> a report visual"),
    "dashboardZones": (EMITTED, "viz/filter/paramctrl/text/image zones positioned on the page"),
    "filterControls": (EMITTED, "Tableau filter cards -> Power BI slicers"),
    "paramControls":  (EMITTED, "parameter controls -> slicers"),
    "relationships":  (EMITTED, "physical relationships -> model relationships"),
    "hierarchies":    (EMITTED, "Tableau hierarchies -> model hierarchies"),
    "calcMeasures":   (EMITTED, "calculated fields -> DAX measures/columns"),
    "rls":            (PARTIAL, "detected; emitted as model roles where unambiguous"),
    "parameters":     (PARTIAL, "value carried; interactive what-if not fully wired"),
    "groups":         (PARTIAL, "emitted as grouping columns where simple"),
    "bins":           (PARTIAL, "emitted as binned columns where simple"),
    "sets":           (GAP,     "Tableau sets have no direct emit yet"),
    "buttons":        (EMITTED, "goto-sheet -> page-navigation actionButton; toggle -> bookmark pair + stacked buttons"),
    "actionFilter":   (PARTIAL, "Power BI cross-filters visuals by default (interaction reproduced, not explicitly wired)"),
    "actionHighlight":(GAP,     "highlight actions -> cross-highlight not wired"),
    "actionUrl":      (GAP,     "URL/goto actions -> drillthrough/links not emitted"),
    "bookmarks":      (PARTIAL, "auto-generated for toggle buttons; user-defined Tableau bookmarks not extracted"),
}

STATUS_MARK = {EMITTED: "[+]", PARTIAL: "[~]", GAP: "[!]", "n/a": "[ ]"}


def _load_ir(path: str) -> Optional[Dict]:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def inventory(ir: Dict) -> Dict[str, int]:
    """Count each interactivity / model feature present in the IR."""
    dashes = ir.get("dashboards", []) or []
    actions = ir.get("actions", []) or []
    zones = [z for d in dashes for z in (d.get("zones", []) or [])]
    counts = {
        "worksheets":     len(ir.get("worksheets", []) or []),
        "dashboardZones": len(zones),
        "filterControls": sum(1 for z in zones if z.get("type") == "filter"),
        "paramControls":  sum(1 for z in zones if z.get("type") == "paramctrl"),
        "relationships":  len(ir.get("relationships", []) or []),
        "hierarchies":    len(ir.get("hierarchies", []) or []),
        "calcMeasures":   len(ir.get("calculatedFields", []) or []),
        "rls":            1 if (ir.get("rls") or {}).get("detected") else 0,
        "parameters":     len(ir.get("parameters", []) or []),
        "groups":         len(ir.get("groups", []) or []),
        "bins":           len(ir.get("bins", []) or []),
        "sets":           len(ir.get("sets", []) or []),
        "buttons":        sum(len(d.get("buttons", []) or []) for d in dashes),
        "actionFilter":   sum(1 for a in actions if a.get("type") == "filter"),
        "actionHighlight":sum(1 for a in actions if a.get("type") == "highlight"),
        "actionUrl":      sum(1 for a in actions if a.get("type") in ("url", "other")),
        "bookmarks":      0,  # never extracted
    }
    return counts


def score(counts: Dict[str, int]) -> Tuple[List[Tuple], Dict[str, int]]:
    """Return per-feature rows and a roll-up of detected/emitted feature kinds."""
    rows = []
    detected_kinds = emitted_kinds = gap_kinds = 0
    for key, (status, note) in CAPABILITY.items():
        n = counts.get(key, 0)
        eff = status if n > 0 else "n/a"
        rows.append((key, n, eff, note))
        if n > 0:
            detected_kinds += 1
            if status == EMITTED:
                emitted_kinds += 1
            elif status == GAP:
                gap_kinds += 1
    roll = {
        "detectedKinds": detected_kinds,
        "emittedKinds": emitted_kinds,
        "gapKinds": gap_kinds,
    }
    return rows, roll


def report_one(folder: str, as_json: bool = False) -> Optional[Dict]:
    name = os.path.basename(os.path.normpath(folder))
    ir = _load_ir(os.path.join(folder, "analysis.json"))
    if ir is None:
        print(f"  (no analysis.json in {folder} — run migrate first)")
        return None
    counts = inventory(ir)
    rows, roll = score(counts)
    result = {"workbook": name, "rollup": roll, "features": [
        {"feature": k, "count": n, "status": s, "note": note} for k, n, s, note in rows]}

    if as_json:
        return result

    print(f"\n=== {name} ===")
    print(f"  {'feature':<16}{'count':>6}  status   note")
    print(f"  {'-'*16}{'-'*6}--{'-'*8}-{'-'*40}")
    for k, n, s, note in rows:
        mark = STATUS_MARK.get(s, "[ ]")
        cnt = str(n) if n else "-"
        print(f"  {k:<16}{cnt:>6}  {mark} {s:<7} {note if n else ''}")
    gaps = [(k, n) for k, n, s, _ in rows if s == GAP and n]
    print(f"  ---")
    print(f"  detected feature kinds : {roll['detectedKinds']}")
    print(f"  fully emitted          : {roll['emittedKinds']}")
    print(f"  GAPS (lost)            : {roll['gapKinds']}"
          + (f"  -> {', '.join(f'{k}({n})' for k, n in gaps)}" if gaps else ""))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tableau->PBI interactivity coverage report")
    ap.add_argument("folder", nargs="?", help="Output/<Workbook> folder (omit with --all)")
    ap.add_argument("--all", action="store_true", help="report every Output/* workbook")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    if args.all:
        root = OUTPUT_ROOT
        folders = [os.path.join(root, d) for d in sorted(os.listdir(root))
                   if os.path.isdir(os.path.join(root, d))]
    elif args.folder:
        folders = [args.folder]
    else:
        ap.error("provide a folder or --all")
        return 2

    results = [r for f in folders if (r := report_one(f, args.json)) is not None]
    if args.json:
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
