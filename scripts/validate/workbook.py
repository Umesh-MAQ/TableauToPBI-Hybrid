"""workbook.py — the single validation workbook (openpyxl).

Builds / updates ONE Excel file, ``<Model>_Validation.xlsx``, that is reused for
the whole migration lifecycle (requirement #2). Each run:

  * rewrites the per-state sheets — Summary, Visual Mapping, Measure Validation,
    Filter Validation — with the latest results, and
  * APPENDS a row to the Iterations sheet (full history preserved).

The Filter Validation and Visual Mapping sheets embed the side-by-side Tableau vs
Power BI screenshot evidence directly in the validation row (requirements #1 / #6).
A missing capture becomes a clearly-labelled placeholder naming the exact file to
drop in — so the workbook is always a standalone evidence document.

Requires openpyxl (declared in requirements.txt). The CLI handles ImportError.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import screenshots as SS

# ---- palette ---------------------------------------------------------------- #
_HDR_FILL = PatternFill("solid", fgColor="1F4E78")
_HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=15, color="1F4E78")
_SUB_FONT = Font(bold=True, size=12, color="1F4E78")
_WRAP = Alignment(wrap_text=True, vertical="top")
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_ALT_FILL = PatternFill("solid", fgColor="F2F6FB")

_STATUS_FILL = {
    "exact match": "C6EFCE", "equivalent": "C6EFCE", "match": "C6EFCE",
    "added control": "DDEBF7", "pass": "C6EFCE", "validated": "C6EFCE",
    "similar match": "FFEB9C", "review": "FFEB9C", "extra (review)": "FFEB9C",
    "needs correction": "FFC7CE", "missing": "FFC7CE", "extra": "FFC7CE",
    "stopped": "FFC7CE",
}
_STATUS_FONT = {
    "exact match": "006100", "equivalent": "006100", "match": "006100",
    "similar match": "9C6500", "review": "9C6500", "extra (review)": "9C6500",
    "needs correction": "9C0006", "missing": "9C0006", "extra": "9C0006",
}

# screenshot render box (pixels)
_IMG_W, _IMG_H = 380, 250


def _status_style(cell, status: str) -> None:
    key = (status or "").strip().lower()
    fill = next((v for k, v in _STATUS_FILL.items() if key.startswith(k)), None)
    font = next((v for k, v in _STATUS_FONT.items() if key.startswith(k)), None)
    if fill:
        cell.fill = PatternFill("solid", fgColor=fill)
    if font:
        cell.font = Font(bold=True, color=font)


def _header_row(ws, row: int, headers: List[str], widths: List[int]) -> None:
    for col, (h, w) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(row=row, column=col, value=h)
        c.fill, c.font, c.alignment, c.border = _HDR_FILL, _HDR_FONT, _CENTER, _BORDER
        ws.column_dimensions[get_column_letter(col)].width = w


def _data_row(ws, row: int, values: List, status_col: Optional[int] = None) -> None:
    for col, val in enumerate(values, start=1):
        c = ws.cell(row=row, column=col, value=val)
        c.alignment, c.border = _WRAP, _BORDER
        if row % 2 == 0:
            c.fill = _ALT_FILL
        if status_col and col == status_col:
            _status_style(c, str(val))


def _reset_sheet(wb: Workbook, name: str):
    if name in wb.sheetnames:
        del wb[name]
    return wb.create_sheet(name)


def _scaled_image(path: str):
    """Return (XLImage scaled to fit the evidence box, display_height_px)."""
    try:
        img = XLImage(path)
    except Exception:
        return None, 0
    w, h = img.width or _IMG_W, img.height or _IMG_H
    scale = min(_IMG_W / w, _IMG_H / h, 1.0)
    img.width = int(w * scale)
    img.height = int(h * scale)
    return img, img.height



def _px_to_points(px: float) -> float:
    """Excel row height is in points; 1px ≈ 0.75pt at 96 DPI."""
    return px * 0.75



# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def _write_summary(wb: Workbook, ctx: Dict) -> None:
    ws = _reset_sheet(wb, "Summary")
    ws.sheet_view.showGridLines = False
    ws["A1"] = f"Tableau → Power BI Migration Validation — {ctx['modelName']}"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:F1")

    meta = [
        ("Model", ctx["modelName"]),
        ("Workbook", ctx.get("workbook", "—")),
        ("Iteration", ctx["iteration"]),
        ("Validated at", ctx["timestamp"]),
        ("Overall status", ctx["overallStatus"]),
    ]
    r = 3
    for k, v in meta:
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r, column=2, value=str(v)).alignment = _WRAP
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Validation area").font = _SUB_FONT
    r += 1
    _header_row(ws, r, ["Area", "Total", "Passed", "Needs review", "Pass rate"],
                [28, 12, 12, 14, 12])
    r += 1
    for area, total, passed in ctx["areaStats"]:
        rate = f"{(passed / total * 100):.0f}%" if total else "—"
        _data_row(ws, r, [area, total, passed, total - passed, rate])
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Iteration control").font = _SUB_FONT
    r += 1
    for line in ctx["iterationNotes"]:
        ws.cell(row=r, column=1, value=line).alignment = _WRAP
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        r += 1

    if ctx.get("repeatedIssues"):
        r += 1
        ws.cell(row=r, column=1, value="Repeated unresolved issues (early stop)").font = _SUB_FONT
        r += 1
        _header_row(ws, r, ["Subject", "Status", "Root cause",
                            "Why no auto-fix", "Recommended manual action"],
                    [26, 16, 40, 36, 44])
        r += 1
        for it in ctx["repeatedIssues"]:
            d = it["diagnosis"]
            _data_row(ws, r, [it["subject"], it["status"], d["rootCause"],
                              d["whyNoAutoFix"], d["manualAction"]], status_col=2)
            ws.row_dimensions[r].height = 60
            r += 1


# --------------------------------------------------------------------------- #
# Visual Mapping
# --------------------------------------------------------------------------- #
def _write_visuals(wb: Workbook, records: List[Dict], out_dir: str) -> None:
    ws = _reset_sheet(wb, "Visual Mapping")
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Strict Tableau → Power BI Visual Mapping (one-to-one)"
    ws["A1"].font = _SUB_FONT
    ws.merge_cells("A1:K1")
    ws["A2"] = ("⬇ Side-by-side Tableau vs Power BI visual screenshots are embedded "
                "below this table — scroll down to the 'Visual Validation Evidence' "
                "section.")
    ws["A2"].font = Font(bold=True, italic=True, color="C0504D")
    ws.merge_cells("A2:K2")
    headers = ["Tableau Worksheet", "Tableau Visual", "Power BI Visual", "Page",
               "Expected Type", "Actual Type", "Aggregation", "Axes",
               "Legend", "Match Status", "Observations / Justification"]
    widths = [24, 20, 20, 14, 18, 18, 24, 28, 16, 16, 44]
    _header_row(ws, 3, headers, widths)
    r = 4
    for rec in records:
        _data_row(ws, r, [
            rec["tableauWorksheet"], rec["tableauVisual"], rec["powerBiVisual"],
            rec["powerBiPage"], rec["expectedType"], rec["actualType"],
            rec["aggregation"], rec["axes"], rec["legend"],
            rec["matchStatus"],
            rec["justification"] or rec["observations"],
        ], status_col=10)
        ws.row_dimensions[r].height = 42
        r += 1
    ws.freeze_panes = "A4"

    # ---- visual validation evidence (embedded side-by-side screenshots) ----- #
    r += 1
    title = ws.cell(row=r, column=1,
                    value="Visual Validation Evidence — Tableau vs Power BI "
                          "(real Tableau thumbnail vs Power BI Desktop capture)")
    title.font = _SUB_FONT
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    r += 2
    for rec in records:
        key = rec.get("screenshotKey")
        if not key:
            continue
        tab_img, pbi_img = SS.find_pair(out_dir, key)
        if not (tab_img or pbi_img):
            continue  # nothing to show for controls/extras with no render
        r = _embed_pair(
            ws, r, out_dir, key,
            heading=(f"{rec['tableauWorksheet']} → {rec['powerBiVisual']}   "
                     f"[{rec['matchStatus']}]"),
            note=(f"Expected: {rec['expectedType']}   |   Actual: "
                  f"{rec['actualType']}   |   {rec['axes']}"))


def _embed_pair(ws, r: int, out_dir: str, key: str, heading: str,
                note: str) -> int:
    """Embed one side-by-side Tableau/Power BI image pair; return the next row."""
    hc = ws.cell(row=r, column=1, value=heading)
    hc.font = Font(bold=True, color="FFFFFF")
    hc.fill = _HDR_FILL
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    r += 1
    nc = ws.cell(row=r, column=1, value=note)
    nc.alignment = _WRAP
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    r += 1
    lbl_t = ws.cell(row=r, column=1, value="Tableau")
    lbl_p = ws.cell(row=r, column=4, value="Power BI")
    for lc in (lbl_t, lbl_p):
        lc.font = Font(bold=True)
        lc.alignment = _CENTER
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=6)
    r += 1
    tab_img, pbi_img = SS.find_pair(out_dir, key)
    exp_t, exp_p = SS.expected_names(key)
    img_h = 0
    placed_t = False
    if tab_img:
        sc, h = _scaled_image(tab_img)
        if sc:
            ws.add_image(sc, f"A{r}")
            img_h = max(img_h, h)
            placed_t = True
    if not placed_t:
        _placeholder(ws, r, 1, "Tableau", exp_t)
    placed_p = False
    if pbi_img:
        sc, h = _scaled_image(pbi_img)
        if sc:
            ws.add_image(sc, f"D{r}")
            img_h = max(img_h, h)
            placed_p = True
    if not placed_p:
        _placeholder(ws, r, 4, "Power BI", exp_p)
    # Make the image row tall enough to fully contain the (floating) images so
    # the next pair's heading sits below them instead of overlapping/cutting.
    ws.row_dimensions[r].height = _px_to_points(max(img_h, 90) + 16)
    return r + 2  # image row + one spacer row




# --------------------------------------------------------------------------- #
# Measure Validation
# --------------------------------------------------------------------------- #
def _write_measures(wb: Workbook, records: List[Dict]) -> None:
    ws = _reset_sheet(wb, "Measure Validation")
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Measure Validation — Tableau calculation vs generated DAX"
    ws["A1"].font = _SUB_FONT
    ws.merge_cells("A1:K1")
    headers = ["Tableau Field", "Tableau Formula (expected)", "DAX Measure (actual)",
               "Aggregation", "Null Handling", "Conditional", "Time Intelligence",
               "Parameter Dependency", "Match Status", "Observations"]
    widths = [22, 40, 40, 16, 16, 14, 16, 20, 18, 40]
    _header_row(ws, 3, headers, widths)
    r = 4
    for rec in records:
        _data_row(ws, r, [
            rec["tableauField"], rec["tableauFormula"], rec["daxMeasure"],
            rec["aggregation"], rec["nullHandling"], rec["conditional"],
            rec["timeIntelligence"], rec["parameterDependency"],
            rec["matchStatus"], rec["observations"],
        ], status_col=9)
        ws.row_dimensions[r].height = 54
        r += 1
    ws.freeze_panes = "A4"


# --------------------------------------------------------------------------- #
# Filter Validation (with embedded side-by-side screenshots)
# --------------------------------------------------------------------------- #
def _placeholder(ws, row: int, col: int, label: str, fname: str) -> None:
    c = ws.cell(row=row, column=col,
                value=f"[{label} screenshot expected]\nDrop file:\n{fname}")
    c.alignment = _CENTER
    c.font = Font(italic=True, color="808080", size=9)
    c.fill = PatternFill("solid", fgColor="F2F2F2")
    c.border = _BORDER


def _write_filters(wb: Workbook, records: List[Dict], out_dir: str) -> None:
    ws = _reset_sheet(wb, "Filter Validation")
    ws.sheet_view.showGridLines = False
    ws["A1"] = ("Filter / Slicer / Parameter Validation — side-by-side "
                "screenshot evidence")
    ws["A1"].font = _SUB_FONT
    ws.merge_cells("A1:F1")
    ws["A2"] = ("⬇ Each filter below shows its Tableau control next to the matching "
                "Power BI slicer/visual screenshot.")
    ws["A2"].font = Font(bold=True, italic=True, color="C0504D")
    ws.merge_cells("A2:F2")
    # column geometry tuned so two ~380px images sit side by side
    widths = [30, 30, 30, 30, 30, 30]
    for col, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = w

    r = 3
    for rec in records:
        # metadata header band -------------------------------------------------
        title = f"{rec['kind']}: {rec['field']}"
        if rec.get("worksheet") and rec["worksheet"] != "—":
            title += f"  (worksheet: {rec['worksheet']})"
        hc = ws.cell(row=r, column=1, value=title)
        hc.font = Font(bold=True, color="FFFFFF")
        hc.fill = _HDR_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        r += 1

        _header_row(ws, r, ["Type", "Scope", "Filter Values", "Expected",
                            "Actual", "Match Status"], widths)
        r += 1
        _data_row(ws, r, [rec["filterType"], rec["scope"], rec["filterValues"],
                          rec["expected"], rec["actual"], rec["matchStatus"]],
                  status_col=6)
        ws.row_dimensions[r].height = 42
        r += 1

        # cross-filter / observations band ------------------------------------
        oc = ws.cell(row=r, column=1,
                     value=f"Cross-filter: {rec.get('crossFilter', '—')}   |   "
                           f"Observation: {rec.get('observations', '')}")
        oc.alignment = _WRAP
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        r += 1

        # side-by-side screenshot evidence ------------------------------------
        lbl_t = ws.cell(row=r, column=1, value="Tableau")
        lbl_p = ws.cell(row=r, column=4, value="Power BI")
        for lc in (lbl_t, lbl_p):
            lc.font = Font(bold=True)
            lc.alignment = _CENTER
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=6)
        r += 1

        tab_img, pbi_img = SS.find_pair(out_dir, rec["screenshotKey"])
        exp_t, exp_p = SS.expected_names(rec["screenshotKey"])
        img_row = r
        img_h = 0
        placed = False
        if tab_img:
            sc, h = _scaled_image(tab_img)
            if sc:
                ws.add_image(sc, f"A{img_row}")
                img_h = max(img_h, h)
                placed = True
        if not (tab_img and placed):
            _placeholder(ws, img_row, 1, "Tableau", exp_t)
        if pbi_img:
            sc, h = _scaled_image(pbi_img)
            if sc:
                ws.add_image(sc, f"D{img_row}")
                img_h = max(img_h, h)
        else:
            _placeholder(ws, img_row, 4, "Power BI", exp_p)
        ws.row_dimensions[img_row].height = _px_to_points(max(img_h, 90) + 16)
        r += 2  # image row + spacer



# --------------------------------------------------------------------------- #
# Iterations (append-only history)
# --------------------------------------------------------------------------- #
def _write_iterations(wb: Workbook, history: List[Dict]) -> None:
    if "Iterations" in wb.sheetnames:
        del wb["Iterations"]
    ws = wb.create_sheet("Iterations")
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Validation Iterations — full lifecycle history"
    ws["A1"].font = _SUB_FONT
    ws.merge_cells("A1:K1")
    headers = ["Iteration", "Timestamp", "Visuals (pass/total)",
               "Measures (pass/total)", "Filters (pass/total)",
               "Unresolved", "Stopped Early", "Status"]
    widths = [10, 22, 20, 20, 20, 12, 14, 34]
    _header_row(ws, 3, headers, widths)
    r = 4
    for it in history:
        _data_row(ws, r, [
            it["iteration"], it["timestamp"],
            f"{it['visualsPassed']}/{it['visualsTotal']}",
            f"{it['measuresPassed']}/{it['measuresTotal']}",
            f"{it['filtersPassed']}/{it['filtersTotal']}",
            it["unresolvedCount"],
            "Yes" if it.get("stoppedEarly") else "No",
            it["status"],
        ], status_col=8)
        r += 1
    ws.freeze_panes = "A4"


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #
def workbook_path(out_dir: str, model_name: str) -> str:
    return os.path.join(out_dir, "validation", f"{model_name}_Validation.xlsx")


def build_or_update(out_dir: str, ctx: Dict, visuals: List[Dict],
                    measures: List[Dict], filters: List[Dict],
                    history: List[Dict]) -> str:
    """Create the workbook on iteration 1; update it in place afterwards."""
    path = workbook_path(out_dir, ctx["modelName"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        wb = load_workbook(path)
        # drop the default empty sheet only ever present on a fresh book
    else:
        wb = Workbook()
        wb.remove(wb.active)

    _write_summary(wb, ctx)
    _write_visuals(wb, visuals, out_dir)
    _write_measures(wb, measures)
    _write_filters(wb, filters, out_dir)
    _write_iterations(wb, history)

    # order the sheets predictably
    order = ["Summary", "Visual Mapping", "Measure Validation",
             "Filter Validation", "Iterations"]
    wb._sheets.sort(key=lambda s: order.index(s.title)
                    if s.title in order else len(order))
    try:
        wb.save(path)
    except PermissionError:
        # the workbook is open in Excel — write a timestamped copy instead of
        # failing the whole validation, and tell the caller where it landed.
        import datetime as _dt
        alt = path.replace(
            ".xlsx", f"_{_dt.datetime.now():%Y%m%d_%H%M%S}.xlsx")
        wb.save(alt)
        print(f"  NOTE: {os.path.basename(path)} is open/locked; wrote "
              f"{os.path.basename(alt)} instead. Close Excel to update in place.")
        return alt
    return path
