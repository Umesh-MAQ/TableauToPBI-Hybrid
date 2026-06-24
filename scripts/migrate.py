"""migrate.py — the single-command hybrid Tableau -> Power BI orchestrator.

This is the entry point the agent layer (and humans) call. It wraps the
deterministic engine (pipeline.py + twb/dax/classify/emit) and adds three
things the older repos lacked:

  1. ONE command end-to-end.  `python scripts/migrate.py run "Data/Netflix"`
     discovers the .twb, parses it, pre-translates the safe DAX, decides whether
     an AI gap-fill is even needed, and — when it is not — emits the whole .pbip
     with ZERO AI cost.

  2. A machine-readable gap envelope.  When the workbook genuinely needs the LLM
     (complex LOD/table-calc DAX, a multi-table star schema), migrate.py writes
     MIGRATION_RESULT.json with status="needs_agent" and points the agent at the
     self-contained agent-todo.json. The orchestrator agent does ONE batched
     gap-fill call, writes agent-fragment.json, then calls `migrate.py finish`.

  3. Parallel generation.  The two independent emitters (TMDL semantic model and
     PBIR report) run concurrently, and the read-only validators run concurrently
     after them. This is the bulk of the "any report < 5 minutes" budget.

Commands
--------
  run    <folder|twb>   Parse + classify, then auto-generate if deterministic,
                        else emit a needs_agent envelope.
  finish <folder|twb>   Merge the agent fragment + generate (parallel).
  generate <folder|twb> Force a (re)generate from an existing decisions.json.

Exit codes
----------
  0  complete  — a valid .pbip was written (deterministic or after finish)
  7  needs_agent — gap envelope written; orchestrator must run the gap-fill agent
  2  error     — a stage failed (see stderr / MIGRATION_RESULT.json)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "twb"))
sys.path.insert(0, os.path.join(HERE, "merge"))
import mark_infer as MI  # noqa: E402  (single source of truth for visual gating)
import feature_audit as FA  # noqa: E402  (deterministic fidelity fingerprint)
import merge_decisions as MD  # noqa: E402  (DAX safety escalation detector)
import screenshot_overlay as SO  # noqa: E402  (dashboard screenshot discovery)

PIPELINE = os.path.join(HERE, "pipeline.py")
LOAD_CONST = os.path.join(HERE, "load_constitution.py")
PARSE = os.path.join(HERE, "twb", "parse_twb.py")
MAPDAX = os.path.join(HERE, "dax", "map_dax.py")
CLASSIFY = os.path.join(HERE, "classify", "classify.py")
MERGE = os.path.join(HERE, "merge", "merge_decisions.py")
RECONCILE = os.path.join(HERE, "dax", "reconcile.py")
EMIT_TMDL = os.path.join(HERE, "emit", "emit_tmdl.py")
EMIT_PBIR = os.path.join(HERE, "emit", "emit_pbir.py")
VALIDATE_BINDINGS = os.path.join(HERE, "emit", "validate_bindings.py")
VALIDATE_PBIP = os.path.join(
    HERE, "..", "plugins", "pbip", "skills", "pbip", "scripts", "validate_pbip.py")
SEM_VALIDATE = os.path.join(HERE, "validate_semantics.py")
BIN_DIR = os.path.join(HERE, "..", "plugins", "pbip", "hooks", "bin")

PY = sys.executable


# --------------------------------------------------------------------------- #
# small process helpers
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    """Run a child process, capturing combined output. Returns (rc, output)."""
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _echo(cmd: List[str], cwd: Optional[str] = None) -> int:
    """Run a child process, streaming output live. Returns rc."""
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    return subprocess.call([str(c) for c in cmd], cwd=cwd)


def _tmdl_validate_binary() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    if system == "windows":
        name = "tmdl-validate-windows-x64.exe"
    elif system == "darwin":
        name = "tmdl-validate-darwin-arm64" if is_arm else "tmdl-validate-darwin-x64"
    else:
        name = "tmdl-validate-linux-x64"
    return os.path.join(BIN_DIR, name)


# --------------------------------------------------------------------------- #
# discovery + naming
# --------------------------------------------------------------------------- #
def _pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:]
                   for w in re.split(r"[^0-9A-Za-z]+", name) if w)


def discover_twb(target: str) -> str:
    """Accept a .twb/.twbx path OR a folder; return the workbook path."""
    if os.path.isfile(target) and target.lower().endswith((".twb", ".twbx")):
        return os.path.abspath(target)
    if os.path.isdir(target):
        hits: List[str] = []
        for root, _dirs, files in os.walk(target):
            for f in files:
                if f.lower().endswith((".twb", ".twbx")):
                    hits.append(os.path.join(root, f))
        if not hits:
            raise FileNotFoundError(
                f"No .twb/.twbx workbook found under: {target}")
        # Prefer a top-level workbook; otherwise the first found.
        hits.sort(key=lambda p: (p.count(os.sep), p.lower()))
        return os.path.abspath(hits[0])
    raise FileNotFoundError(f"Not a workbook or folder: {target}")


def out_dir_for(output_root: str, twb: str) -> str:
    name = os.path.splitext(os.path.basename(twb))[0]
    return os.path.join(output_root, _pascal(name))


# --------------------------------------------------------------------------- #
# stage 1 — deterministic prepare (parse -> dax -> classify)
# --------------------------------------------------------------------------- #
def prepare(twb: str, output_root: str) -> Dict:
    """Run the deterministic front half and return the gap analysis."""
    odir = out_dir_for(output_root, twb)
    os.makedirs(odir, exist_ok=True)

    rc = _echo([PY, LOAD_CONST, odir])
    if rc != 0:
        raise RuntimeError("constitution load failed (run setup first)")

    rc = _echo([PY, PARSE, twb, "--output-root", output_root])
    if rc != 0:
        raise RuntimeError("twb parse failed")

    analysis = os.path.join(odir, "analysis.json")
    rc = _echo([PY, MAPDAX, analysis])
    if rc != 0:
        raise RuntimeError("dax pre-translation failed")

    rc = _echo([PY, CLASSIFY, analysis])
    if rc != 0:
        raise RuntimeError("classify failed")

    return gap_report(analysis)


def gap_report(analysis: str) -> Dict:
    """Read classification.json + IR and summarise what the agent must author."""
    odir = os.path.dirname(os.path.abspath(analysis))
    with open(analysis, encoding="utf-8-sig") as fh:
        ir = json.load(fh)
    cls_path = os.path.join(odir, "classification.json")
    with open(cls_path, encoding="utf-8-sig") as fh:
        cls = json.load(fh)

    cols = {c["name"] for c in ir.get("columns", [])}
    ambiguous = [w["name"] for w in ir.get("worksheets", [])
                 if w.get("inferredVisualType") is None
                 or MI.binding_needs_agent(w, cols)]
    agent_measures = [m["caption"] for m in cls.get("measures", [])
                      if m.get("route") == "agent"]
    schema_route = cls.get("schema", {}).get("route", "agent")

    # Output quality is the priority (time/cost are secondary): wherever the
    # deterministic path could only GUESS, route the worksheet to the agent so the
    # rendered output is correct rather than approximate. Three flavours of visual
    # ambiguity are gated here so they reach the agent (agent-todo carries the
    # visuals[] tasks) instead of being guessed:
    #   1. type ambiguity    — mark 'Automatic' whose shape the parser could not
    #      resolve (inferredVisualType is None);
    #   2. value ambiguity   — the chart TYPE is known but the worksheet exposes no
    #      bindable value, so the emitter would plot the first model measure;
    #   3. category ambiguity — the chart needs a category but it could only be
    #      guessed (the named field is not a real column, no date grain / real shelf
    #      dimension) — see mark_infer.binding_needs_agent.
    # This keeps the engine generic: any Tableau workbook whose visual cannot be
    # bound deterministically with confidence is routed to the agent for a better
    # result rather than mis-rendered.
    hard = (schema_route == "agent") or bool(agent_measures) or bool(ambiguous)

    return {
        "outDir": odir,
        "analysis": analysis,
        "modelName": ir.get("workbook", {}).get("pascalName", "Model"),
        "schemaRoute": schema_route,
        "agentMeasures": agent_measures,
        "ambiguousVisuals": ambiguous,
        "agentTodo": os.path.join(odir, "agent-todo.json"),
        "needsAgent": hard,
    }


# --------------------------------------------------------------------------- #
# stage 2 — parallel generate (emit TMDL || emit PBIR, then validators)
# --------------------------------------------------------------------------- #
def generate(analysis: str, decisions: str) -> Dict:
    """Reconcile, emit model + report in parallel, then validate in parallel."""
    t0 = time.time()
    odir = os.path.dirname(os.path.abspath(analysis))

    # Guard first: no pending measure may be silently dropped.
    rc, out = _run([PY, RECONCILE, analysis, "--decisions", decisions])
    if rc != 0:
        print(out)
        return {"ok": False, "stage": "reconcile", "rc": rc, "log": out}

    # Normalize any object name Power BI would reject (e.g. a Tableau LOD field
    # "{SUM([CY Sales])}") in BOTH decisions and IR before either emitter runs, so
    # the model loads with all measures instead of failing the whole load.
    _normalize_member_names(analysis, decisions)

    # The two emitters are independent (different output folders) -> parallelize.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut = {
            "tmdl": ex.submit(_run, [PY, EMIT_TMDL, analysis, "--decisions", decisions]),
            "pbir": ex.submit(_run, [PY, EMIT_PBIR, analysis, "--decisions", decisions]),
        }
        results = {k: f.result() for k, f in fut.items()}

    for stage, (rc, out) in results.items():
        print(f"\n--- emit:{stage} (rc={rc}) ---\n{out}")
        if rc != 0:
            return {"ok": False, "stage": f"emit:{stage}", "rc": rc, "log": out}

    model_name = _model_name(decisions, odir)
    sm_def = os.path.join(odir, f"{model_name}.SemanticModel", "definition")

    # Read-only validators run concurrently after the artifacts exist.
    checks: Dict[str, List[str]] = {
        "bindings": [PY, VALIDATE_BINDINGS, odir],
        "pbip": [PY, VALIDATE_PBIP, odir],
        "semantics": [PY, SEM_VALIDATE, odir],
    }
    tmdl_bin = _tmdl_validate_binary()
    if os.path.isfile(tmdl_bin):
        checks["tmdl"] = [tmdl_bin, sm_def]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(checks)) as ex:
        cfut = {k: ex.submit(_run, cmd) for k, cmd in checks.items()}
        cres = {k: f.result() for k, f in cfut.items()}

    validation: Dict[str, Dict] = {}
    hard_fail = False
    for name, (rc, out) in cres.items():
        validation[name] = {"rc": rc, "log": out.strip()[-4000:]}
        print(f"\n--- validate:{name} (rc={rc}) ---\n{out}")
        # tmdl-validate / pbip use rc 2 = error, 1 = warning; bindings/semantics
        # use non-zero = error. Treat rc>=2 as hard fail, rc==1 as warning.
        if rc >= 2 or (name in ("bindings",) and rc != 0):
            hard_fail = True

    # Deterministic fidelity fingerprint (pattern adopted from Agentic-AI-Solutions):
    # on EVERY run, cross-check which Tableau features the .twb actually contains
    # against what the parser captured and what the emitter reproduces. A silently
    # dropped feature (GAP / SILENT MISS / uncatalogued element) is surfaced here in
    # MIGRATION_RESULT.json instead of being discovered later in Power BI Desktop.
    # Advisory only: a gap may be intentional (agent-handled) and must never fail an
    # otherwise-valid emit.
    fidelity = _fidelity_fingerprint(analysis, odir)

    elapsed = round(time.time() - t0, 1)
    pbip = os.path.join(odir, f"{model_name}.pbip")
    return {
        "ok": not hard_fail,
        "stage": "generate",
        "pbip": pbip if os.path.isfile(pbip) else None,
        "modelName": model_name,
        "validation": validation,
        "fidelity": fidelity,
        "elapsedSec": elapsed,
    }


def _fidelity_fingerprint(analysis: str, odir: str) -> Optional[Dict]:
    """Cross-check emitted feature coverage against the raw .twb (advisory QA).

    Reuses ``feature_audit`` — the ground-truth detector that scans the .twb XML
    for every catalogued Tableau feature, confirms the parser captured it (IR) and
    that the emitter reproduces it. Returns a compact summary so a single, dropped
    feature is visible in the result envelope rather than only in Power BI Desktop.
    Never raises: fidelity reporting must not break an otherwise-valid generate.
    """
    try:
        with open(analysis, encoding="utf-8-sig") as fh:
            ir = json.load(fh)
        twb = FA._resolve_twb(odir)
        if not twb:
            return None
        manifest = FA.audit(twb, ir=ir)
        feats = manifest.get("features", [])
        return {
            "summary": manifest.get("summary", {}),
            "gaps": [r["label"] for r in feats
                     if r.get("status") == "gap" or r.get("silentMiss")],
            "silentMisses": [r["label"] for r in feats if r.get("silentMiss")],
            "unknownTags": list((manifest.get("unknownTags") or {}).keys())[:20],
        }
    except Exception as exc:  # advisory only — never break a valid generate
        return {"error": str(exc)}


def _model_name(decisions: str, odir: str) -> str:
    try:
        with open(decisions, encoding="utf-8-sig") as fh:
            return json.load(fh).get("modelName") or os.path.basename(odir)
    except Exception:
        return os.path.basename(odir)


def _uncovered_visuals(decisions_path: str, ambiguous: List[str]) -> List[str]:
    """Gated (ambiguous) worksheets that the merged decisions do NOT cover with a
    visualDecision. Under the output-first policy such visuals must re-gate to the
    agent rather than fall back to a deterministic guess at emit time."""
    if not ambiguous:
        return []
    try:
        with open(decisions_path, encoding="utf-8-sig") as fh:
            dec = json.load(fh)
    except Exception:
        return list(ambiguous)
    covered = {vd.get("worksheet") for vd in dec.get("visualDecisions", [])}
    return [w for w in ambiguous if w not in covered]


def _unsafe_dax_measures(decisions_path: str) -> List[str]:
    """Measures whose DAX the deterministic engine cannot safely emit (a measure
    ref survives inside a CALCULATE boolean filter after sanitization). Under the
    output-first policy these escalate to the agent rather than ship DAX Power BI
    Desktop rejects at runtime — the safety net the static validators cannot see."""
    try:
        with open(decisions_path, encoding="utf-8-sig") as fh:
            dec = json.load(fh)
    except Exception:
        return []
    return MD.unsafe_calculate_filter_measures(dec.get("measures", []))


def _ambiguous_from_analysis(analysis: str) -> List[str]:
    """Recompute the agent-gated worksheet list from the IR (same rule as
    gap_report): a mark whose visual type could not be resolved, or a chart whose
    binding can only be guessed. Used to re-check coverage at the generate choke
    point so a gated visual never silently falls back to a deterministic table."""
    try:
        with open(analysis, encoding="utf-8-sig") as fh:
            ir = json.load(fh)
    except Exception:
        return []
    cols = {c["name"] for c in ir.get("columns", [])}
    return [w["name"] for w in ir.get("worksheets", [])
            if w.get("inferredVisualType") is None or MI.binding_needs_agent(w, cols)]


# Power BI rejects these characters in a table/column/measure object name: square
# brackets [] are DAX reference delimiters and Tableau LOD braces {} are not valid
# member-name characters. A single bad name (e.g. the anonymous LOD field
# "{SUM([CY Sales])}") makes Power BI Desktop fail the WHOLE model load with "the
# '<name>' measure cannot be created", so NONE of the measures appear.
_PBI_BAD_NAME_CHARS = re.compile(r"[\[\]{}]")


def _pbi_member_name(name: str) -> str:
    """Return ``name`` stripped of the characters Power BI forbids in object names,
    with leftover whitespace collapsed. Never returns empty."""
    cleaned = _PBI_BAD_NAME_CHARS.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or (name or "").strip()


def _swap_refs(value: str, rename: Dict[str, str]) -> str:
    """Swap any renamed member name found in a string: exact field captions are
    replaced wholesale, and references embedded in template strings (chart titles,
    tooltip text using ``{field}``) are substring-replaced."""
    if value in rename:
        return rename[value]
    for old, new in rename.items():
        if old in value:
            value = value.replace(old, new)
    return value


def _rewrite_refs(node, rename: Dict[str, str]) -> None:
    """Recursively rewrite renamed member names through a JSON-like structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                node[k] = _swap_refs(v, rename)
            else:
                _rewrite_refs(v, rename)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = _swap_refs(v, rename)
            else:
                _rewrite_refs(v, rename)


