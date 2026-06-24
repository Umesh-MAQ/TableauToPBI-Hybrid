"""pipeline.py — deterministic orchestrator for the hybrid migration pipeline.

Two phases bracket the single LLM gap-fill step (decisions.json):

  prepare  <twb>                  Stage 1 + 6 (deterministic)
                                  -> analysis.json + dax-partial.json + gaps report
  [ AGENT writes decisions.json from analysis.json + dax-partial.json ]
  generate <analysis> --decisions Stage 10 + 13 + 11/14 (deterministic)
                                  -> SemanticModel + Report + validation

This keeps raw .twb XML and TMDL/PBIR boilerplate entirely off the LLM; the agent
only authors the small decisions.json (complex DAX, ambiguous charts, design).

Usage:
    python pipeline.py prepare  "Data/Midnight Census/Midnight Census Dashboard.twb"
    python pipeline.py generate "Output/MidnightCensusDashboard/analysis.json" \
        --decisions "Output/MidnightCensusDashboard/decisions.json"
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOAD_CONST = os.path.join(HERE, "load_constitution.py")
PARSE = os.path.join(HERE, "twb", "parse_twb.py")
MAPDAX = os.path.join(HERE, "dax", "map_dax.py")
CLASSIFY = os.path.join(HERE, "classify", "classify.py")
MERGE = os.path.join(HERE, "merge", "merge_decisions.py")
RECONCILE = os.path.join(HERE, "dax", "reconcile.py")
EMIT_TMDL = os.path.join(HERE, "emit", "emit_tmdl.py")
EMIT_PBIR = os.path.join(HERE, "emit", "emit_pbir.py")
VALIDATE_BINDINGS = os.path.join(HERE, "emit", "validate_bindings.py")
VALIDATE = os.path.join(
    HERE, "..", "plugins", "pbip", "skills", "pbip", "scripts", "validate_pbip.py")
SEM_VALIDATE = os.path.join(HERE, "validate_semantics.py")
BIN_DIR = os.path.join(HERE, "..", "plugins", "pbip", "hooks", "bin")


def _tmdl_validate_binary() -> str:
    """Resolve the tmdl-validate binary for the current OS/architecture.

    The hooks/bin folder ships native binaries for windows-x64, linux-x64,
    darwin-x64 and darwin-arm64; pick the right one so the pipeline is not
    Windows-only.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    if system == "windows":
        name = "tmdl-validate-windows-x64.exe"
    elif system == "darwin":
        name = "tmdl-validate-darwin-arm64" if is_arm else "tmdl-validate-darwin-x64"
    else:  # linux and other POSIX systems
        name = "tmdl-validate-linux-x64"
    return os.path.join(BIN_DIR, name)


TMDL_VALIDATE = _tmdl_validate_binary()


def run(cmd: list) -> int:
    """Run a child process, streaming output; return its exit code."""
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    return subprocess.call(cmd)


def analysis_path_for(output_root: str, twb: str) -> str:
    name = os.path.splitext(os.path.basename(twb))[0]
    import re
    pascal = "".join(w[:1].upper() + w[1:]
                     for w in re.split(r"[^0-9A-Za-z]+", name) if w)
    return os.path.join(output_root, pascal, "analysis.json")


def _output_dir_for(output_root: str, twb: str) -> str:
    """Return Output/{PascalName}/ for the given workbook path."""
    import re
    name = os.path.splitext(os.path.basename(twb))[0]
    pascal = "".join(w[:1].upper() + w[1:]
                     for w in re.split(r"[^0-9A-Za-z]+", name) if w)
    return os.path.join(output_root, pascal)


def prepare(args) -> int:
    """Stage 0 + 1 + 6: load constitution cache, parse workbook, pre-translate DAX."""
    # Stage 0 — deterministic constitution snapshot (hard-stop if files missing).
    out_dir = _output_dir_for(args.output_root, args.twb)
    rc = run([sys.executable, LOAD_CONST, out_dir])
    if rc != 0:
        return rc
    # Stage 1 — parse .twb -> analysis.json
    rc = run([sys.executable, PARSE, args.twb, "--output-root", args.output_root])
    if rc != 0:
        return rc
    analysis = analysis_path_for(args.output_root, args.twb)
    rc = run([sys.executable, MAPDAX, analysis])
    if rc != 0:
        return rc
    # Stage 6.5 — binary-route remaining work and emit the agent's self-contained
    # to-do list (classification.json, schema-easy.json, agent-todo.json).
    rc = run([sys.executable, CLASSIFY, analysis])
    if rc != 0:
        return rc
    _print_gaps(analysis)
    return 0


