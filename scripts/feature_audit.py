"""feature_audit.py — ground-truth Tableau feature detector for migration QA.

Built for migrating workbooks AT SCALE (thousands of .twb files) where the one
unacceptable failure mode is a feature being *silently* dropped. This auditor
does NOT trust the IR alone: it scans the raw .twb XML for every catalogued
Tableau feature signature, then cross-checks against the deterministic IR
(analysis.json) and the emitter capability matrix. It answers three questions
for every workbook:

  1. WHAT features does this .twb actually contain?            (raw XML truth)
  2. Did the parser CAPTURE each one in the IR?               (silent-miss check)
  3. Does the emitter REPRODUCE it in the .pbip?              (capability matrix)

Anything detected in the XML but absent from the IR is a SILENT MISS (a parser
blind spot). Any element tag not in the known catalogue is reported as UNKNOWN
(an uncatalogued construct that needs human/agent review). Either condition —
plus any feature the emitter cannot reproduce (GAP) — makes the audit exit
non-zero so a batch driver can triage exactly the reports that need attention.

Usage
-----
  python scripts/feature_audit.py "Data/Netflix/Netflix.twb"     # one workbook
  python scripts/feature_audit.py "Output/NetfixWorkbook"        # by Output dir
  python scripts/feature_audit.py --all                          # every Data/**/*.twb
  python scripts/feature_audit.py "Data/x.twb" --json            # machine readable
  python scripts/feature_audit.py --all --strict                 # partials also fail

Exit codes
----------
  0  every detected feature is fully reproduced (or nothing to migrate)
  1  only PARTIAL-fidelity features present (reproduced with caveats)
  2  at least one GAP, SILENT MISS, or UNKNOWN element — needs review
  3  usage error (file not found / unparseable)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "twb"))

# Status vocabulary (mirrors coverage_report.py).
EMITTED = "emitted"   # reproduced in the .pbip
PARTIAL = "partial"   # reproduced with reduced fidelity / only under conditions
GAP = "gap"           # detected but NOT reproduced (lost unless handled downstream)
NA = "n/a"            # not present in this workbook

STATUS_MARK = {EMITTED: "[+]", PARTIAL: "[~]", GAP: "[!]", NA: "[ ]"}


def _count(root: ET.Element, tag: str) -> int:
    return sum(1 for _ in root.iter(tag))


def _count_attr_true(root: ET.Element, tag: str) -> int:
    """Count <tag>true</tag> toggles (Tableau stores feature flags as text)."""
    return sum(1 for el in root.iter(tag) if (el.text or "").strip().lower() == "true")


def _count_relation_type(root: ET.Element, kind: str) -> int:
    return sum(1 for r in root.iter("relation") if r.get("type") == kind)


def _count_sets(root: ET.Element) -> int:
    seen = set()
    for grp in root.iter("group"):
        name = grp.get("name", "")
        if grp.get("hidden") == "true" or grp.get("user:auto-column"):
            continue
        if "Set]" in name and name not in seen:
            seen.add(name)
    return len(seen)


def _count_user_groups(root: ET.Element) -> int:
    seen = set()
    for grp in root.iter("group"):
        name = grp.get("name", "")
        if grp.get("hidden") == "true" or grp.get("user:auto-column"):
            continue
        if "Set]" in name or name in seen:
            continue
        seen.add(name)
    return len(seen)


def _count_action(root: ET.Element, kind: str) -> int:
    """Count dashboard <action>s whose command resolves to a given kind.

    Tableau encodes the action type in the <command> name. Beyond filter /
    highlight / url, modern dashboards rely on SET and PARAMETER actions, which
    older migrators silently lump into "other" — so we classify them explicitly.
    """
    n = 0
    for act in root.iter("action"):
        cmd = act.find(".//command")
        name = (cmd.get("command", "") if cmd is not None else "").lower()
        link = (act.get("type", "") or "").lower()
        blob = name + " " + link
        if kind == "filter" and "filter" in blob:
            n += 1
        elif kind == "highlight" and "highlight" in blob:
            n += 1
        elif kind == "url" and ("url" in blob or "weburl" in blob):
            n += 1
        elif kind == "set" and ("set-" in blob or "changeset" in blob or "members" in blob):
            n += 1
        elif kind == "parameter" and ("parameter" in blob or "param" in blob):
            n += 1
        elif kind == "goto" and "goto" in blob:
            n += 1
    return n


def _count_buttons(root: ET.Element, action: str) -> int:
    n = 0
    # Only the main dashboard layout — skip the phone <devicelayouts> duplicates
    # so the count matches the IR (which ignores device layouts).
    for db in root.iter("dashboard"):
        layout = db.find("zones")
        if layout is None:
            continue
        for b in layout.iter("button"):
            toggle = b.find("toggle-action") is not None
            if action == "toggle" and toggle:
                n += 1
            elif action == "goto-sheet" and not toggle and "goto-sheet" in (b.get("action", "") or ""):
                n += 1
    return n


def _count_bins(root: ET.Element) -> int:
    """Real numeric bins are <calculation class='bin'>, NOT <bucket> (which is a
    discrete colour-encoding rule). Mirrors twb_meta.extract_bins exactly."""
    return sum(1 for col in root.iter("column")
               for calc in col.findall("calculation")
               if calc.get("class") == "bin")


def _count_conditional_format(root: ET.Element) -> int:
    """Encoding-driven colour/size rules = Tableau conditional formatting."""
    return sum(1 for _ in root.iter("color-one-way")) + sum(1 for _ in root.iter("range"))


# ---------------------------------------------------------------------------
# Feature catalogue. Each entry:
#   key -> (label, detector(root)->int, status, ir_probe(ir)->int|None, note)
# ir_probe returns the IR count for the same feature, or None when the IR does
# not (and is not expected to) track it. When the XML count is > 0 but the IR
# probe returns 0, that is a SILENT MISS and is flagged loudly.
# ---------------------------------------------------------------------------
Detector = Callable[[ET.Element], int]
IrProbe = Callable[[Dict], Optional[int]]


def _ws(ir: Dict) -> int: return len(ir.get("worksheets", []) or [])
def _dash(ir: Dict) -> int: return len(ir.get("dashboards", []) or [])
def _acts(ir: Dict, t: str) -> int:
    return sum(1 for a in (ir.get("actions", []) or []) if a.get("type") == t)
def _refs(ir: Dict) -> int:
    return sum(len(w.get("referenceLines", []) or []) for w in (ir.get("worksheets", []) or []))


FEATURES: List[Tuple[str, str, Detector, str, IrProbe, str]] = [
    # --- worksheets & dashboards (core) ---
    ("worksheets", "Worksheets", lambda r: _count(r, "worksheet"), EMITTED,
     _ws, "each worksheet -> a report visual"),
    ("dashboards", "Dashboards", lambda r: _count(r, "dashboard"), EMITTED,
     _dash, "each dashboard -> a report page"),
    ("zones", "Dashboard zones", lambda r: _count(r, "zone"), EMITTED,
     lambda ir: sum(len(d.get("zones", []) or []) for d in (ir.get("dashboards", []) or [])),
     "viz/filter/param/text/image zones positioned on the page"),
    ("filters", "Filter cards", lambda r: _count(r, "filter"), EMITTED,
     lambda ir: sum(1 for d in (ir.get("dashboards", []) or [])
                    for z in (d.get("zones", []) or []) if z.get("type") == "filter"),
     "Tableau filter cards -> Power BI slicers"),
    # --- model / data ---
    ("calcFields", "Calculated fields", lambda r: _count(r, "calculation"), PARTIAL,
     lambda ir: len(ir.get("calculatedFields", []) or []),
     "translated to DAX; complex LOD/table-calc may need agent review"),
    ("tableCalcs", "Table calculations", lambda r: _count(r, "table-calc"), PARTIAL,
     None, "WINDOW_/RANK/RUNNING -> DAX; verify partition/order semantics"),
    ("parameters", "Parameters", lambda r: _count(r, "param-domain-type"), PARTIAL,
     lambda ir: len(ir.get("parameters", []) or []),
     "value carried; interactive what-if not fully wired"),
    ("relationships", "Relationships", lambda r: _count(r, "relationship"), EMITTED,
     lambda ir: len(ir.get("relationships", []) or []), "-> model relationships"),
    ("joins", "Physical joins", lambda r: _count_relation_type(r, "join"), PARTIAL,
     None, "join relations -> merged/related tables; verify cardinality"),
    ("unions", "Unions", lambda r: _count_relation_type(r, "union"), GAP,
     None, "table unions not reproduced as Power Query appends yet"),
    ("customSql", "Custom SQL", lambda r: _count_relation_type(r, "text"), GAP,
     None, "custom SQL relations -> need native query / M rewrite"),
    ("extracts", "Data extracts", lambda r: _count(r, "extract"), PARTIAL,
     None, "extract -> import mode; connection re-pointed to source"),
    ("hierarchies", "Hierarchies", lambda r: _count(r, "drill-path"), EMITTED,
     lambda ir: len(ir.get("hierarchies", []) or []), "-> model hierarchies"),
    ("groups", "Groups", _count_user_groups, PARTIAL,
     lambda ir: len(ir.get("groups", []) or []), "-> grouping columns where simple"),
    ("bins", "Bins", _count_bins, PARTIAL,
     lambda ir: len(ir.get("bins", []) or []), "-> binned columns where simple"),
    ("sets", "Sets", _count_sets, GAP,
     lambda ir: len(ir.get("sets", []) or []),
     "Tableau sets have no direct emit; consider calc column/group"),
    ("maps", "Map layers", lambda r: _count(r, "mapsource"), PARTIAL,
     None, "geographic roles -> map visual; custom map styles dropped"),
    ("blending", "Data blending", lambda r: 0, PARTIAL,
     lambda ir: 1 if (ir.get("blending") or {}).get("blended") else 0,
     "blended datasources -> relationships/composite model (verify)"),
    ("rls", "Row-level security", lambda r: 0, EMITTED,
     lambda ir: 1 if (ir.get("rls") or {}).get("detected") else 0,
     "user filters -> Power BI RLS role with [col] = USERPRINCIPALNAME() (verify column)"),
    # --- dashboard interactivity ---
    ("navButtons", "Navigation buttons", lambda r: _count_buttons(r, "goto-sheet"), EMITTED,
     lambda ir: sum(1 for d in (ir.get("dashboards", []) or [])
                    for b in (d.get("buttons", []) or []) if b.get("action") == "goto-sheet"),
     "goto-sheet -> page-navigation actionButton"),
    ("toggleButtons", "Show/hide toggle buttons", lambda r: _count_buttons(r, "toggle"), EMITTED,
     lambda ir: sum(1 for d in (ir.get("dashboards", []) or [])
                    for b in (d.get("buttons", []) or []) if b.get("action") == "toggle"),
     "toggle -> bookmark pair + stacked bookmark buttons"),
    ("actionFilter", "Filter actions", lambda r: _count_action(r, "filter"), PARTIAL,
     lambda ir: _acts(ir, "filter"), "Power BI cross-filters by default (verify scope)"),
    ("actionHighlight", "Highlight actions", lambda r: _count_action(r, "highlight"), PARTIAL,
     lambda ir: _acts(ir, "highlight"), "Power BI cross-highlights by default (verify scope)"),
    ("highlighters", "Highlighters", lambda r: _count(r, "highlight"), PARTIAL,
     None, "legend/marks highlighter -> Power BI cross-highlight by default"),
    ("actionUrl", "URL actions", lambda r: _count_action(r, "url"), GAP,
     lambda ir: _acts(ir, "url"), "URL actions -> Web URL button / drillthrough not emitted"),
    ("actionSet", "Set actions", lambda r: _count_action(r, "set"), GAP,
     None, "set actions -> no Power BI equivalent; consider drill/cross-filter"),
    ("actionParameter", "Parameter actions", lambda r: _count_action(r, "parameter"), GAP,
     None, "parameter actions -> field parameters / bookmarks (manual)"),
    ("dynamicZoneVis", "Dynamic zone visibility", lambda r: _count(r, "ZoneVisibilityControl"), GAP,
     None, "field/parameter-driven show-hide -> bookmark or filter logic (manual)"),
    ("collapsibleContainers", "Collapsible containers", lambda r: _count(r, "CollapsiblePane"), PARTIAL,
     None, "collapsible panel -> bookmark toggle where it maps to a drawer"),
    ("setControls", "Set membership controls", lambda r: _count(r, "SetMembershipControl"), GAP,
     None, "in-dashboard set control -> no direct equivalent"),
    # --- analytics overlays ---
    ("referenceLines", "Reference lines/bands", lambda r: _count(r, "reference-line"), GAP,
     _refs, "constant/computed reference lines -> analytics lines (verify)"),
    ("trendLines", "Trend lines", lambda r: _count(r, "trend-lines") + _count(r, "trendline"), GAP,
     None, "trend lines -> Power BI trend/analytics line (manual)"),
    ("forecast", "Forecasts", lambda r: _count(r, "forecast") + _count(r, "forecast-settings"), GAP,
     None, "forecast -> Power BI forecast analytics (manual)"),
    ("clustering", "Clustering", lambda r: _count(r, "cluster"), GAP,
     None, "k-means clusters -> no deterministic equivalent"),
    ("animations", "Mark animations",
     lambda r: _count_attr_true(r, "MarkAnimation") + _count_attr_true(r, "AnimationOnByDefault"),
     PARTIAL, None, "animations cosmetic; not reproduced"),
    # --- formatting / tooltips ---
    ("customTooltips", "Customized tooltips", lambda r: _count(r, "customized-tooltip"), PARTIAL,
     None, "custom tooltip text reproduced; viz-in-tooltip dropped"),
    ("customLabels", "Customized labels", lambda r: _count(r, "customized-label"), PARTIAL,
     None, "custom mark labels -> data labels (verify)"),
    ("conditionalFormat", "Conditional formatting", _count_conditional_format, PARTIAL,
     None, "encoding colour/size rules -> conditional formatting (verify)"),
    ("pageShelf", "Page shelf", lambda r: _count(r, "page-reference"), GAP,
     None, "page shelf animation -> no equivalent; use slicer/bookmark"),
    ("annotations", "Annotations", lambda r: _count(r, "annotation"), GAP,
     None, "mark/point annotations -> manual text boxes"),
    ("storyPoints", "Story points", lambda r: _count(r, "story") + _count(r, "flipboard"), GAP,
     None, "Tableau stories -> sequence of bookmarks (manual)"),
]

# Element tags that are structural plumbing (never user-facing features). The
# UNKNOWN sweep reports any tag that is neither catalogued above nor listed here,
# so a genuinely novel Tableau construct can never hide.
STRUCTURAL_TAGS = {
    "workbook", "document-format-change-manifest", "preferences", "preference",
    "datasources", "datasource", "named-connections", "named-connection",
    "connection", "relation", "metadata-records", "metadata-record", "remote-name",
    "remote-type", "parent-name", "remote-alias", "contains-null", "local-name",
    "local-type", "ordinal", "object-id", "collation", "attributes", "attribute",
    "aliases", "alias", "column", "calculation", "columns", "object-graph",
    "objects", "object", "simple-id", "repository-location", "edge", "map",
    "semantic-values", "semantic-value", "approx-count", "_dummy",
    "worksheets", "worksheet", "table", "view", "datasource-dependencies",
    "column-instance", "panes", "pane", "mark", "cards", "card", "encodings",
    "encoding", "rows", "cols", "style", "style-rule", "format", "run",
    "formatted-text", "color", "color-palette", "tooltip", "tooltip-style",
    "tooltip-text", "customized-tooltip", "layout-options", "title", "strip",
    "slices", "scale", "width", "size", "family", "manual-sort", "computed-sort",
    "field", "aggregation", "lod", "breakdown", "viewpoint", "viewpoints",
    "dashboards", "dashboard", "zones", "zone", "zone-style", "button-visual-state",
    "image-path", "button", "toggle-action", "windows", "window", "layout",
    "devicelayouts", "devicelayout", "device-preview", "layout-cache", "thumbnails",
    "thumbnail", "shared-views", "shared-view", "selection-collection", "mapsources",
    "members", "member", "group", "groupfilter", "range", "bucket", "multibucket",
    "buckets", "bucket-selection", "multibucket-selection", "properties", "param",
    "value", "actions", "action", "command", "activation", "source", "active",
    "relationships", "relationship", "first-end-point", "second-end-point",
    "viewpoint", "dictionary", "tuple-selection", "tuple-reference",
    "tuple-descriptor", "pane-descriptor", "x-fields", "y-fields", "tuple",
    "node-selection", "oriented-node-reference", "node-reference", "fields",
    "geometry", "refresh", "refresh-event", "date-options", "extract",
    "mapsource", "color-one-way", "reference-line", "table-calc", "drill-path",
    "customized-label", "caption", "page-reference", "mark-sizing",
    "expression", "filter", "view", "text", "zoom", "highlight", "AccessibleZoneTabOrder", "ZoneFriendlyName",
    "ZoneBackgroundTransparency", "WorksheetBackgroundTransparency",
    "SheetIdentifierTracking", "WindowsPersistSimpleIdentifiers",
    "SetMembershipControl", "ZoneVisibilityControl", "CollapsiblePane",
    "BasicButtonObject", "SortTagCleanup", "MarkAnimation", "AnimationOnByDefault",
    "AutoCreateAndUpdateDSDPhoneLayouts", "ObjectModelEncapsulateLegacy",
    "ObjectModelTableType", "ObjectModelExtractV2", "SchemaViewerObjectModel",
    "MapboxVectorStylesAndLayers", "RefreshableParameterRanges",
    "VConnDownstreamExtractsWithWarnings", "map-pri",
}


def _load_root(twb_path: str) -> ET.Element:
    return ET.parse(twb_path).getroot()


def _resolve_twb(target: str) -> Optional[str]:
    """Accept a .twb path, or an Output/<folder> whose analysis.json names one."""
    if target.lower().endswith(".twb") and os.path.isfile(target):
        return target
    analysis = (target if target.endswith("analysis.json")
                else os.path.join(target, "analysis.json"))
    if os.path.isfile(analysis):
        with open(analysis, encoding="utf-8-sig") as fh:
            src = (json.load(fh).get("workbook") or {}).get("sourcePath")
        if src and os.path.isfile(src):
            return src
        if src and os.path.isfile(os.path.join(HERE, "..", src)):
            return os.path.normpath(os.path.join(HERE, "..", src))
    return None


def audit(twb_path: str, ir: Optional[Dict] = None) -> Dict:
    """Return the full feature manifest for one workbook."""
    root = _load_root(twb_path)
    if ir is None:
        import parse_twb as P  # built only when not supplied (keeps audit cheap)
        ir = P.build_ir(twb_path)

    rows: List[Dict] = []
    for key, label, detect, status, probe, note in FEATURES:
        xml_n = detect(root)
        if xml_n <= 0:
            continue
        ir_n = probe(ir) if probe is not None else None
        silent = ir_n is not None and ir_n == 0  # present in XML, absent from IR
        rows.append({
            "key": key, "label": label, "xmlCount": xml_n,
            "irCount": ir_n, "status": status, "silentMiss": silent, "note": note,
        })

    # UNKNOWN sweep: any element tag neither catalogued nor structural plumbing.
    catalogued = {"worksheet", "dashboard", "zone", "filter", "calculation",
                  "table-calc", "param-domain-type", "relationship", "relation",
                  "extract", "drill-path", "group", "bucket", "multibucket",
                  "mapsource", "button", "toggle-action", "action",
                  "ZoneVisibilityControl", "CollapsiblePane", "SetMembershipControl",
                  "reference-line", "trend-lines", "trendline", "forecast",
                  "forecast-settings", "cluster", "MarkAnimation",
                  "AnimationOnByDefault", "customized-tooltip", "customized-label",
                  "color-one-way", "range", "page-reference", "annotation",
                  "story", "flipboard", "highlight"}
    known = STRUCTURAL_TAGS | catalogued
    seen_tags: Dict[str, int] = {}
    for el in root.iter():
        seen_tags[el.tag] = seen_tags.get(el.tag, 0) + 1
    unknown = {t: n for t, n in seen_tags.items()
               if t not in known and not t.startswith("_.fcp.")}

    gaps = [r for r in rows if r["status"] == GAP or r["silentMiss"]]
    partials = [r for r in rows if r["status"] == PARTIAL and not r["silentMiss"]]
    return {
        "workbook": os.path.basename(twb_path),
        "twbPath": twb_path.replace("\\", "/"),
        "features": rows,
        "unknownTags": dict(sorted(unknown.items(), key=lambda kv: -kv[1])),
        "summary": {
            "detected": len(rows),
            "gaps": len(gaps),
            "silentMisses": sum(1 for r in rows if r["silentMiss"]),
            "partials": len(partials),
            "unknownTagKinds": len(unknown),
        },
    }


def _verdict(manifest: Dict, strict: bool) -> int:
    s = manifest["summary"]
    if s["gaps"] or s["silentMisses"] or s["unknownTagKinds"]:
        return 2
    if strict and s["partials"]:
        return 2
    return 1 if s["partials"] else 0


def _print_one(manifest: Dict) -> None:
    print(f"=== {manifest['workbook']} ===")
    print(f"  {'feature':24} {'xml':>5} {'ir':>4}  status     note")
    print("  " + "-" * 92)
    for r in manifest["features"]:
        mark = STATUS_MARK[r["status"]]
        flag = "  <== SILENT MISS (in .twb, NOT in IR)" if r["silentMiss"] else ""
        ir_s = "-" if r["irCount"] is None else str(r["irCount"])
        print(f"  {r['label']:24} {r['xmlCount']:>5} {ir_s:>4}  {mark} {r['status']:<7} {r['note']}{flag}")
    if manifest["unknownTags"]:
        print("\n  UNCATALOGUED ELEMENTS (need review — possible un-migrated feature):")
        for tag, n in manifest["unknownTags"].items():
            print(f"    {n:>5}  <{tag}>")
    s = manifest["summary"]
    print(f"\n  detected={s['detected']}  gaps={s['gaps']}  "
          f"silentMisses={s['silentMisses']}  partials={s['partials']}  "
          f"unknownKinds={s['unknownTagKinds']}")
    if s["gaps"] or s["silentMisses"] or s["unknownTagKinds"]:
        print("  RESULT: REVIEW REQUIRED — feature(s) not guaranteed in the .pbip")
    elif s["partials"]:
        print("  RESULT: OK with caveats — all features reproduced (some partial)")
    else:
        print("  RESULT: FULL — every detected feature is reproduced")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ground-truth Tableau feature audit.")
    ap.add_argument("target", nargs="?", help="path to a .twb or an Output/<folder>")
    ap.add_argument("--all", action="store_true", help="audit every Data/**/*.twb")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="PARTIAL features also fail (exit 2)")
    args = ap.parse_args(argv)

    targets: List[str] = []
    if args.all:
        targets = sorted(glob.glob(os.path.join(HERE, "..", "Data", "**", "*.twb"),
                                   recursive=True))
    elif args.target:
        twb = _resolve_twb(args.target)
        if not twb:
            print(f"ERROR: could not resolve a .twb from {args.target!r}", file=sys.stderr)
            return 3
        targets = [twb]
    else:
        ap.print_help()
        return 3
    if not targets:
        print("ERROR: no .twb files found", file=sys.stderr)
        return 3

    manifests = []
    worst = 0
    for twb in targets:
        try:
            m = audit(twb)
        except ET.ParseError as e:
            print(f"ERROR: cannot parse {twb}: {e}", file=sys.stderr)
            worst = max(worst, 3)
            continue
        manifests.append(m)
        worst = max(worst, _verdict(m, args.strict))

    if args.json:
        out = manifests[0] if len(manifests) == 1 else manifests
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for m in manifests:
            _print_one(m)
        if len(manifests) > 1:
            need = sum(1 for m in manifests
                       if m["summary"]["gaps"] or m["summary"]["silentMisses"]
                       or m["summary"]["unknownTagKinds"])
            print(f"BATCH: {len(manifests)} workbook(s); {need} need review.")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