def _normalize_member_names(analysis: str, decisions: str) -> Dict[str, str]:
    """Rename every measure / calculated column whose name Power BI would reject to a
    valid member name, in BOTH the decisions (model side) and the analysis IR
    (worksheet-binding side), so the ``name == Tableau caption`` invariant the
    emitters bind on is preserved with a VALID name. Returns the ``{old: new}`` map.

    This is the single emit-time choke point: an invalid object name can never reach
    Power BI regardless of whether it was authored by the agent or the deterministic
    translator, so a model that previously failed to load (no measures created) now
    loads with every measure intact."""
    try:
        with open(decisions, encoding="utf-8-sig") as fh:
            dec = json.load(fh)
    except Exception:
        return {}
    taken = {m.get("name", "").lower() for m in dec.get("measures", [])}
    taken |= {c.get("name", "").lower() for c in dec.get("calculatedColumns", [])}
    rename: Dict[str, str] = {}

    def _unique(base: str) -> str:
        cand, n = base, 2
        while cand.lower() in taken and cand not in rename.values():
            cand, n = f"{base} {n}", n + 1
        taken.add(cand.lower())
        return cand

    for m in dec.get("measures", []):
        nm = m.get("name", "")
        new = _pbi_member_name(nm)
        if nm and new != nm:
            taken.discard(nm.lower())
            new = _unique(new)
            rename[nm] = new
            m["name"] = new
    for c in dec.get("calculatedColumns", []):
        nm = c.get("name", "")
        new = _pbi_member_name(nm)
        if nm and new != nm:
            taken.discard(nm.lower())
            new = _unique(new)
            rename[nm] = new
            c["name"] = new
    if not rename:
        return {}

    def _fix_dax(dax: str) -> str:
        for old, new in rename.items():
            dax = dax.replace(f"[{old}]", f"[{new}]")
        return dax

    for m in dec.get("measures", []):
        if m.get("dax"):
            m["dax"] = _fix_dax(m["dax"])
    for c in dec.get("calculatedColumns", []):
        if c.get("dax"):
            c["dax"] = _fix_dax(c["dax"])
    _rewrite_refs(dec.get("visualDecisions", []), rename)
    with open(decisions, "w", encoding="utf-8") as fh:
        json.dump(dec, fh, indent=2, ensure_ascii=False)

    try:
        with open(analysis, encoding="utf-8-sig") as fh:
            ir = json.load(fh)
    except Exception:
        return rename
    for cf in ir.get("calculatedFields", []):
        if cf.get("caption") in rename:
            cf["caption"] = rename[cf["caption"]]
    _rewrite_refs(ir.get("worksheets", []), rename)
    with open(analysis, "w", encoding="utf-8") as fh:
        json.dump(ir, fh, indent=2, ensure_ascii=False)
    if rename:
        print("  normalized invalid member name(s): "
              + ", ".join(f"{o!r}->{n!r}" for o, n in rename.items()))
    return rename


