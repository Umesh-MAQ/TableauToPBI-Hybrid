"""star_det.py — deterministic schema builder for the unambiguous single-flat case.

The IR is a flat column list (columns are not mapped to individual source tables),
so splitting a workbook into a fact + dimension star reliably is NOT possible
deterministically — that genuine design reasoning stays with the agent. What IS
deterministic is the single-flat case: one active datasource exposing exactly one
real source table (one CSV/Hyper extract), no parameters. There we emit one fact
table and no relationships, with zero ambiguity.

``detect(ir)`` classifies the strategy and proposes canonical table names (a hint
the agent may refine for the star case). ``build_star(ir)`` returns a complete
schema fragment ONLY for the single-flat case, else None (route schema -> agent).
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

# Tableau exposes source tables with a "#csv"/"#xlsx" marker; extract/custom-SQL
# entries carry bracket punctuation we must ignore as non-real tables.
_REAL_SUFFIX = re.compile(r"#(csv|xlsx?|txt)$", re.IGNORECASE)
# Words that typically name the fact/transaction grain (best-effort fact guess for
# the star case; only a hint — the agent finalizes the real star design).
_FACT_WORDS = ("order", "sales", "fact", "transaction", "claim", "encounter", "line")

_SOURCE_TYPE_MAP = {
    "CSV": "csv", "Excel": "excel", "Hyper": "csv",
    "SqlServer": "sql", "PostgreSQL": "sql", "MySQL": "sql",
    "Oracle": "sql", "Snowflake": "sql", "Databricks": "sql", "BigQuery": "sql",
}


def _active_datasources(ir: Dict) -> List[Dict]:
    return [d for d in ir.get("dataSources", []) if d.get("active")]


def clean_source_tables(ds: Dict) -> List[str]:
    """Real source-table stems from a datasource, ignoring extract/SQL junk.

    Keeps entries marked "#csv"/"#xlsx" (stem stripped) and plain identifiers;
    drops bracketed extract refs (``Extract].[…``) and ``Custom SQL Query``.
    Order-preserving and de-duplicated.
    """
    out: List[str] = []
    seen = set()
    for raw in ds.get("tables", []):
        name = (raw or "").strip()
        if not name or "[" in name or "]" in name:
            continue
        if name.lower().startswith("custom sql"):
            continue
        stem = _REAL_SUFFIX.sub("", name)
        if not stem or stem in seen:
            continue
        seen.add(stem)
        out.append(stem)
    return out


def detect(ir: Dict) -> Dict:
    """Classify table strategy and propose canonical names.

    Returns {strategy, fact, dimensions, datasource}. ``strategy`` is
    'single-flat' only for exactly one active datasource with <=1 real table;
    otherwise 'star-schema' (a hint set the agent will finalize).
    """
    active = _active_datasources(ir)
    pascal = ir.get("workbook", {}).get("pascalName", "Model")
    if len(active) != 1:
        return {"strategy": "star-schema", "fact": None, "dimensions": [],
                "datasource": None, "reason": f"{len(active)} active datasources"}
    ds = active[0]
    tables = clean_source_tables(ds)
    if len(tables) <= 1:
        fact = tables[0] if tables else pascal
        return {"strategy": "single-flat", "fact": fact, "dimensions": [],
                "datasource": ds.get("name"), "reason": "single source table"}
    # star candidate: best-effort fact guess (hint only)
    fact = next((t for t in tables
                 if any(w in t.lower() for w in _FACT_WORDS)), tables[0])
    dims = [t for t in tables if t != fact]
    return {"strategy": "star-schema", "fact": fact, "dimensions": dims,
            "datasource": ds.get("name"), "reason": f"{len(tables)} source tables"}


def _source_type(ds: Dict) -> str:
    return _SOURCE_TYPE_MAP.get(ds.get("sourceType", ""), "csv")


def _csv_file(ds: Dict, stem: str) -> Optional[str]:
    """Pick the data file for a single-flat table: the datasource's first real
    data file, else ``{stem}.csv`` as a sensible default."""
    for f in ds.get("files", []):
        if isinstance(f, str) and re.search(r"\.(csv|xlsx?|txt)$", f, re.IGNORECASE):
            return os.path.basename(f)
    return f"{stem}.csv"


def build_star(ir: Dict) -> Optional[Dict]:
    """Return a complete schema fragment for the single-flat case, else None.

    Conservative gates (any failure -> None -> agent designs the schema):
      * exactly one active datasource with <=1 real source table, and
      * no parameters (parameter datatables need agent-authored rows/wiring).
    """
    det = detect(ir)
    if det["strategy"] != "single-flat":
        return None
    if ir.get("parameters"):
        return None
    active = _active_datasources(ir)
    ds = active[0]
    fact = det["fact"]
    return {
        "tableStrategy": "single-flat",
        "tables": [{
            "name": fact,
            "role": "fact",
            "sourceType": _source_type(ds),
            "sourceFile": _csv_file(ds, fact),
            "sourceDatasource": ds.get("name"),
            "keyColumns": [],
        }],
        "relationships": [],
    }
