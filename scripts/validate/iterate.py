"""iterate.py — iteration history + early-termination logic.

Backs two lifecycle requirements:

  * #2 Single workbook across iterations — the per-iteration state (counts,
    unresolved-issue signatures, status) is persisted to
    ``validation/iterations.json`` so the SAME workbook can append a new
    ``Iterations`` row each cycle instead of starting over.
  * #3 Early termination — if the SAME unresolved issues reappear in two
    consecutive iterations with no measurable improvement, the validator signals
    "stop": no further cycles, no PBIP regeneration; the workbook records the
    repeated issue, its root cause and the recommended manual action.

An "unresolved issue" is any validation record whose Match Status is not a pass
(pass = Exact Match / Equivalent / Match / Added Control).
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

_PASS_STATUSES = {"exact match", "equivalent", "match", "added control", "pass"}


def history_path(out_dir: str) -> str:
    return os.path.join(out_dir, "validation", "iterations.json")


def load_history(out_dir: str) -> List[Dict]:
    p = history_path(out_dir)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data.get("iterations", []) if isinstance(data, dict) else (data or [])


def _is_pass(status: str) -> bool:
    return (status or "").strip().lower() in _PASS_STATUSES


def collect_issues(visuals: List[Dict], measures: List[Dict],
                   filters: List[Dict]) -> List[Dict]:
    """Stable, comparable signatures for every non-passing validation record."""
    issues: List[Dict] = []
    for r in visuals:
        if not _is_pass(r["matchStatus"]):
            ident = r.get("tableauWorksheet") or r.get("powerBiVisual")
            issues.append({
                "id": f"visual::{ident}::{r['matchStatus']}",
                "category": "Visual",
                "subject": ident,
                "status": r["matchStatus"],
                "detail": r.get("justification") or r.get("observations", ""),
            })
    for r in measures:
        if not _is_pass(r["matchStatus"]):
            issues.append({
                "id": f"measure::{r['tableauField']}::{r['matchStatus']}",
                "category": "Measure",
                "subject": r["tableauField"],
                "status": r["matchStatus"],
                "detail": r.get("observations", ""),
            })
    for r in filters:
        if not _is_pass(r["matchStatus"]):
            issues.append({
                "id": f"filter::{r['field']}::{r['matchStatus']}",
                "category": "Filter",
                "subject": r["field"],
                "status": r["matchStatus"],
                "detail": r.get("observations", ""),
            })
    return issues


def issue_ids(issues: List[Dict]) -> set:
    return {i["id"] for i in issues}


def evaluate_termination(history: List[Dict],
                         current_issues: List[Dict]) -> Tuple[bool, List[Dict]]:
    """Decide whether to stop iterating.

    Returns ``(stop, repeated_issues)``. We stop when the previous iteration's
    unresolved-issue set is non-empty and the current set is identical (no issue
    resolved, none newly introduced) — i.e. two consecutive iterations with no
    measurable improvement.
    """
    if not history:
        return False, []
    prev = history[-1]
    prev_ids = set(prev.get("issueIds", []))
    cur_ids = issue_ids(current_issues)
    if not cur_ids:
        return False, []
    if prev_ids and cur_ids == prev_ids:
        repeated = [i for i in current_issues if i["id"] in prev_ids]
        return True, repeated
    return False, []


def diagnose(issue: Dict) -> Dict:
    """Root cause + why auto-correction failed + recommended manual action."""
    cat, status = issue["category"], issue["status"]
    subject = issue["subject"]
    if cat == "Visual" and status == "Missing":
        return {
            "rootCause": ("The Tableau worksheet could not be bound to a Power BI "
                          "visual (unrecognised mark or no bindable field)."),
            "whyNoAutoFix": ("The deterministic engine and gap-fill agent produced "
                             "no visualDecision that resolves this worksheet."),
            "manualAction": (f"In Power BI Desktop add a visual reproducing '{subject}' "
                             "and bind its category/value fields manually."),
        }
    if cat == "Visual" and status == "Extra":
        return {
            "rootCause": "A Power BI visual exists with no Tableau source worksheet.",
            "whyNoAutoFix": "Removing it could discard an intentional helper visual.",
            "manualAction": f"Review the extra visual '{subject}' and delete if unintended.",
        }
    if cat == "Measure":
        return {
            "rootCause": ("The generated DAX diverges from the Tableau formula on a "
                          "flagged dimension (aggregation / null / conditional / time)."),
            "whyNoAutoFix": ("The translation is ambiguous; a safe deterministic "
                             "rewrite is not available."),
            "manualAction": (f"Review measure '{subject}' DAX against the Tableau "
                             "formula and adjust the flagged logic."),
        }
    return {
        "rootCause": ("The filter/parameter behaviour could not be confirmed "
                      "equivalent automatically."),
        "whyNoAutoFix": "Behaviour equivalence needs visual confirmation.",
        "manualAction": (f"Verify '{subject}' filtering in Power BI Desktop and add "
                         "a slicer if the interaction differs."),
    }


def make_iteration_record(iteration: int, visuals: List[Dict],
                          measures: List[Dict], filters: List[Dict],
                          issues: List[Dict], stopped_early: bool,
                          timestamp: str) -> Dict:
    def _counts(records, pass_test):
        total = len(records)
        passed = sum(1 for r in records if pass_test(r["matchStatus"]))
        return total, passed
    v_total, v_pass = _counts(visuals, _is_pass)
    m_total, m_pass = _counts(measures, _is_pass)
    f_total, f_pass = _counts(filters, _is_pass)
    return {
        "iteration": iteration,
        "timestamp": timestamp,
        "visualsTotal": v_total, "visualsPassed": v_pass,
        "measuresTotal": m_total, "measuresPassed": m_pass,
        "filtersTotal": f_total, "filtersPassed": f_pass,
        "unresolvedCount": len(issues),
        "issueIds": sorted(issue_ids(issues)),
        "stoppedEarly": stopped_early,
        "status": ("Stopped (repeated unresolved issues)" if stopped_early
                   else ("Validated" if not issues else "Issues found")),
    }


def append_iteration(out_dir: str, record: Dict) -> None:
    history = load_history(out_dir)
    history.append(record)
    p = history_path(out_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"iterations": history}, fh, indent=2, ensure_ascii=False)


def next_iteration_number(out_dir: str) -> int:
    history = load_history(out_dir)
    return (history[-1]["iteration"] + 1) if history else 1
