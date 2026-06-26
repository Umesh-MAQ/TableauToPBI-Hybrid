"""schema.py — DETERMINISTIC schema builder for dummy-data generation.

Stage 2 of the dummy-data feature. Turns the raw ``extracted.json`` into a clean,
generator-ready ``schema.json``:

  * maps raw Tableau datatypes -> a small canonical type enum
    (string | integer | decimal | date | datetime | boolean)
  * infers a *semantic category* from each column name (id, person_name, country,
    year, rating, duration, email, currency, ...) so the generator knows what
    kind of value is plausible — without ever looking at real data
  * carries through any discrete value domain found during extraction
  * picks a sensible default row count per table

This stage is 100% rule-based and reproducible. The actual *values* are invented
later by the agentic stage (dummy_data.py), which is the only part that benefits
from AI judgement.

Output: ``schema.json`` (consumed by dummy_data.py).

Usage:
    python schema.py Output/datagen/NetfixWorkbook/extracted.json
    python schema.py Output/datagen/NetfixWorkbook --rows 200
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Dict, List, Optional

DEFAULT_ROWS = 100

# Raw Tableau / metadata datatype -> canonical generator type.
_TYPE_MAP = {
    "integer": "integer", "long": "integer", "int": "integer",
    "real": "decimal", "double": "decimal", "float": "decimal", "decimal": "decimal",
    "date": "date",
    "datetime": "datetime", "timestamp": "datetime",
    "boolean": "boolean", "bool": "boolean",
    "string": "string", "str": "string", "wstr": "string", "text": "string",
}

# Ordered (first match wins) name-pattern -> semantic category. The category
# drives which generator the dummy stage uses and what hint the AI agent gets.
_SEMANTIC_RULES: List[tuple] = [
    (r"(^|_)id$|_id$|^id$|^.*_?key$", "identifier"),
    (r"e[\-_ ]?mail", "email"),
    (r"phone|mobile|contact", "phone"),
    (r"first.?name", "first_name"),
    (r"last.?name|surname", "last_name"),
    (r"full.?name|customer.?name|director|cast|actor|author|owner", "person_name"),
    (r"\bname\b|title", "title"),
    (r"country|nation", "country"),
    (r"state|province|region", "region"),
    (r"city|town", "city"),
    (r"address|street", "address"),
    (r"postal|zip", "postcode"),
    (r"year", "year"),
    (r"month", "month"),
    (r"date|added|created|updated|timestamp", "date"),
    (r"duration|length|runtime", "duration"),
    (r"rating|rated|certificat", "rating"),
    (r"type|category|genre|listed.?in|class", "category"),
    (r"description|summary|comment|note|desc", "description"),
    (r"price|cost|amount|revenue|sales|salary|profit|total", "currency"),
    (r"qty|quantity|count|number|num", "count"),
    (r"percent|ratio|rate|pct", "percent"),
    (r"status|flag|active|enabled", "status"),
]


def canonical_type(raw: Optional[str]) -> str:
    return _TYPE_MAP.get((raw or "string").strip().lower(), "string")


def infer_semantic(name: str, ctype: str) -> str:
    """Infer a semantic category from a column name, falling back to its type."""
    low = (name or "").strip().lower()
    for pattern, category in _SEMANTIC_RULES:
        if re.search(pattern, low):
            return category
    if ctype in ("integer", "decimal"):
        return "count"
    if ctype in ("date", "datetime"):
        return "date"
    if ctype == "boolean":
        return "status"
    return "text"


def build_schema(extracted: Dict, rows: int = DEFAULT_ROWS) -> Dict:
    """Normalize extracted.json into a generator-ready schema."""
    tables_out: List[Dict] = []
    for ds in extracted.get("datasources", []):
        for tbl in ds.get("tables", []):
            columns: List[Dict] = []
            for col in tbl.get("columns", []):
                ctype = canonical_type(col.get("rawType"))
                columns.append({
                    "name": col["name"],
                    "type": ctype,
                    "role": col.get("role", "dimension"),
                    "semantic": infer_semantic(col["name"], ctype),
                    "ordinal": col.get("ordinal", len(columns)),
                    "nullable": col.get("role") != "measure",
                    "domain": col.get("domain") or [],
                })
            columns.sort(key=lambda c: c["ordinal"])
            tables_out.append({
                "datasource": ds.get("name"),
                "table": tbl.get("table"),
                "file": tbl.get("file") or f"{tbl.get('table', 'data')}.csv",
                "delimiter": tbl.get("delimiter", ","),
                "headerRow": tbl.get("headerRow", True),
                "rowCount": rows,
                "columns": columns,
            })

    return {
        "workbook": extracted.get("workbook", {}),
        "tables": tables_out,
    }


def run(target: str, rows: int = DEFAULT_ROWS) -> str:
    """Build schema from extracted.json (file or model dir). Returns schema.json path."""
    if os.path.isdir(target):
        extracted_path = os.path.join(target, "extracted.json")
    else:
        extracted_path = target
    with open(extracted_path, encoding="utf-8") as fh:
        extracted = json.load(fh)
    schema = build_schema(extracted, rows)
    out_path = os.path.join(os.path.dirname(extracted_path), "schema.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, ensure_ascii=False)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build a generator-ready schema from extracted.json.")
    ap.add_argument("target", help="Path to extracted.json or its model directory.")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="Default rows per table.")
    args = ap.parse_args(argv)
    path = run(args.target, args.rows)
    schema = json.load(open(path, encoding="utf-8"))
    cols = sum(len(t["columns"]) for t in schema["tables"])
    print(f"[schema] wrote {path}  ({len(schema['tables'])} table(s), {cols} column(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
