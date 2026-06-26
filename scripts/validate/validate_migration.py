"""validate_migration.py — post-generate validation orchestrator (CLI).

Runs ONE validation iteration against a generated migration and maintains the
single lifecycle workbook:

    python scripts/validate/validate_migration.py <output_dir>
    python scripts/validate/validate_migration.py Output/NetfixWorkbook

What it does each call:
  1. Load the IR + decisions + the emitted PBIR/TMDL (model_read).
  2. Compare visuals, measures and filters (compare).
  3. Collect unresolved issues and check the early-termination rule against the
     previous iteration (iterate).
  4. Build or update <Model>_Validation.xlsx — rewrite the state sheets, append
     the Iterations row, embed side-by-side screenshot evidence (workbook).
  5. Write validation_result.json and print a banner.

Exit codes:
  0  validated        — no unresolved issues
  6  issues-found     — unresolved issues remain (more iterations may help)
  8  stopped-early    — same unresolved issues repeated; manual action required
  2  error            — missing artifacts / openpyxl not installed
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import glob
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import compare as C  # noqa: E402
import iterate as IT  # noqa: E402
import screenshots as SS  # noqa: E402
from model_read import MigrationArtifacts  # noqa: E402


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_twb(art) -> str:
    """Locate the real .twb/.twbx on this machine.

    The path stored in analysis.json is whatever absolute path the migration ran
    against, which may not exist here, so fall back to matching the basename under
    the repo's Data/ folder.
    """
    src = (art.analysis.get("workbook", {}) or {}).get("sourcePath") or ""
    if src and os.path.isfile(src):
        return src
    repo_root = os.path.dirname(os.path.dirname(art.out_dir))
    data_root = os.path.join(repo_root, "Data")
    want = os.path.basename(src).lower() if src else ""
    fallback = ""
    for dirpath, _dirs, files in os.walk(data_root):
        for fn in files:
            if fn.lower().endswith((".twb", ".twbx")):
                full = os.path.join(dirpath, fn)
                if want and fn.lower() == want:
                    return full
                fallback = fallback or full
    return fallback


# --------------------------------------------------------------------------- #
# fresh-capture bookkeeping
# --------------------------------------------------------------------------- #
# EVERY validation run regenerates REAL screenshots from both desktop apps. A
# run always deletes the previous pipeline-generated captures first so Tableau
# and Power BI Desktop are relaunched and every pane is recaptured from scratch —
# a saved image is never reused across runs.
def _clear_auto_captures(screens_dir: str) -> int:
    """Delete the previous run's captures so validation regenerates fresh from
    both desktop apps.

    Removes every pipeline capture: the per-page renders/meta
    (``page_*_tableau.png`` / ``page_*_powerbi.png`` + ``.json``) and the
    per-visual panes (``<key>_tableau.png`` / ``<key>_powerbi.png``). These are
    all reproducible by relaunching Tableau + Power BI Desktop, so a clean run is
    safe and runs unconditionally on every validation / refinement.
    """
    if not os.path.isdir(screens_dir):
        return 0
    removed = 0
    for pat in ("*_tableau.png", "*_powerbi.png",
                "page_*_powerbi*.json", "page_*_tableau*.json"):
        for f in glob.glob(os.path.join(screens_dir, pat)):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed


def _capture_powerbi(art, visuals, filters) -> int:
    """Capture REAL Power BI report pages from the .pbip via Power BI Desktop.

    Power BI renders whole pages, so each page is captured once and assigned to
    every visual/filter on it as ``<key>_powerbi.png``. Disabled by setting the
    env var ``PBI_CAPTURE=0``. Best-effort: if Power BI Desktop isn't installed,
    the window never appears (e.g. a sign-in prompt), or this isn't Windows, the
    Power BI panes stay as placeholders.

    Returns the number of ``<key>_powerbi.png`` files written.
    """
    if os.environ.get("PBI_CAPTURE", "1") == "0":
        print("  (PBI_CAPTURE=0 — skipping Power BI Desktop capture)")
        return 0
    try:
        import powerbi_capture as PC  # noqa: E402
    except ImportError:
        return 0

    pbip = os.path.join(art.out_dir, f"{art.model_name}.pbip")
    if not os.path.isfile(pbip):
        return 0

    # Always relaunch Power BI Desktop and recapture every page from the live
    # report — no previously-saved page or pane image is ever reused.
    screens_dir = SS.ensure_dir(art.out_dir)
    print("  launching Power BI Desktop to capture real report page(s) — "
          "this can take a minute (set PBI_CAPTURE=0 to skip)…")
    pages = PC.capture_report(art.out_dir, pbip, art.model_name)
    if not pages:
        print("  Power BI capture unavailable this run; panes left as "
              "placeholders (drop <key>_powerbi.png in to supply them).")
        return 0

    try:
        import crop as CROP  # noqa: E402
        from PIL import Image  # noqa: E402
    except ImportError:
        CROP = None
        Image = None

    norm_pages = {PC._norm(k): v for k, v in pages.items()}
    default_png = next(iter(pages.values()))
    single = len(pages) == 1

    def _png_for(page_name) -> str:
        if single or not page_name or page_name == "—":
            return default_png
        return norm_pages.get(PC._norm(page_name), default_png)

    # cache of capture layers keyed by png path: each entry is a list of
    # (clean_page_image, logical→pixel scale, max_logical_y) ordered
    # highest-resolution first, so a visual is cropped from the sharpest layer
    # that still contains it.
    page_cache: dict = {}

    def _load_page(png_path):
        if png_path in page_cache:
            return page_cache[png_path]
        layers = []
        meta = os.path.splitext(png_path)[0] + ".json"
        layer_specs = None
        primary_scale = None
        if os.path.isfile(meta):
            try:
                with open(meta, encoding="utf-8") as fh:
                    data = json.load(fh)
                primary_scale = data.get("scale")
                layer_specs = data.get("layers")
            except (OSError, json.JSONDecodeError):
                layer_specs = None
        if Image is not None and layer_specs:
            base_dir = os.path.dirname(png_path)
            for spec in layer_specs:
                lp = os.path.join(base_dir, spec.get("image", ""))
                if not os.path.isfile(lp):
                    continue
                try:
                    limg = Image.open(lp).convert("RGB")
                except OSError:
                    continue
                layers.append((limg, spec.get("scale"), spec.get("maxY")))
        elif Image is not None and png_path and os.path.isfile(png_path):
            # legacy single-layer meta (no "layers" list)
            try:
                img = Image.open(png_path).convert("RGB")
            except OSError:
                img = None
            if img is not None:
                layers.append((img, primary_scale, None))
        page_cache[png_path] = layers
        return layers

    pos_by_name = {v["name"]: v for v in art.emitted_visuals}

    cropped = 0
    screens_dir = SS.ensure_dir(art.out_dir)
    targets = [(r.get("screenshotKey"), r.get("powerBiPage"),
                r.get("powerBiVisualName")) for r in visuals]
    targets += [(r.get("screenshotKey"), r.get("powerBiPage"),
                 r.get("powerBiVisualName")) for r in filters]
    for key, page, vname in targets:
        if not key:
            continue
        dst = os.path.join(screens_dir, f"{key}_powerbi.png")
        if os.path.isfile(dst):
            continue  # never overwrite a user-supplied capture
        src = _png_for(page)
        layers = _load_page(src)
        # Visual-by-visual only: crop just this visual out of the clean page
        # image, choosing the HIGHEST-resolution layer that still contains the
        # whole visual (max_logical_y >= visual bottom). If the visual cannot be
        # isolated, leave a placeholder rather than substituting the whole page.
        vis = pos_by_name.get(vname) if vname else None
        crop_img = None
        if CROP is not None and vis and layers:
            pos = vis["position"]
            v_bottom = float(pos.get("y", 0)) + float(pos.get("height", 0))
            for limg, lscale, lmaxy in layers:
                if not lscale:
                    continue
                if lmaxy is not None and v_bottom > lmaxy + 1:
                    continue  # visual clipped in this (sharper) layer
                crop_img = CROP.crop_visual(limg, lscale, pos)
                if crop_img is not None:
                    break
        if crop_img is not None:
            crop_img.save(dst)
            cropped += 1

    print(f"  {len(pages)} real Power BI page(s); {cropped} per-visual "
          f"crop(s) assigned.")
    return cropped


def _capture_tableau(art, visuals, filters) -> int:
    """Capture REAL per-worksheet screenshots from Tableau Desktop.

    Opens each worksheet maximized in Tableau Desktop (presentation mode) and
    captures it at full-screen resolution — the genuine Tableau render with live
    data, far sharper than the ~192px thumbnail embedded in the .twb and free of
    dashboard clipping. Disabled with ``TAB_CAPTURE=0``. Best-effort: if Tableau
    Desktop isn't installed, a connection/sign-in prompt blocks it, or this isn't
    Windows, the affected worksheets fall back to the embedded thumbnail.

    Each captured worksheet image is assigned to every visual/filter that maps to
    it as ``<key>_tableau.png`` (never overwriting an existing file). Returns the
    number of ``<key>_tableau.png`` files written.
    """
    if os.environ.get("TAB_CAPTURE", "1") == "0":
        print("  (TAB_CAPTURE=0 — skipping Tableau Desktop capture)")
        return 0
    try:
        import tableau_capture as TC  # noqa: E402
    except ImportError:
        return 0

    twb = _resolve_twb(art)
    if not twb:
        return 0

    screens = SS.ensure_dir(art.out_dir)
    # worksheet name -> keys still needing a Tableau capture
    ws_keys: dict = {}
    for rec in (visuals + filters):
        key = rec.get("screenshotKey")
        ws = rec.get("tableauWorksheet") or rec.get("worksheet")
        if not key or not ws or ws == "—":
            continue
        if os.path.isfile(os.path.join(screens, f"{key}_tableau.png")):
            continue  # user-supplied or already captured — never overwrite
        ws_keys.setdefault(ws, []).append(key)
    if not ws_keys:
        return 0

    caps = TC.capture_report(art.out_dir, twb, art.model_name, list(ws_keys))
    made = 0
    for ws, keys in ws_keys.items():
        src = caps.get(ws)
        if not src or not os.path.isfile(src):
            continue
        for key in keys:
            dst = os.path.join(screens, f"{key}_tableau.png")
            if os.path.isfile(dst):
                continue
            try:
                shutil.copyfile(src, dst)
                made += 1
            except OSError:
                pass
    if made:
        print(f"  staged {made} real Tableau Desktop worksheet capture(s)")
    elif ws_keys:
        print("  Tableau Desktop capture unavailable this run; falling back to "
              "embedded thumbnails.")
    return made



def _generate_screenshots(art, visuals, filters) -> int:
    """Stage REAL evidence for every visual + filter.

    The Tableau pane is a REAL Tableau Desktop capture of the worksheet (see
    ``_capture_tableau``); worksheets Tableau can't capture fall back to the
    genuine thumbnail Tableau embedded in the .twb. The Power BI pane is a REAL
    Power BI Desktop capture of the rendered report page (see ``_capture_powerbi``).
    Both never overwrite a user-supplied PNG of the same name.

    Returns ``(tableau_thumbnail_count, power_bi_capture_count)``.
    """
    # ALWAYS start from a clean slate: every validation / refinement recaptures
    # the ENTIRE set of screenshots fresh from BOTH desktop apps — a previous
    # run's images are never reused. Delete all pipeline captures up front so
    # Tableau Desktop and Power BI Desktop are relaunched and every pane is
    # regenerated from the live reports on each run.
    screens_dir = SS.ensure_dir(art.out_dir)
    n = _clear_auto_captures(screens_dir)
    print(f"  fresh run — cleared {n} previous capture(s); regenerating EVERY "
          "screenshot from Tableau + Power BI Desktop (no image is reused).")

    # real Tableau Desktop per-worksheet captures (highest-fidelity Tableau side)
    _capture_tableau(art, visuals, filters)

    try:
        import tableau_thumbs as TT  # noqa: E402  (catchable ImportError)
    except ImportError:
        return 0, 0

    twb = _resolve_twb(art)
    if not twb:
        print("  (no .twb/.twbx found — Tableau thumbnails unavailable)")
    else:
        thumbs = TT.load_thumbnails(twb)
        if not thumbs:
            print(f"  (no embedded thumbnails in {os.path.basename(twb)})")
        else:
            # Visual-by-visual evidence. Preference per visual/filter:
            #   1. the worksheet's OWN Tableau thumbnail (true per-visual render);
            #   2. else the real Tableau render of the DASHBOARD that contains it
            #      (a genuine image showing the visual in context — the analogue
            #      of the whole-page Power BI capture), so we show real evidence
            #      instead of a blank placeholder. Tableau only embeds a thumbnail
            #      for a subset of sheets, so most non-dashboard visuals rely on
            #      this dashboard-context fallback.
            ws2dash = TT.worksheet_dashboard_map(art.analysis)
            dash_thumbs = [d.get("name") for d in art.analysis.get("dashboards", [])
                           if TT._match(thumbs, d.get("name")) is not None]
            own = ctx_made = 0
            for rec in (visuals + filters):
                name = rec.get("tableauWorksheet") or rec.get("worksheet")
                key = rec.get("screenshotKey")
                if not key:
                    continue
                page = rec.get("powerBiPage")
                fb = [ws2dash.get(name), page, *dash_thumbs]
                primary = name if (name and name != "—") else (
                    dash_thumbs[0] if dash_thumbs else None)
                if not primary:
                    continue
                kind = TT.save_thumbnail(art.out_dir, key, primary, thumbs,
                                         fallback_names=fb)
                if kind == "own":
                    own += 1
                elif kind == "context":
                    ctx_made += 1
            if own or ctx_made:
                print(f"  staged {own} per-visual + {ctx_made} dashboard-context "
                      f"real Tableau thumbnail(s) from {os.path.basename(twb)}")

    # real Power BI Desktop captures (assigned per page)
    powerbi_made = _capture_powerbi(art, visuals, filters)

    # count whatever Tableau thumbnails now exist for the result banner
    tableau_made = sum(1 for r in (visuals + filters)
                       if r.get("screenshotKey") and os.path.isfile(
                           os.path.join(SS.screenshots_dir(art.out_dir),
                                        f"{r['screenshotKey']}_tableau.png")))
    return tableau_made, powerbi_made


def _overall(visuals, measures, filters, stopped_early) -> str:
    pass_test = lambda s: IT._is_pass(s)  # noqa: E731
    unresolved = (sum(1 for r in visuals if not pass_test(r["matchStatus"]))
                  + sum(1 for r in measures if not pass_test(r["matchStatus"]))
                  + sum(1 for r in filters if not pass_test(r["matchStatus"])))
    if stopped_early:
        return f"Stopped early — {unresolved} repeated unresolved issue(s)"
    if unresolved == 0:
        return "Validated — full one-to-one fidelity"
    return f"Issues found — {unresolved} item(s) need review"


def run(out_dir: str) -> int:
    out_dir = os.path.abspath(out_dir)
    if not os.path.isfile(os.path.join(out_dir, "analysis.json")):
        print(f"ERROR: no analysis.json under {out_dir}; run migrate first.",
              file=sys.stderr)
        return 2

    try:
        import workbook as WB  # noqa: E402  (import here so the ImportError is catchable)
    except ImportError:
        print("ERROR: openpyxl is required for the validation workbook.\n"
              "       pip install -r requirements.txt", file=sys.stderr)
        return 2

    art = MigrationArtifacts(out_dir)
    visuals = C.compare_visuals(art)
    measures = C.compare_measures(art)
    filters = C.compare_filters(art)

    # Auto-generate the side-by-side screenshot evidence (visual + filter) from
    # the source data, unless the user dropped real captures in already.
    tableau_made, powerbi_made = _generate_screenshots(art, visuals, filters)

    issues = IT.collect_issues(visuals, measures, filters)
    history = IT.load_history(out_dir)
    stop, repeated = IT.evaluate_termination(history, issues)
    iteration = IT.next_iteration_number(out_dir)
    ts = _timestamp()

    # iteration history record (persisted for the next run's comparison)
    record = IT.make_iteration_record(iteration, visuals, measures, filters,
                                      issues, stop, ts)
    IT.append_iteration(out_dir, record)
    history = IT.load_history(out_dir)

    repeated_ctx = [{"subject": i["subject"], "status": i["status"],
                     "diagnosis": IT.diagnose(i)} for i in repeated]

    iter_notes = _iteration_notes(history, stop, issues, repeated)
    overall = _overall(visuals, measures, filters, stop)
    ctx = {
        "modelName": art.model_name,
        "workbook": art.result.get("workbook") or art.analysis.get(
            "workbook", {}).get("sourcePath", "—"),
        "iteration": iteration,
        "timestamp": ts,
        "overallStatus": overall,
        "areaStats": [
            ("Visual Mapping", len(visuals),
             sum(1 for r in visuals if IT._is_pass(r["matchStatus"]))),
            ("Measure Validation", len(measures),
             sum(1 for r in measures if IT._is_pass(r["matchStatus"]))),
            ("Filter Validation", len(filters),
             sum(1 for r in filters if IT._is_pass(r["matchStatus"]))),
        ],
        "iterationNotes": iter_notes,
        "repeatedIssues": repeated_ctx,
    }

    xlsx = WB.build_or_update(out_dir, ctx, visuals, measures, filters, history)

    result = {
        "status": ("stopped_early" if stop
                   else ("validated" if not issues else "issues_found")),
        "iteration": iteration,
        "timestamp": ts,
        "workbook": xlsx,
        "overall": overall,
        "counts": {
            "visuals": len(visuals), "measures": len(measures),
            "filters": len(filters), "unresolved": len(issues),
        },
        "tableauThumbnails": tableau_made,
        "powerBiCaptures": powerbi_made,
        "repeatedIssues": repeated_ctx,
    }
    with open(os.path.join(out_dir, "validation", "validation_result.json"),
              "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    _banner(result, xlsx)
    if stop:
        return 8
    return 0 if not issues else 6


def _iteration_notes(history, stop, issues, repeated):
    notes = [f"Single validation workbook maintained across all iterations "
             f"(total iterations so far: {len(history)})."]
    if len(history) == 1:
        notes.append("Iteration 1 — workbook created.")
    else:
        notes.append(f"Iteration {history[-1]['iteration']} — existing workbook "
                     "updated in place; Iterations sheet appended.")
    if stop:
        notes.append("EARLY STOP: the same unresolved issue(s) reappeared in two "
                     "consecutive iterations with no measurable improvement. No "
                     "further iterations and no PBIP regeneration were performed.")
        for i in repeated:
            notes.append(f"   • repeated: {i['category']} '{i['subject']}' "
                         f"({i['status']}).")
    elif not issues:
        notes.append("All validation areas passed — no further iterations required.")
    else:
        notes.append(f"{len(issues)} unresolved item(s); another iteration may "
                     "resolve them after correction.")
    return notes


def _banner(result, xlsx):
    print("\n" + "=" * 64)
    print(f"  VALIDATION: {result['status'].upper()}  (iteration {result['iteration']})")
    print("=" * 64)
    c = result["counts"]
    print(f"  visuals   : {c['visuals']}")
    print(f"  measures  : {c['measures']}")
    print(f"  filters   : {c['filters']}")
    print(f"  unresolved: {c['unresolved']}")
    print(f"  screenshots: {result.get('tableauThumbnails', 0)} real Tableau "
          f"thumbnail(s) + {result.get('powerBiCaptures', 0)} real Power BI "
          f"Desktop pane(s)")
    print(f"  overall   : {result['overall']}")
    print(f"  workbook  : {xlsx}")
    if result["repeatedIssues"]:
        print("  repeated unresolved issues (manual action required):")
        for it in result["repeatedIssues"]:
            print(f"    - {it['subject']} [{it['status']}]: "
                  f"{it['diagnosis']['manualAction']}")
    print("=" * 64 + "\n")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_migration.py <output_dir>", file=sys.stderr)
        return 2
    return run(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
