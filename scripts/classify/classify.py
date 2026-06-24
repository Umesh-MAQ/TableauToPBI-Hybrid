"""classify.py — deterministic split of the migration workload (Stage 6.5).

Runs after map_dax (which already wrote dax-partial.json) and before the agent
gap-fill. Routing is BINARY, not a three-tier easy/medium/hard split:

    route = "deterministic"  if map_dax.translate() succeeds for a measure-kind calc
                             (the DAX is already in dax-partial.json), OR the schema
                             is the unambiguous single-flat case (star_det built it)
    route = "agent"          for everything else — one batched Sonnet call authors
                             the remaining measures and (if needed) the star schema

There is no separate "hard" bucket: Opus is a reactive fallback wired into the
orchestrator only when merge-time validation fails, never a pre-classification.

Outputs (next to analysis.json):
  * classification.json — the routing map (tableMap, schema route, per-measure route)
  * schema-easy.json     — the deterministic single-flat schema fragment (omitted if
                           the schema routes to the agent)
  * agent-todo.json      — the self-contained work list the combined agent call reads
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "dax"))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "schema"))

import map_dax  # noqa: E402
import star_det  # noqa: E402

# Pattern tags that only enrich the agent prompt — they never change routing.
_HINT_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("lod-fixed", re.compile(r"\{\s*FIXED", re.IGNORECASE)),
    ("lod-include", re.compile(r"\{\s*INCLUDE", re.IGNORECASE)),
    ("lod-exclude", re.compile(r"\{\s*EXCLUDE", re.IGNORECASE)),
    ("table-calc", re.compile(
        r"\b(WINDOW_\w+|RUNNING_\w+|INDEX|RANK|LOOKUP|PREVIOUS_VALUE|FIRST|LAST|TOTAL)\s*\(",
        re.IGNORECASE)),
    ("date-fn", re.compile(r"\b(DATEPARSE|DATEADD|DATETRUNC|DATEDIFF)\s*\(", re.IGNORECASE)),
)


def _hint(formula: str) -> Optional[str]:
    """Best-effort pattern tag for a formula, or None. Prompt enrichment only."""
    for tag, rx in _HINT_PATTERNS:
        if rx.search(formula or ""):
            return tag
    return None


def _table_map_from_detect(det: Dict, ir: Dict) -> Dict:
    """Canonical table names shared by the DAX and schema steps."""
    return {
        "fact": det["fact"] or ir.get("workbook", {}).get("pascalName", "Model"),
        "dimensions": det["dimensions"],
        "strategy": det["strategy"],
    }


def _ambiguous_visuals(ir: Dict) -> List[Dict]:
    """Worksheets the deterministic mark heuristic could NOT resolve.

    ``inferredVisualType is None`` means the Tableau mark is 'Automatic' and the
    shelf shape is genuinely ambiguous (e.g. several measures over one-or-more
    dimensions — a multi-series line, a combo chart, a matrix or a detail table).
    Rather than silently defaulting these to a table, hand the agent the resolved
    shelf facts so it can author a ``visualDecisions`` entry. The emitter still
    falls back to a table only when no decision is supplied, so the run is never
    blocked by an unanswered visual.
    """
    todo: List[Dict] = []
    for ws in ir.get("worksheets", []):
        if ws.get("inferredVisualType") is not None:
            continue
        todo.append({
            "worksheet": ws.get("name"),
            "markClass": ws.get("markClass"),
            "categoryField": ws.get("categoryField"),
            "categoryDateLevel": ws.get("categoryDateLevel"),
            "dimensions": ws.get("dimensions", []),
            "values": ws.get("values", []),
            "measures": [m.get("field") for m in (ws.get("measures") or [])
                         if m.get("field")],
            "rows": ws.get("rows", []),
            "cols": ws.get("cols", []),
            "encodings": ws.get("encodings", {}),
            "orientation": ws.get("orientation"),
            "title": ws.get("title"),
        })
    return todo


def classify(ir: Dict) -> Dict:
    """Produce the classification + the agent work list from the IR.

    Returns {classification, schemaFragment, agentTodo}. ``schemaFragment`` is the
    deterministic single-flat fragment or None; the caller persists each artifact.
    """
    det = star_det.detect(ir)
    table_map = _table_map_from_detect(det, ir)
    host = table_map["fact"]

    # Reuse map_dax's exact routing so classify and the emitted dax-partial.json
    # never disagree about which measures are deterministic.
    split = map_dax.build_measures(ir, host)
    det_names = {m["name"] for m in split["measures"]}

    columns = sorted(map_dax._base_columns(ir))
    sibling_measures = sorted(set(map_dax._measure_refs(ir).values()))

    measures: List[Dict] = []
    agent_measures: List[Dict] = []
    for field in ir.get("calculatedFields", []):
        caption = field.get("caption")
        if not caption:
            continue
        kind = field.get("suggestedDaxKind", "measure")
        name = map_dax.measure_name(caption)
        is_det = kind == "measure" and name in det_names
        route = "deterministic" if is_det else "agent"
        hint = None if is_det else _hint(field.get("formula", ""))
        measures.append({"caption": caption, "route": route, "kind": kind, "hint": hint})
        if not is_det:
            agent_measures.append({
                "caption": caption,
                "name": name,
                "formula": field.get("formula", ""),
                "kind": kind,
                "hint": hint,
                "dataType": field.get("dataType"),
            })

    schema_fragment = star_det.build_star(ir)
    schema_route = "deterministic" if schema_fragment else "agent"

    det_count = sum(1 for m in measures if m["route"] == "deterministic")
    agent_count = len(measures) - det_count

    ambiguous_visuals = _ambiguous_visuals(ir)

    classification = {
        "classificationVersion": "1.0",
        "tableMap": table_map,
        "schema": {
            "route": schema_route,
            "reason": det["reason"],
        },
        "measures": measures,
        "counts": {
            "deterministicMeasures": det_count,
            "agentMeasures": agent_count,
            "ambiguousVisuals": len(ambiguous_visuals),
        },
    }

    agent_todo = {
        "modelName": ir.get("workbook", {}).get("pascalName", "Model"),
        "tableMap": table_map,
        "schemaNeeded": schema_fragment is None,
        "measures": agent_measures,
        "visuals": ambiguous_visuals,
        "context": {
            "baseColumns": columns,
            "siblingMeasures": sibling_measures,
            # full parameter objects (name, dataType, domainType, default, values) so
            # the agent can author param datatables when it owns the schema design.
            "parameters": ir.get("parameters", []),
        },
    }

    return {
        "classification": classification,
        "schemaFragment": schema_fragment,
        "agentTodo": agent_todo,
    }


def write_outputs(result: Dict, analysis_path: str) -> Dict[str, str]:
    """Persist classification.json, schema-easy.json (if any), agent-todo.json."""
    out_dir = os.path.dirname(os.path.abspath(analysis_path))
    paths: Dict[str, str] = {}

    cls_path = os.path.join(out_dir, "classification.json")
    with open(cls_path, "w", encoding="utf-8") as fh:
        json.dump(result["classification"], fh, indent=2, ensure_ascii=False)
    paths["classification"] = cls_path

    if result["schemaFragment"] is not None:
        schema_path = os.path.join(out_dir, "schema-easy.json")
        with open(schema_path, "w", encoding="utf-8") as fh:
            json.dump(result["schemaFragment"], fh, indent=2, ensure_ascii=False)
        paths["schema"] = schema_path

    todo_path = os.path.join(out_dir, "agent-todo.json")
    with open(todo_path, "w", encoding="utf-8") as fh:
        json.dump(result["agentTodo"], fh, indent=2, ensure_ascii=False)
    paths["agentTodo"] = todo_path

    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Binary-route Tableau calcs/schema into deterministic vs agent work.")
    parser.add_argument("analysis", help="path to analysis.json (IR)")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.analysis):
        print(f"ERROR: file not found: {args.analysis}", file=sys.stderr)
        return 2

    with open(args.analysis, encoding="utf-8-sig") as fh:
        ir = json.load(fh)

    result = classify(ir)
    paths = write_outputs(result, args.analysis)

    cls = result["classification"]
    print(f"classify: schema={cls['schema']['route']} "
          f"measures det={cls['counts']['deterministicMeasures']} "
          f"agent={cls['counts']['agentMeasures']}")
    for label, path in paths.items():
        print(f"  wrote {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
