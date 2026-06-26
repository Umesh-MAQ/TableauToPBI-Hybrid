"""model_read.py — load every artifact the validator compares.

Reads, in one place, the four sources of truth a post-generate validation needs:

  * ``analysis.json``  — the Tableau IR (source worksheets, calc fields, filters,
    parameters, dashboards). The authoritative "what the Tableau workbook does".
  * ``decisions.json`` — the merged build spec (DAX measures, visual decisions,
    field parameters). The authoritative "what we asked Power BI to build".
  * the emitted ``*.Report`` PBIR — the ACTUAL Power BI visuals on disk
    (visualType + field bindings + title), so the validator checks the real
    output rather than the intent.
  * the emitted ``*.SemanticModel`` TMDL — the ACTUAL measures Power BI will load.

Everything here is read-only and standard-library only.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional


def _load_json(path: str) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, ValueError):
        return None  # empty or partially-written file — treat as absent


def model_name_for(out_dir: str, decisions: Optional[dict]) -> str:
    if decisions and decisions.get("modelName"):
        return decisions["modelName"]
    return os.path.basename(os.path.normpath(out_dir))


# --------------------------------------------------------------------------- #
# emitted PBIR report — the real visuals on disk
# --------------------------------------------------------------------------- #
def _title_text(visual: dict) -> Optional[str]:
    """Pull the visible title text out of a visual.json container objects block."""
    objs = (visual.get("visual", {}) or {}).get("visualContainerObjects", {}) or {}
    for block in objs.get("title", []) or []:
        props = block.get("properties", {}) or {}
        text = props.get("text", {})
        lit = (((text or {}).get("expr", {}) or {}).get("Literal", {}) or {}).get("Value")
        if isinstance(lit, str):
            return lit.strip("'")
    return None


def _bindings(visual: dict) -> List[Dict[str, str]]:
    """Flatten every field binding (entity.property, measure vs column)."""
    out: List[Dict[str, str]] = []
    qs = (((visual.get("visual", {}) or {}).get("query", {}) or {})
          .get("queryState", {}) or {})
    for role, state in qs.items():
        for p in (state or {}).get("projections", []) or []:
            f = p.get("field", {}) or {}
            if "Column" in f:
                kind, cm = "column", f["Column"]
            elif "Measure" in f:
                kind, cm = "measure", f["Measure"]
            elif "Aggregation" in f:
                kind = "measure"
                cm = (f["Aggregation"].get("Expression", {}) or {}).get("Column", {}) or {}
            else:
                continue
            ent = (cm.get("Expression", {}) or {}).get("SourceRef", {}).get("Entity")
            prop = cm.get("Property")
            out.append({"role": role, "kind": kind,
                        "entity": ent or "", "property": prop or ""})
    return out


def read_emitted_visuals(out_dir: str, model_name: str) -> List[Dict]:
    """Enumerate every emitted visual: page, name, visualType, title, bindings."""
    report_def = os.path.join(out_dir, f"{model_name}.Report", "definition")
    pages = os.path.join(report_def, "pages")
    visuals: List[Dict] = []
    if not os.path.isdir(pages):
        return visuals
    for page in sorted(os.listdir(pages)):
        page_json = _load_json(os.path.join(pages, page, "page.json")) or {}
        page_label = page_json.get("displayName") or page
        page_w = page_json.get("width") or 1280
        page_h = page_json.get("height") or 720
        vdir = os.path.join(pages, page, "visuals")
        if not os.path.isdir(vdir):
            continue
        for folder in sorted(os.listdir(vdir)):
            v = _load_json(os.path.join(vdir, folder, "visual.json"))
            if not v:
                continue
            vis = v.get("visual", {}) or {}
            vtype = vis.get("visualType") or ""
            pos = v.get("position", {}) or {}
            visuals.append({
                "page": page_label,
                "name": v.get("name") or folder,
                "folder": folder,
                "visualType": vtype,
                "title": _title_text(v),
                "bindings": _bindings(v),
                "isSlicer": vtype == "slicer",
                "isControl": vtype in ("slicer", "actionButton", "textbox"),
                "position": {
                    "x": pos.get("x", 0), "y": pos.get("y", 0),
                    "width": pos.get("width", 0), "height": pos.get("height", 0),
                },
                "pageWidth": page_w,
                "pageHeight": page_h,
            })
    return visuals


# --------------------------------------------------------------------------- #
# emitted TMDL — the real measures on disk
# --------------------------------------------------------------------------- #
_MEASURE_RE = re.compile(r"^\s*measure\s+(?P<name>'[^']+'|\S+)\s*=\s*(?P<expr>.*)$")


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name.endswith("'"):
        return name[1:-1]
    return name


def read_emitted_measures(out_dir: str, model_name: str) -> Dict[str, str]:
    """Return {measure name: first-line DAX} for every measure in the TMDL model."""
    sm_def = os.path.join(out_dir, f"{model_name}.SemanticModel", "definition")
    tdir = os.path.join(sm_def, "tables")
    measures: Dict[str, str] = {}
    if not os.path.isdir(tdir):
        return measures
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".tmdl"):
            continue
        with open(os.path.join(tdir, fn), encoding="utf-8-sig") as fh:
            lines = fh.readlines()
        for i, raw in enumerate(lines):
            m = _MEASURE_RE.match(raw)
            if not m:
                continue
            name = _unquote(m.group("name"))
            expr = m.group("expr").strip()
            if not expr:  # multi-line measure: take the next non-empty line
                for nxt in lines[i + 1:]:
                    if nxt.strip():
                        expr = nxt.strip()
                        break
            measures[name] = expr
    return measures


class MigrationArtifacts:
    """Bundle of everything the comparators read for one migration."""

    def __init__(self, out_dir: str):
        self.out_dir = os.path.abspath(out_dir)
        self.analysis = _load_json(os.path.join(self.out_dir, "analysis.json")) or {}
        self.decisions = _load_json(os.path.join(self.out_dir, "decisions.json")) or {}
        self.result = _load_json(os.path.join(self.out_dir, "MIGRATION_RESULT.json")) or {}
        self.model_name = model_name_for(self.out_dir, self.decisions)
        self.emitted_visuals = read_emitted_visuals(self.out_dir, self.model_name)
        self.emitted_measures = read_emitted_measures(self.out_dir, self.model_name)

    # convenience accessors -------------------------------------------------- #
    @property
    def worksheets(self) -> List[dict]:
        return self.analysis.get("worksheets", []) or []

    @property
    def dashboards(self) -> List[dict]:
        return self.analysis.get("dashboards", []) or []

    @property
    def calc_fields(self) -> List[dict]:
        return self.analysis.get("calculatedFields", []) or []

    @property
    def parameters(self) -> List[dict]:
        return self.analysis.get("parameters", []) or []

    @property
    def decision_measures(self) -> List[dict]:
        return self.decisions.get("measures", []) or []

    @property
    def visual_decisions(self) -> List[dict]:
        return self.decisions.get("visualDecisions", []) or []

    @property
    def field_parameters(self) -> List[dict]:
        return self.decisions.get("fieldParameters", []) or []