def _print_gaps(analysis: str) -> None:
    """Tell the agent exactly what the batched gap-fill call must author.

    Sources the routing from classification.json (authoritative binary route) and
    points the agent at agent-todo.json, the self-contained work list it reads.
    """
    with open(analysis, encoding="utf-8-sig") as fh:
        ir = json.load(fh)
    ambiguous = [w["name"] for w in ir.get("worksheets", [])
                 if w.get("inferredVisualType") is None]
    out_dir = os.path.dirname(analysis)
    cls_path = os.path.join(out_dir, "classification.json")
    schema_route, agent_measures = "agent", []
    if os.path.isfile(cls_path):
        with open(cls_path, encoding="utf-8-sig") as fh:
            cls = json.load(fh)
        schema_route = cls.get("schema", {}).get("route", "agent")
        agent_measures = [m["caption"] for m in cls.get("measures", [])
                          if m.get("route") == "agent"]
    print("\n=== AGENT GAP-FILL REQUIRED (one batched call -> agent-fragment.json) ===")
    print(f"  folder              : {out_dir}")
    print(f"  schema route        : {schema_route}")
    print(f"  agent DAX measures  : {len(agent_measures)} -> {agent_measures}")
    print(f"  ambiguous visuals   : {len(ambiguous)} -> {ambiguous}")
    print("  read: agent-todo.json (self-contained work list)")
    print("  write: agent-fragment.json (see scripts/contracts/fragment_schema.json)")
    print("  then: python pipeline.py merge <analysis>  -> assembles decisions.json")


def merge_cmd(args) -> int:
    """Stage 6.9: assemble dax-partial + schema-easy + agent-fragment -> decisions.json.

    Deterministic merge done by script (not the agent). Exit 4 means a pending
    measure is still missing -> the orchestrator escalates to the Opus fallback.
    """
    cmd = [sys.executable, MERGE, args.analysis]
    if args.agent_fragment:
        cmd += ["--agent-fragment", args.agent_fragment]
    if args.out:
        cmd += ["--out", args.out]
    return run(cmd)


def generate(args) -> int:
    """Stage 10 + 13 + validation: emit model and report, then validate."""
    # Guard: no pending measure may be silently dropped. If the agent omitted any,
    # this fails (exit 4) and writes measures-todo.json so the agent re-authors them.
    rc = run([sys.executable, RECONCILE, args.analysis, "--decisions", args.decisions])
    if rc != 0:
        return rc
    rc = run([sys.executable, EMIT_TMDL, args.analysis, "--decisions", args.decisions])
    if rc != 0:
        return rc
    rc = run([sys.executable, EMIT_PBIR, args.analysis, "--decisions", args.decisions])
    if rc != 0:
        return rc
    project_dir = os.path.dirname(os.path.abspath(args.analysis))
    model_name = _model_name(args.decisions, project_dir)
    sm_def = os.path.join(project_dir, f"{model_name}.SemanticModel", "definition")
    if os.path.isfile(TMDL_VALIDATE):
        run([TMDL_VALIDATE, sm_def])
    # Guard: every report visual field must bind to a column/measure that actually
    # exists on its table (catches fact columns mis-bound to a synthetic DimDate). [main]
    rc_bind = run([sys.executable, VALIDATE_BINDINGS, project_dir])
    if rc_bind != 0:
        return rc_bind
    rc_pbip = run([sys.executable, VALIDATE, project_dir])
    # Semantic check: every relationship/measure reference must resolve, types
    # must match, no reserved VAR names, no illegal PBIR properties. This is the
    # check that catches "won't open in Desktop" errors the syntax validators miss. [HEAD]
    rc_sem = run([sys.executable, SEM_VALIDATE, project_dir])
    return rc_sem if rc_sem != 0 else rc_pbip


def _model_name(decisions_path: str, project_dir: str) -> str:
    with open(decisions_path, encoding="utf-8-sig") as fh:
        return json.load(fh).get("modelName") or os.path.basename(project_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hybrid migration orchestrator.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare", help="Stage 1 + 6 (deterministic)")
    p_prep.add_argument("twb")
    p_prep.add_argument("--output-root", default="Output")
    p_prep.set_defaults(func=prepare)

    p_merge = sub.add_parser("merge", help="Stage 6.9: assemble decisions.json from fragments")
    p_merge.add_argument("analysis")
    p_merge.add_argument("--agent-fragment", help="path to agent-fragment.json")
    p_merge.add_argument("--out", help="output decisions.json path")
    p_merge.set_defaults(func=merge_cmd)

    p_gen = sub.add_parser("generate", help="Stage 10 + 13 + validation")
    p_gen.add_argument("analysis")
    p_gen.add_argument("--decisions", required=True)
    p_gen.set_defaults(func=generate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