def merge(analysis: str, agent_fragment: Optional[str], out: str) -> int:
    cmd = [PY, MERGE, analysis]
    if agent_fragment and os.path.isfile(agent_fragment):
        cmd += ["--agent-fragment", agent_fragment]
    cmd += ["--out", out]
    return _echo(cmd)


# --------------------------------------------------------------------------- #
# result envelope
# --------------------------------------------------------------------------- #
def write_result(odir: str, payload: Dict) -> str:
    path = os.path.join(odir, "MIGRATION_RESULT.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return path


def _print_banner(payload: Dict) -> None:
    print("\n" + "=" * 64)
    print(f"  MIGRATION RESULT: {payload.get('status', '?').upper()}")
    print("=" * 64)
    for k in ("modelName", "pbip", "elapsedSec"):
        if payload.get(k) is not None:
            print(f"  {k:12}: {payload[k]}")
    if payload.get("status") == "needs_agent":
        g = payload.get("gaps", {})
        print(f"  schemaRoute : {g.get('schemaRoute')}")
        print(f"  agentMeasures ({len(g.get('agentMeasures', []))}): {g.get('agentMeasures')}")
        print(f"  ambiguousVisuals ({len(g.get('ambiguousVisuals', []))}): {g.get('ambiguousVisuals')}")
        if g.get("unsafeMeasures"):
            print(f"  unsafeMeasures ({len(g.get('unsafeMeasures', []))}): {g.get('unsafeMeasures')}")
        print(f"  -> read  : {payload.get('agentTodo')}")
        print(f"  -> write : {os.path.join(payload.get('outDir',''), 'agent-fragment.json')}")
        print(f"  -> then  : python scripts/migrate.py finish \"{payload.get('outDir')}\"")
    fid = payload.get("fidelity") or {}
    s = fid.get("summary") or {}
    if s:
        flagged = (s.get("gaps", 0) + s.get("silentMisses", 0)
                   + s.get("unknownTagKinds", 0))
        verdict = "REVIEW" if flagged else ("CAVEATS" if s.get("partials") else "FULL")
        print(f"  fidelity    : {verdict}  "
              f"(detected={s.get('detected', 0)} gaps={s.get('gaps', 0)} "
              f"silentMisses={s.get('silentMisses', 0)} "
              f"partials={s.get('partials', 0)} "
              f"unknownKinds={s.get('unknownTagKinds', 0)})")
        if fid.get("gaps"):
            print(f"     gaps     : {fid['gaps']}")
        if fid.get("unknownTags"):
            print(f"     unknown  : {fid['unknownTags']}")
    print("=" * 64 + "\n")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _report_screenshots(twb: str, odir: str) -> None:
    """Discover a report's dashboard screenshots and record them for the agent.

    The screenshots are the vision layer's input: the orchestrating agent reads
    them and authors ``visualHints`` in agent-fragment.json, which merge_decisions
    overlays onto the visual decisions (see screenshot_overlay.py). Writing the
    inventory here makes the convention discoverable and the run reproducible. Pure
    no-op for any report that ships no ``Screenshot(s)`` folder.
    """
    data_dir = os.path.dirname(os.path.abspath(twb))
    shots = SO.discover_screenshots(data_dir)
    if not shots:
        return
    n = sum(len(v) for v in shots.values())
    print(f"screenshots: {n} image(s), {len(shots)} dashboard(s): "
          f"{', '.join(sorted(shots))}")
    try:
        with open(os.path.join(odir, "screenshots.json"), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump({"dashboards": shots}, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def cmd_run(args) -> int:
    twb = discover_twb(args.target)
    print(f"workbook: {twb}")
    gaps = prepare(twb, args.output_root)
    odir = gaps["outDir"]
    _report_screenshots(twb, odir)

    if gaps["needsAgent"]:
        # Idempotent re-run: if a previously-authored agent fragment already exists
        # and still covers every required measure, reuse it and generate end-to-end
        # instead of re-gating to the agent. `merge` returns rc==0 only when the
        # fragment fully reconciles (rc==4 = missing measures), so this can never
        # silently emit an incomplete model.
        fragment = os.path.join(odir, "agent-fragment.json")
        if os.path.isfile(fragment):
            decisions = os.path.join(odir, "decisions.json")
            rc = merge(gaps["analysis"], fragment, decisions)
            # Output-first reuse guard: a fragment that fills the MEASURES (merge
            # rc==0) but omits a visualDecision for a gated (ambiguous) visual would
            # let that visual fall back to a deterministic guess. Only reuse when
            # every gated visual is also covered; otherwise re-gate to the agent so
            # the output is authored, not approximated.
            uncovered = _uncovered_visuals(decisions, gaps["ambiguousVisuals"]) if rc == 0 else None
            if rc == 0 and not uncovered:
                print("  reusing existing agent-fragment.json (gaps already filled)")
                return _finish_generate(gaps["analysis"], decisions, odir, twb,
                                        mode="hybrid")
            if uncovered:
                print(f"  existing agent-fragment.json missing visualDecisions for "
                      f"{len(uncovered)} gated visual(s) -> agent needed: {uncovered}")
            else:
                print("  existing agent-fragment.json incomplete -> agent needed")
        payload = {
            "status": "needs_agent",
            "workbook": twb,
            "outDir": odir,
            "modelName": gaps["modelName"],
            "analysis": gaps["analysis"],
            "agentTodo": gaps["agentTodo"],
            "gaps": {
                "schemaRoute": gaps["schemaRoute"],
                "agentMeasures": gaps["agentMeasures"],
                "ambiguousVisuals": gaps["ambiguousVisuals"],
            },
        }
        write_result(odir, payload)
        _print_banner(payload)
        return 7

    # Fully deterministic — no AI needed. Merge (no fragment) + generate.
    decisions = os.path.join(odir, "decisions.json")
    rc = merge(gaps["analysis"], None, decisions)
    if rc != 0:
        payload = {"status": "error", "stage": "merge", "rc": rc, "outDir": odir}
        write_result(odir, payload)
        _print_banner(payload)
        return 2
    return _finish_generate(gaps["analysis"], decisions, odir, twb, mode="deterministic")


def cmd_finish(args) -> int:
    twb = discover_twb(args.target)
    odir = out_dir_for(args.output_root, twb)
    analysis = os.path.join(odir, "analysis.json")
    if not os.path.isfile(analysis):
        print(f"ERROR: run prepare first; missing {analysis}", file=sys.stderr)
        return 2
    fragment = args.agent_fragment or os.path.join(odir, "agent-fragment.json")
    decisions = os.path.join(odir, "decisions.json")
    rc = merge(analysis, fragment, decisions)
    if rc == 4:
        print("ERROR: agent fragment is missing measures (reconcile exit 4). "
              "Re-author agent-fragment.json.", file=sys.stderr)
        return 2
    if rc != 0:
        payload = {"status": "error", "stage": "merge", "rc": rc, "outDir": odir}
        write_result(odir, payload)
        _print_banner(payload)
        return 2
    return _finish_generate(analysis, decisions, odir, twb, mode="hybrid")


def cmd_generate(args) -> int:
    twb = discover_twb(args.target)
    odir = out_dir_for(args.output_root, twb)
    analysis = os.path.join(odir, "analysis.json")
    decisions = args.decisions or os.path.join(odir, "decisions.json")
    if not (os.path.isfile(analysis) and os.path.isfile(decisions)):
        print("ERROR: need analysis.json + decisions.json", file=sys.stderr)
        return 2
    return _finish_generate(analysis, decisions, odir, twb, mode="manual")


def _finish_generate(analysis: str, decisions: str, odir: str,
                     twb: str, mode: str) -> int:
    # Output-first re-gate guard (single choke point for run-reuse, finish and
    # generate): a worksheet the deterministic engine could not analyse (gated as
    # ambiguous) must be authored by the agent, never silently emitted as a table
    # fallback. If the merged decisions omit a visualDecision for any gated visual,
    # re-gate to the agent instead of completing with a guessed table.
    uncovered = _uncovered_visuals(decisions, _ambiguous_from_analysis(analysis))
    if uncovered:
        payload = {
            "status": "needs_agent",
            "mode": mode,
            "workbook": twb,
            "outDir": odir,
            "modelName": _model_name(decisions, odir),
            "analysis": analysis,
            "agentTodo": os.path.join(odir, "agent-todo.json"),
            "gaps": {"ambiguousVisuals": uncovered},
        }
        write_result(odir, payload)
        _print_banner(payload)
        return 7
    # DAX safety choke point: a measure ref left inside a CALCULATE boolean filter
    # passes every static validator but is rejected by Power BI Desktop at runtime
    # (the yellow-warning measures). The deterministic sanitizer auto-fixes the
    # shapes it can; anything residual is escalated to the agent here rather than
    # silently emitted — "use the agent wherever the deterministic path will fail".
    unsafe = _unsafe_dax_measures(decisions)
    if unsafe:
        payload = {
            "status": "needs_agent",
            "mode": mode,
            "workbook": twb,
            "outDir": odir,
            "modelName": _model_name(decisions, odir),
            "analysis": analysis,
            "agentTodo": os.path.join(odir, "agent-todo.json"),
            "gaps": {"unsafeMeasures": unsafe},
        }
        write_result(odir, payload)
        _print_banner(payload)
        return 7
    result = generate(analysis, decisions)
    payload = {
        "status": "complete" if result.get("ok") else "error",
        "mode": mode,
        "workbook": twb,
        "outDir": odir,
        "modelName": result.get("modelName"),
        "pbip": result.get("pbip"),
        "elapsedSec": result.get("elapsedSec"),
        "validation": result.get("validation"),
        "fidelity": result.get("fidelity"),
    }
    if not result.get("ok"):
        payload["failedStage"] = result.get("stage")
        payload["log"] = result.get("log", "")[-2000:]
    write_result(odir, payload)
    _print_banner(payload)
    return 0 if result.get("ok") else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Single-command hybrid Tableau -> Power BI migrator.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="parse + classify, auto-generate if deterministic")
    pr.add_argument("target", help="folder containing a .twb/.twbx, or the workbook path")
    pr.add_argument("--output-root", default="Output")
    pr.set_defaults(func=cmd_run)

    pf = sub.add_parser("finish", help="merge agent fragment + generate (parallel)")
    pf.add_argument("target")
    pf.add_argument("--output-root", default="Output")
    pf.add_argument("--agent-fragment", default=None)
    pf.set_defaults(func=cmd_finish)

    pg = sub.add_parser("generate", help="(re)generate from an existing decisions.json")
    pg.add_argument("target")
    pg.add_argument("--output-root", default="Output")
    pg.add_argument("--decisions", default=None)
    pg.set_defaults(func=cmd_generate)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
