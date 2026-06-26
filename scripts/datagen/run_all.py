"""run_all.py — one-shot orchestrator for the whole datagen feature.

Runs every stage end-to-end so you never have to chain the commands by hand:

    extract  ->  schema  ->  plan  ->  build  ->  rebind

  * extract / schema / plan are deterministic (the AI is never required).
  * build generates the CSVs. If an AI ``dummy-fragment.json`` is present it uses
    those realistic value pools; otherwise it falls back to the built-in seeded
    generator, so a valid dataset is ALWAYS produced — no manual step needed.
  * rebind repoints the source .twb at the freshly generated CSVs (in place, with
    a one-time .twb.bak backup) so the workbook opens straight onto the dummy data.

So a single call does everything:

    python run_all.py "Data/Netflix"                 # full pipeline + rebind
    python run_all.py "Data/Loan" --rows 200
    python run_all.py "Data/Sales and Customer" --plan-only   # stop before build
    python run_all.py "Data/Netflix" --no-rebind     # build CSVs, don't touch .twb
    python run_all.py "Data/Netflix" --rebind-copy   # write a sibling (dummy).twb

It can also be driven from code instead of the terminal::

    import run_all
    run_all.run("Data/Netflix")                      # returns a summary dict
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract as E  # noqa: E402
import schema as S  # noqa: E402
import dummy_data as D  # noqa: E402


def run(target: str, *, rows: int = S.DEFAULT_ROWS, out_root: str = "Output/datagen",
        seed: int = 42, plan_only: bool = False, do_rebind: bool = True,
        rebind_in_place: bool = True, verbose: bool = True) -> Dict:
    """Run the full datagen pipeline programmatically and return a summary.

    Args:
        target: Path to a .twb file or a folder containing one.
        rows: Rows to generate per table.
        out_root: Datagen output root (default ``Output/datagen``).
        seed: Deterministic generation seed.
        plan_only: Stop after writing dummy-todo.json (skip build + rebind).
        do_rebind: After build, repoint the source .twb at the generated CSVs.
        rebind_in_place: True overwrites the original .twb (keeps a .twb.bak);
            False writes a sibling ``(dummy).twb`` copy and binds it to the
            model dir's ``data/`` folder.
        verbose: Print progress lines.

    Returns:
        A summary dict with the produced artifact paths.
    """
    def _log(msg: str) -> None:
        if verbose:
            print(msg)

    extracted = E.run(target, out_root)
    model_dir = os.path.dirname(extracted)
    _log(f"[run_all] extracted -> {extracted}")

    schema_path = S.run(model_dir, rows)
    _log(f"[run_all] schema    -> {schema_path}")

    todo = D.cmd_plan(model_dir)
    _log(f"[run_all] todo      -> {todo}")

    summary: Dict = {
        "modelDir": model_dir,
        "extracted": extracted,
        "schema": schema_path,
        "todo": todo,
        "csvFiles": [],
        "rebind": None,
    }

    if plan_only:
        _log(f"\n[run_all] plan-only: optionally write "
             f"{os.path.join(model_dir, 'dummy-fragment.json')} (AI), "
             f"then re-run without --plan-only.")
        return summary

    # build — generate the CSVs (beside the .twb so rebind resolves locally).
    files = D.cmd_build(model_dir, write_beside_source=True, seed=seed)
    summary["csvFiles"] = files
    for f in files:
        _log(f"[run_all] data      -> {f}")

    # rebind — repoint the workbook at the generated CSVs.
    if do_rebind:
        schema = D._load(model_dir, "schema.json")
        res = D.cmd_rebind(model_dir, schema, beside=rebind_in_place)
        summary["rebind"] = res
        if res:
            _log(f"[run_all] rebind    -> {res['reboundCount']} connection(s) "
                 f"=> {res['outPath']}")
        else:
            _log("[run_all] rebind    -> skipped (source .twb not found)")

    _log("\n[run_all] done. Open the workbook — it now resolves onto the dummy data.")
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the full datagen pipeline (extract + schema + plan + build + rebind).")
    ap.add_argument("target", help="Path to a .twb file or a folder containing one.")
    ap.add_argument("--rows", type=int, default=S.DEFAULT_ROWS, help="Rows per table.")
    ap.add_argument("--out", default="Output/datagen", help="Output root.")
    ap.add_argument("--seed", type=int, default=42, help="Deterministic seed.")
    ap.add_argument("--plan-only", action="store_true",
                    help="Stop after plan (skip build + rebind).")
    ap.add_argument("--no-rebind", action="store_true",
                    help="Generate CSVs but do not repoint the .twb.")
    ap.add_argument("--rebind-copy", action="store_true",
                    help="Write a sibling '(dummy).twb' instead of rewriting in place.")
    args = ap.parse_args(argv)

    run(args.target, rows=args.rows, out_root=args.out, seed=args.seed,
        plan_only=args.plan_only, do_rebind=not args.no_rebind,
        rebind_in_place=not args.rebind_copy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
