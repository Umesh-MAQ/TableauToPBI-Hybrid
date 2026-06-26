"""extract.py — DETERMINISTIC data-source extraction for dummy-data generation.

Stage 1 of the dummy-data feature. Reads a Tableau workbook (.twb) and pulls the
*physical* schema of every real datasource straight from the XML — the column
names, raw Tableau datatypes, ordinals, owning physical table, role
(dimension/measure), aggregation, and any value aliases (a discrete domain the
generator can sample from). No AI, no guessing — this is pure structural
extraction so the result is byte-for-byte reproducible.

The richest source of a CSV/extract's true column list is the physical
``<relation><columns>`` block; we read that first, then enrich each column with
the friendlier ``<metadata-record>`` lineage and the datasource-body captions.

Output: ``extracted.json`` (consumed by schema.py).

Usage:
    python extract.py "Data/Netflix/Netfix Workbook.twb"
    python extract.py "Data/Netflix" --out Output/datagen
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

# Reuse the battle-tested twb parser helpers instead of re-implementing XML walks.
_TWB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "twb")
sys.path.insert(0, os.path.abspath(_TWB))
import twb_xml as X  # noqa: E402
import twb_meta as M  # noqa: E402


def _find_twb(target: str) -> str:
    """Resolve a .twb path from either a direct file or a folder."""
    if os.path.isfile(target) and target.lower().endswith((".twb", ".twbx")):
        return target
    if os.path.isdir(target):
        for fn in sorted(os.listdir(target)):
            if fn.lower().endswith(".twb"):
                return os.path.join(target, fn)
    raise FileNotFoundError(f"No .twb workbook found at: {target}")


def _relation_columns(ds) -> Dict[str, Dict]:
    """Map physical table name -> {file, delimiter, headerRow, columns}.

    Each ``<relation>`` carries its OWN source CSV in the ``name`` attribute
    (e.g. ``name='customer.csv' table='[customer#csv]'``). A joined datasource
    reaches several CSVs through a single connection, so we must take the file
    from the relation itself — NOT from the datasource's first connection — or
    every table in the join would be mislabelled with one shared filename.
    """
    tables: Dict[str, Dict] = {}
    for rel in ds.iter("relation"):
        cols_el = rel.find("columns")
        if cols_el is None:
            continue
        table = X.strip_brackets(rel.get("table") or rel.get("name") or "table")
        fname = os.path.basename(rel.get("name") or "") or f"{table}.csv"
        delim = cols_el.get("separator") or ","
        header = (cols_el.get("header") or "yes").lower() in ("yes", "true")
        entry = tables.setdefault(table, {
            "file": fname,
            "delimiter": "\t" if delim == "&#9;" else delim,
            "headerRow": header,
            "columns": [],
        })
        seen = {c["name"] for c in entry["columns"]}
        for col in cols_el.findall("column"):
            name = X.strip_brackets(X.attr(col, "name"))
            if name in seen:  # same CSV reached via two named-connections — keep one
                continue
            seen.add(name)
            entry["columns"].append({
                "name": name,
                "rawType": X.attr(col, "datatype") or "string",
                "ordinal": X.int_attr(col, "ordinal", len(entry["columns"])),
            })
    return tables


def _value_aliases(ds, col_name: str) -> List[str]:
    """Collect any discrete display aliases declared for a column (a value domain)."""
    domain: List[str] = []
    for col in ds.iter("column"):
        if X.strip_brackets(X.attr(col, "name")) != col_name:
            continue
        for al in col.findall("aliases/alias"):
            value = X.attr(al, "value")
            if value and value not in domain:
                domain.append(value)
    return domain


def _connection_class(ds) -> str:
    """Resolve the (possibly federated) connection class for a datasource."""
    outer = ds.find("connection")
    if outer is None:
        return ""
    inner = outer.find("named-connections/named-connection/connection")
    return X.attr(inner if inner is not None else outer, "class") or ""


def build_extraction(twb_path: str) -> Dict:
    """Parse the workbook and return the deterministic extraction dictionary.

    Each physical CSV is emitted exactly ONCE with its richest column set. A CSV
    can surface in several datasources (e.g. a join member that also has its own
    datasource); keying by the relation's real filename and keeping the widest
    definition avoids generating the same file twice with mismatched columns
    (which previously left worksheets bound to the wrong header → blank visuals).
    """
    root = X.load_twb(twb_path)
    wb_name = os.path.splitext(os.path.basename(twb_path))[0]
    col_meta = M.extract_column_metadata(root)

    # Pass 1 — richest column set per physical CSV across the whole workbook.
    best: Dict[str, Dict] = {}  # file -> {dsName, connClass, internalName, table, info, ds}
    order: List[str] = []       # datasource caption order, for stable output
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        caption = X.datasource_caption(ds)
        if caption not in order:
            order.append(caption)
        conn_class = _connection_class(ds)
        for table, info in _relation_columns(ds).items():
            f = info["file"]
            prev = best.get(f)
            if prev is None or len(info["columns"]) > len(prev["info"]["columns"]):
                best[f] = {
                    "dsName": caption, "connClass": conn_class,
                    "internalName": X.attr(ds, "name"), "table": table,
                    "info": info, "ds": ds,
                }

    # Pass 2 — group the deduped files by their owning datasource and enrich.
    grouped: Dict[str, List[Dict]] = {}
    ds_meta: Dict[str, Dict] = {}
    for f, rec in best.items():
        ds = rec["ds"]
        info = rec["info"]
        enriched: List[Dict] = []
        for c in info["columns"]:
            meta = col_meta.get(c["name"], {})
            raw_type = (meta.get("metaType") or c["rawType"] or "string").lower()
            numeric = raw_type in ("integer", "long", "real", "double", "float", "decimal")
            # A column is only a measure if it is numeric AND aggregated — a
            # string with a Count aggregation is still a dimension.
            role = "measure" if (numeric and meta.get("metaRole") == "measure") else "dimension"
            enriched.append({
                "name": c["name"],
                "rawType": meta.get("metaType") or c["rawType"],
                "ordinal": c["ordinal"],
                "role": role,
                "aggregation": meta.get("aggregation"),
                "parentTable": meta.get("parentTable") or rec["table"],
                "domain": _value_aliases(ds, c["name"]),
            })
        enriched.sort(key=lambda x: x["ordinal"])
        grouped.setdefault(rec["dsName"], []).append({
            "table": rec["table"],
            "file": f,
            "delimiter": info["delimiter"],
            "headerRow": info["headerRow"],
            "columns": enriched,
        })
        ds_meta.setdefault(rec["dsName"], {
            "internalName": rec["internalName"],
            "connectionClass": rec["connClass"],
        })

    datasources: List[Dict] = []
    for caption in order:
        if caption not in grouped:
            continue
        meta = ds_meta[caption]
        datasources.append({
            "name": caption,
            "internalName": meta["internalName"],
            "connectionClass": meta["connectionClass"],
            "sourceType": X.resolve_source_type(meta["connectionClass"]),
            "tables": sorted(grouped[caption], key=lambda t: t["file"]),
        })

    return {
        "workbook": {
            "name": wb_name,
            "pascalName": X.to_pascal_case(wb_name),
            "sourcePath": twb_path.replace("\\", "/"),
        },
        "datasources": datasources,
    }


def run(target: str, out_root: str = "Output/datagen") -> str:
    """Extract from target (.twb or folder) and write extracted.json. Returns its path."""
    twb_path = _find_twb(target)
    data = build_extraction(twb_path)
    model_dir = os.path.join(out_root, data["workbook"]["pascalName"])
    os.makedirs(model_dir, exist_ok=True)
    out_path = os.path.join(model_dir, "extracted.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Extract Tableau datasource schema for dummy-data generation.")
    ap.add_argument("target", help="Path to a .twb file or a folder containing one.")
    ap.add_argument("--out", default="Output/datagen", help="Output root (default: Output/datagen).")
    args = ap.parse_args(argv)
    path = run(args.target, args.out)
    n = sum(len(d["tables"]) for d in json.load(open(path, encoding="utf-8"))["datasources"])
    print(f"[extract] wrote {path}  ({n} table(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
