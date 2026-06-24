"""parse_twb.py — Stage 1 deterministic Tableau parser (CLI entry point).

Replaces the token-heavy "agent reads raw XML" stage. Converts a .twb file into
analysis.json (the IR) written INSIDE Output/{WorkbookName}/. The agent later
reads this slim IR instead of the raw workbook XML.

Usage:
    python parse_twb.py "Data/Midnight Census/Midnight Census Dashboard.twb"
    python parse_twb.py <twb> --output-root Output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twb_xml as X  # noqa: E402
import twb_datasources as DS  # noqa: E402
import twb_visuals as V  # noqa: E402
import twb_fields as F  # noqa: E402
import twb_meta as M  # noqa: E402

IR_VERSION = "1.0"


def build_ir(twb_path: str) -> Dict:
    """Parse a .twb file and assemble the full IR dictionary."""
    root = X.load_twb(twb_path)
    wb_name = os.path.splitext(os.path.basename(twb_path))[0]
    pascal = X.to_pascal_case(wb_name)
    active = V.worksheet_datasources(root)
    calc_fields = DS.extract_calculated_fields(root)
    columns = DS.extract_columns(root)
    parameters = DS.extract_parameters(root)
    calc_map = F.build_calc_map(calc_fields)
    param_map = F.build_param_map(parameters)
    measures = F.measure_captions(calc_fields, columns)

    # Inline inter-calc references ([Calculation_<id>] / [Name (copy)_<hash>] ->
    # [Caption]) so the IR formulas are readable and the DAX translator can
    # resolve measure refs. Uses the full calc-name map (includes duplicated calcs).
    formula_ref_map = M.build_calc_name_map(root)
    for c in calc_fields:
        c["formula"] = F.resolve_formula_refs(c.get("formula"), formula_ref_map)
        c["dependsOn"] = F.formula_dependencies(c["formula"])

    # Enrich columns + column->table map with authoritative physical lineage from
    # <metadata-records> (true type, owning physical table, original column name).
    col_meta = M.extract_column_metadata(root)
    for c in columns:
        m = col_meta.get(c["name"])
        if m:
            c["physicalTable"] = m["parentTable"]
            c["remoteName"] = m["remoteName"]
            c["metaType"] = m["metaType"]
    column_table_map = M.extract_column_table_map(root)
    for name, meta in col_meta.items():
        if name not in column_table_map and meta["parentTable"]:
            column_table_map[name] = meta["parentTable"]

    return {
        "irVersion": IR_VERSION,
        "workbook": {
            "name": wb_name,
            "pascalName": pascal,
            "sourcePath": twb_path.replace("\\", "/"),
            "version": X.attr(root, "version", ""),
            "platform": X.attr(root, "platform", ""),
        },
        "dataSources": DS.extract_datasources(root, active),
        "columns": columns,
        "calculatedFields": calc_fields,
        "parameters": parameters,
        "worksheets": V.extract_worksheets(root, calc_map, measures, param_map),
        "dashboards": V.extract_dashboards(root, calc_map, param_map),
        "actions": V.extract_actions(root, calc_map),
        "physicalTables": M.extract_physical_tables(root),
        "columnTableMap": column_table_map,
        "relationships": M.extract_relationships(root),
        "hierarchies": M.extract_hierarchies(root),
        "sets": _extract_sets(root),
        "groups": M.extract_groups(root),
        "bins": M.extract_bins(root),
        "theme": M.extract_theme(root),
        "blending": _detect_blending(root),
        "rls": _detect_rls(root, calc_fields, column_table_map),
    }


def _extract_sets(root) -> list:
    """Lightweight set extraction (name + source field + members)."""
    sets = []
    seen = set()
    for grp in root.iter("group"):
        name = X.attr(grp, "name", "")
        if X.attr(grp, "hidden") == "true" or grp.get("user:auto-column"):
            continue
        if "Set]" not in name:
            continue
        clean = X.strip_brackets(name)
        if clean in seen:
            continue
        seen.add(clean)
        members = [X.strip_brackets(X.attr(m, "member"))
                   for m in grp.iter("groupfilter")
                   if X.attr(m, "function") == "member"]
        sets.append({
            "name": clean,
            "sourceField": clean.split("(")[0].strip() or None,
            "members": [m for m in members if m] or None,
        })
    return sets


def _detect_blending(root) -> Dict:
    """Count real datasources to flag potential data blending."""
    real = [ds for ds in X.iter_datasources(root)
            if X.attr(ds, "name") != "Parameters"]
    active = V.worksheet_datasources(root)
    if len(active) > 1:
        return {"blended": True, "datasources": sorted(active)}
    return {"blended": False, "datasources": [ds for ds in active]} if real else None


def _detect_rls(root, calc_fields=None, column_table_map=None) -> Dict:
    """Detect Tableau row-level security and surface it for a Power BI RLS role.

    Two patterns are recognised (additively, cheapest first):

    * **Dynamic user functions** -- a calc whose formula calls a Tableau identity
      function (USERNAME/FULLNAME/ISMEMBEROF/...). This is the canonical RLS calc.
    * **Mapping-table user filter** -- a security/mapping table (e.g. User_Access
      with a Username column) joined to the data, where a calc field references a
      user-identity column (Username/Email/Login). The formula may compare that
      column to a literal instead of calling USERNAME().

    ``calc_fields`` and ``column_table_map`` are passed in from build_ir to avoid
    re-extracting calculated fields here (the previous version re-parsed them).

    NOTE: a Tableau ``derivation='User'`` column-instance is NOT a reliable RLS
    signal -- it merely marks an ad-hoc user-created pill and appears in most
    ordinary workbooks, so it is deliberately not used here.
    """
    fields = calc_fields if calc_fields is not None else DS.extract_calculated_fields(root)

    # Pattern 1: explicit Tableau RLS identity functions.
    func_signals = ("USERNAME(", "FULLNAME(", "ISMEMBEROF(", "ISUSERNAME(",
                    "ISFULLNAME(", "USERDOMAIN(")
    for field in fields:
        upper = (field.get("formula") or "").upper()
        if any(sig in upper for sig in func_signals):
            rtype = "Group-based" if "ISMEMBEROF(" in upper else "Dynamic"
            return {
                "detected": True, "type": rtype,
                "securedTable": None, "mappingTable": None, "userColumn": None,
                "signalField": field["caption"],
            }

    # Pattern 2: a calc references a user-identity column from a mapping table.
    # Requiring an actual calc dependency (not just the column's presence) keeps
    # ordinary workbooks that merely have an 'email'/'account' column from being
    # misflagged.
    user_re = re.compile(r"(user\s?name|user[_ ]?id|e-?mail|login\s?name|\bupn\b)",
                         re.IGNORECASE)
    ctm = column_table_map if column_table_map is not None else M.extract_column_table_map(root)
    user_cols = [name for name in ctm if user_re.search(name)]
    if user_cols:
        for field in fields:
            deps = field.get("dependsOn") or []
            hit = next((u for u in user_cols if u in deps), None)
            if hit:
                mapping_table = ctm.get(hit)
                secured_table = next(
                    (t for t in dict.fromkeys(ctm.values()) if t != mapping_table), None)
                return {
                    "detected": True,
                    "type": "Mapping-table",
                    "securedTable": secured_table,
                    "mappingTable": mapping_table,
                    "userColumn": hit,
                    "signalField": field["caption"],
                }

    return {"detected": False, "type": None, "securedTable": None,
            "mappingTable": None, "userColumn": None, "signalField": None}


def resolve_output_dir(ir: Dict, output_root: str) -> str:
    """Output/{PascalName}/ — created if missing."""
    target = os.path.join(output_root, ir["workbook"]["pascalName"])
    os.makedirs(target, exist_ok=True)
    return target


def write_ir(ir: Dict, output_dir: str) -> str:
    """Write analysis.json into the workbook output folder."""
    path = os.path.join(output_dir, "analysis.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ir, fh, indent=2, ensure_ascii=False)
    return path


def _summary(ir: Dict) -> str:
    return (
        f"  workbook       : {ir['workbook']['name']} -> {ir['workbook']['pascalName']}\n"
        f"  dataSources    : {len(ir['dataSources'])}\n"
        f"  columns        : {len(ir['columns'])}\n"
        f"  calcFields     : {len(ir['calculatedFields'])} "
        f"({sum(1 for c in ir['calculatedFields'] if c.get('isTableCalc'))} table-calc)\n"
        f"  parameters     : {len(ir['parameters'])}\n"
        f"  worksheets     : {len(ir['worksheets'])} "
        f"({sum(1 for w in ir['worksheets'] if (w.get('markClass') or 'Automatic') == 'Automatic')} automatic-mark)\n"
        f"  dashboards     : {len(ir['dashboards'])}\n"
        f"  physTables     : {len(ir['physicalTables'])}\n"
        f"  relationships  : {len(ir['relationships'])}\n"
        f"  hierarchies    : {len(ir['hierarchies'])}\n"
        f"  groups/bins    : {len(ir['groups'])}/{len(ir['bins'])}\n"
        f"  RLS detected   : {ir['rls']['detected']}"
    )


def _coverage(ir: Dict) -> List[str]:
    """Audit that every visual and dashboard zone looks fully captured.

    Returns human-readable warnings (never fatal) so a parse that silently drops
    a worksheet's fields or references a missing sheet is visible immediately,
    instead of surfacing later as a blank Power BI visual.
    """
    warnings: List[str] = []
    ws_names = {w["name"] for w in ir["worksheets"]}
    for w in ir["worksheets"]:
        has_fields = bool(w["dimensions"] or w["values"] or w["measures"])
        has_shelf = bool(w["rows"] or w["cols"])
        if has_shelf and not has_fields:
            warnings.append(f"worksheet '{w['name']}' has shelves but no resolved fields")
    placed = set()
    for db in ir["dashboards"]:
        for z in db["zones"]:
            wsname = z.get("worksheet")
            if z.get("type") == "viz":
                placed.add(wsname)
                if wsname not in ws_names:
                    warnings.append(
                        f"dashboard '{db['name']}' references unknown worksheet '{wsname}'")
    return warnings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Parse a .twb into analysis.json (IR).")
    parser.add_argument("twb", help="path to the .twb file")
    parser.add_argument("--output-root", default="Output", help="root output folder")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.twb):
        print(f"ERROR: file not found: {args.twb}", file=sys.stderr)
        return 2

    base = os.path.basename(args.twb)
    if base.startswith("~") or os.path.splitext(base)[1].lower() in (".twbr", ".twbm"):
        print(f"SKIP: backup/temporary workbook (not a real .twb): {args.twb}",
              file=sys.stderr)
        return 0

    ir = build_ir(args.twb)
    out_dir = resolve_output_dir(ir, args.output_root)
    path = write_ir(ir, out_dir)
    print(f"Wrote IR: {path}\n{_summary(ir)}")
    for w in _coverage(ir):
        print(f"  WARN: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
