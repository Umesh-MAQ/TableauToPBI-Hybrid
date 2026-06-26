"""dummy_data.py — AGENTIC dummy-data generator (stage 3).

This is the only stage that benefits from AI judgement. It mirrors the repo's
hybrid pattern (a deterministic engine that writes a self-contained *todo* for the
agent, the agent writes ONE *fragment*, the engine consumes it):

  plan   (deterministic)  schema.json ─► dummy-todo.json
                          A compact task envelope the AI agent reads. For every
                          column it states the canonical type, the inferred
                          semantic, any known value domain, and asks the agent to
                          supply a realistic value POOL (and/or a numeric range).

  <the AI agent writes>   dummy-fragment.json
                          { "<table>": { "<column>": {
                              "values": [...],            # discrete pool to sample
                              "min": .., "max": ..,       # numeric / date range
                              "pattern": "tmpl {i}",      # optional template
                          } } }

  build  (deterministic)  schema.json + dummy-fragment.json ─► <file>.csv
                          Seeded, type-aware generation. Uses the AI pool/range
                          when present, otherwise a built-in fallback generator so
                          a valid CSV is ALWAYS produced — even with no AI input.

By splitting it this way the heavy lifting (parsing, typing, CSV writing,
reproducibility via a fixed seed) stays deterministic and free, and the agent only
spends tokens inventing realistic values — exactly where AI adds value.

Usage:
    python dummy_data.py plan  Output/datagen/NetfixWorkbook
    python dummy_data.py build Output/datagen/NetfixWorkbook --write-beside-source
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import random
import re
from typing import Dict, List, Optional

# ----------------------------------------------------------------------------- #
# plan — deterministic AI task envelope
# ----------------------------------------------------------------------------- #
_NEEDS_POOL = {
    "category", "rating", "country", "region", "city", "status",
    "title", "person_name", "description",
}


def build_todo(schema: Dict) -> Dict:
    """Produce the agent task envelope from the schema."""
    tasks: List[Dict] = []
    for tbl in schema.get("tables", []):
        cols = []
        for c in tbl["columns"]:
            ask = "value_pool" if (c["semantic"] in _NEEDS_POOL and not c["domain"]) else "auto"
            cols.append({
                "column": c["name"],
                "type": c["type"],
                "semantic": c["semantic"],
                "knownDomain": c["domain"],
                "agentShould": ask,
            })
        tasks.append({"table": tbl["table"], "file": tbl["file"], "columns": cols})

    return {
        "instructions": (
            "For every column with agentShould='value_pool', invent 6-15 realistic, "
            "domain-appropriate sample values. For numeric/date columns you may "
            "instead give a {min,max} range. Write results to dummy-fragment.json "
            "as { table: { column: {values:[...]} | {min,max} } }. Leave 'auto' "
            "columns out — the engine fills them deterministically."
        ),
        "fragmentSchema": {
            "<table>": {"<column>": {"values": ["..."], "min": "?", "max": "?", "pattern": "?"}}
        },
        "workbook": schema.get("workbook", {}),
        "tables": tasks,
    }


# ----------------------------------------------------------------------------- #
# build — deterministic, seeded generation (consumes the AI fragment)
# ----------------------------------------------------------------------------- #
_FALLBACK_POOLS = {
    "country": ["United States", "India", "United Kingdom", "Canada", "Japan",
                "France", "Germany", "Brazil", "Australia", "South Korea"],
    "region": ["North", "South", "East", "West", "Central"],
    "city": ["Springfield", "Riverton", "Fairview", "Madison", "Georgetown", "Clinton"],
    "rating": ["A", "B", "C", "D", "E"],
    "category": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"],
    "status": ["Active", "Inactive", "Pending"],
    "first_name": ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie"],
    "last_name": ["Smith", "Johnson", "Lee", "Patel", "Garcia", "Nguyen", "Brown"],
}

_LOREM = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
          "tempor incididunt ut labore et dolore magna aliqua").split()


def _gen_value(col: Dict, spec: Optional[Dict], rng: random.Random, i: int):
    """Generate one cell value for a column, honouring an AI spec when present."""
    ctype, semantic = col["type"], col["semantic"]
    domain = col.get("domain") or []

    # 1) AI-provided discrete pool wins.
    if spec and spec.get("values"):
        return rng.choice(spec["values"])
    # 2) A domain captured during extraction.
    if domain:
        return rng.choice(domain)
    # 3) AI-provided pattern template.
    if spec and spec.get("pattern"):
        return spec["pattern"].replace("{i}", str(i + 1))

    lo = spec.get("min") if spec else None
    hi = spec.get("max") if spec else None

    if semantic == "identifier":
        return i + 1
    if ctype == "integer" or semantic in ("count", "year"):
        a = int(lo) if _is_num(lo) else (1990 if semantic == "year" else 1)
        b = int(hi) if _is_num(hi) else (2024 if semantic == "year" else 1000)
        return rng.randint(min(a, b), max(a, b))
    if ctype == "decimal" or semantic in ("currency", "percent"):
        a = float(lo) if _is_num(lo) else 0.0
        b = float(hi) if _is_num(hi) else (1.0 if semantic == "percent" else 10000.0)
        return round(rng.uniform(min(a, b), max(a, b)), 2)
    if ctype in ("date", "datetime") or semantic == "date":
        start = _parse_date(lo) or _dt.date(2018, 1, 1)
        end = _parse_date(hi) or _dt.date(2024, 12, 31)
        span = max((end - start).days, 1)
        d = start + _dt.timedelta(days=rng.randint(0, span))
        return d.isoformat() if ctype != "datetime" else f"{d.isoformat()} 00:00:00"
    if ctype == "boolean" or semantic == "status":
        return rng.choice(_FALLBACK_POOLS["status"]) if semantic == "status" else rng.choice(["true", "false"])
    if semantic in _FALLBACK_POOLS:
        return rng.choice(_FALLBACK_POOLS[semantic])
    if semantic == "person_name":
        return f"{rng.choice(_FALLBACK_POOLS['first_name'])} {rng.choice(_FALLBACK_POOLS['last_name'])}"
    if semantic == "email":
        return f"user{i + 1}@example.com"
    if semantic == "phone":
        return f"+1-555-{rng.randint(1000, 9999):04d}"
    if semantic == "duration":
        return f"{rng.randint(20, 180)} min"
    if semantic == "title":
        return f"{rng.choice(['The', 'A', 'My']).strip()} {rng.choice(_LOREM).title()} {rng.choice(_LOREM).title()}"
    if semantic == "description":
        return " ".join(rng.sample(_LOREM, k=min(8, len(_LOREM)))).capitalize()
    return f"{col['name']}_{i + 1}"


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _parse_date(v) -> Optional[_dt.date]:
    if not v:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    if not m:
        return None
    try:
        return _dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def generate_table(tbl: Dict, fragment: Dict, seed: int = 42) -> List[List[str]]:
    """Return rows (header + data) for one table."""
    rng = random.Random(f"{seed}:{tbl['table']}")
    # The AI may key the fragment by the table name or the file name — accept both.
    specs = fragment.get(tbl["table"]) or fragment.get(tbl.get("file")) or {}
    header = [c["name"] for c in tbl["columns"]]
    rows: List[List[str]] = [header] if tbl.get("headerRow", True) else []
    for i in range(int(tbl.get("rowCount", 100))):
        nullable_hit = rng.random()
        row = []
        for c in tbl["columns"]:
            # Sprinkle a few blanks into nullable dimension columns for realism.
            if c.get("nullable") and c["semantic"] not in ("identifier",) and nullable_hit < 0.03:
                row.append("")
            else:
                row.append(str(_gen_value(c, specs.get(c["name"]), rng, i)))
        rows.append(row)
    return rows


def _write_csv(path: str, rows: List[List[str]], delimiter: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter=delimiter or ",").writerows(rows)


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #
def _load(model_dir: str, name: str, default=None):
    p = os.path.join(model_dir, name)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def cmd_plan(model_dir: str) -> str:
    schema = _load(model_dir, "schema.json")
    if schema is None:
        raise FileNotFoundError(f"schema.json not found in {model_dir} (run schema.py first)")
    todo = build_todo(schema)
    out = os.path.join(model_dir, "dummy-todo.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(todo, fh, indent=2, ensure_ascii=False)
    return out


def cmd_build(model_dir: str, write_beside_source: bool, seed: int) -> List[str]:
    schema = _load(model_dir, "schema.json")
    if schema is None:
        raise FileNotFoundError(f"schema.json not found in {model_dir} (run schema.py first)")
    fragment = _load(model_dir, "dummy-fragment.json", default={}) or {}

    written: List[str] = []
    source_dir = os.path.dirname(schema.get("workbook", {}).get("sourcePath", "") or "")
    for tbl in schema["tables"]:
        rows = generate_table(tbl, fragment, seed)
        # Always write a copy into the model dir; optionally beside the workbook
        # so the report's datasource resolves against real dummy files.
        targets = [os.path.join(model_dir, "data", tbl["file"])]
        if write_beside_source and source_dir:
            targets.append(os.path.join(source_dir, tbl["file"]))
        for t in targets:
            _write_csv(t, rows, tbl.get("delimiter", ","))
            written.append(t)
    return written


def _data_date_range(model_dir: str, schema: Dict) -> Optional[tuple]:
    """Scan the generated CSVs' date columns and return (min_iso, max_iso).

    Used to reset a workbook's date-range parameters to the dummy data window so
    date-filtered dashboards still render. Returns None when no date data exists.
    """
    lo: Optional[str] = None
    hi: Optional[str] = None
    for tbl in schema.get("tables", []):
        date_cols = [c["name"] for c in tbl["columns"]
                     if c["type"] in ("date", "datetime") or c["semantic"] == "date"]
        if not date_cols:
            continue
        path = os.path.join(model_dir, "data", tbl["file"])
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for c in date_cols:
                    m = re.search(r"\d{4}-\d{2}-\d{2}", row.get(c, "") or "")
                    if not m:
                        continue
                    d = m.group(0)
                    if lo is None or d < lo:
                        lo = d
                    if hi is None or d > hi:
                        hi = d
    return (lo, hi) if lo and hi else None


def cmd_rebind(model_dir: str, schema: Dict, beside: bool) -> Optional[Dict]:
    """Deterministically repoint the source .twb at the freshly generated CSVs.

    When ``beside`` is true the CSVs sit next to the workbook and we rewrite it
    in place (a one-time .twb.bak backup is kept); otherwise we bind against the
    model dir's data/ folder and write a sibling '(dummy).twb' copy. Date-range
    parameters are also reset to the generated data window so date-filtered
    dashboards don't open blank.
    """
    import rebind as R  # local import: rebind is an optional, standalone stage
    src = schema.get("workbook", {}).get("sourcePath")
    if not src or not os.path.isfile(src):
        return None
    data_dir = os.path.dirname(src) if beside else os.path.join(model_dir, "data")
    drange = _data_date_range(model_dir, schema)
    return R.run(src, data_dir, beside=beside, in_place=beside, date_range=drange)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Agentic dummy-data generator (plan/build).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="Write dummy-todo.json (AI task envelope).")
    p_plan.add_argument("model_dir", help="Model dir holding schema.json.")

    p_build = sub.add_parser("build", help="Generate CSV(s) from schema + dummy-fragment.json.")
    p_build.add_argument("model_dir", help="Model dir holding schema.json.")
    p_build.add_argument("--write-beside-source", action="store_true",
                         help="Also write the CSV next to the source .twb so the datasource resolves.")
    p_build.add_argument("--rebind", action="store_true",
                         help="After generating, repoint the source .twb at the dummy CSVs "
                              "(deterministic). Implies --write-beside-source.")
    p_build.add_argument("--seed", type=int, default=42, help="Deterministic seed.")

    args = ap.parse_args(argv)
    if args.cmd == "plan":
        out = cmd_plan(args.model_dir)
        print(f"[dummy] wrote {out}")
    else:
        beside = args.write_beside_source or args.rebind
        files = cmd_build(args.model_dir, beside, args.seed)
        for f in files:
            print(f"[dummy] wrote {f}")
        if args.rebind:
            schema = _load(args.model_dir, "schema.json")
            res = cmd_rebind(args.model_dir, schema, beside=True)
            if res:
                print(f"[dummy] rebound {res['reboundCount']} connection(s) -> {res['outPath']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
