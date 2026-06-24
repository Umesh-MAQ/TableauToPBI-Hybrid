"""emit_tmdl.py — Stage 10 deterministic TMDL semantic-model generator.

Consumes analysis.json (IR) + decisions.json and writes a complete
{Model}.SemanticModel/ folder plus the {Model}.pbip root and .platform file. All
TMDL boilerplate is emitted here; the LLM only supplies measure DAX via decisions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tmdl_blocks as B  # noqa: E402
import date_levels as D  # noqa: E402
import field_param as FP  # noqa: E402

_TWB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "twb")
sys.path.insert(0, os.path.normpath(_TWB_DIR))
import csv_probe as CP  # noqa: E402

# Repo root (…/scripts/emit/emit_tmdl.py -> two levels up) used to resolve CSVs
# against the local workspace, independent of any path baked into decisions.json.
_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

PBISM = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "version": "4.2", "settings": {},
}
PLATFORM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"


def platform_file(item_type: str, name: str, seed: str) -> str:
    """Build a Fabric .platform file (deterministic GUID-shaped logicalId)."""
    import hashlib
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    lid = f"{h[:8]}-{h[8:12]}-4{h[13:16]}-9{h[17:20]}-{h[20:32]}"
    return json.dumps({
        "$schema": PLATFORM_SCHEMA,
        "metadata": {"type": item_type, "displayName": name},
        "config": {"version": "2.0", "logicalId": lid},
    }, indent=2)


def pbip_root(model_name: str) -> str:
    """Build the {Model}.pbip root file (report artifact only)."""
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{model_name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2)

def load_json(path: str) -> Dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _norm_name(name: str) -> str:
    """Case/punctuation-insensitive measure-name key for de-duplication."""
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


# --- Tableau number-format -> Power BI formatString -------------------------
# A measure whose DAX is exactly one aggregation over one column, e.g.
# ``SUM ( loan[loan_amount] )`` — used to look up that column's Tableau format.
_SINGLE_AGG_MEASURE_RE = re.compile(
    r"^\s*[A-Za-z.]+\s*\(\s*[\w']*\[([^\]]+)\]\s*\)\s*$")


# Trailing Tableau scale letter -> the divisor it represents. Power BI does NOT
# reliably honour the "scale by 1000" trailing-comma notation when a literal
# suffix follows it (e.g. ``#,##0,,"M"`` renders the full unscaled number + "M");
# the supported, deterministic mechanism is the visual's Display units. So the
# model formatString carries only currency + two-decimal precision, and the scale
# (K/M/B/T) is applied via ``labelDisplayUnits`` on each visual instead.
_SCALE_LETTER = {"K": 1000, "M": 1000000, "B": 1000000000, "T": 1000000000000}


def _tableau_to_pbi_format(tab):
    """Convert a Tableau number-format code to a clean Power BI formatString.

    The returned format string preserves the meaningful display intent — currency
    symbol, thousands grouping, and a fixed **two-decimal** precision — but never
    bakes in the thousand/million scale (trailing ``,``) or the K/M/B/T suffix.
    Power BI applies the scale + suffix through the visual's Display units (see
    ``_tableau_scale_divisor`` / ``measure_display_units``); baking it into the
    format string is unreliable (a literal suffix disables the comma scaling, so
    the full unscaled value leaks through). The negative section (after ``;``) is
    dropped. Returns None when there is nothing usable to convert.
    """
    if not tab:
        return None
    s = tab.split(";", 1)[0].strip()
    if not s:
        return None
    # Percent — always two-decimal precision (e.g. 13.08%).
    if s.startswith("p") or s.endswith("%"):
        return "0.00%"
    # Drop the Tableau type marker (c/n). A currency symbol is shown only when the
    # format string explicitly carries one (e.g. c"$"…); a bare ``c`` prefix does
    # not, so never inject a "$" of our own.
    if s[:1] in ("c", "n"):
        s = s[1:]
    has_dollar = ('"$"' in s) or s.lstrip().startswith("$")
    s = s.replace('"$"', "").replace("$", "")
    # Strip a trailing scale letter (its scaling moves to Display units).
    s = re.sub(r'"?[KMBT]"?\s*$', "", s).strip()
    # Keep only the integer grouping pattern (e.g. #,##0); discard the scaling
    # commas and original decimals, then pin two-decimal precision.
    m = re.match(r"[#,]*0", s)
    core = m.group(0) if m else "#,##0"
    # TMDL treats a value starting with a double quote as a fully quoted string, so
    # emit a leading "$" as backslash-escaped \$ (Power BI renders it identically).
    return ("\\$" if has_dollar else "") + core + ".00"


def _tableau_scale_divisor(tab):
    """Display-units divisor implied by a Tableau number format.

    Each trailing thousands separator on the integer part divides by 1000, so
    ``c"$"#,##0,,M`` -> 1_000_000 (Millions) and ``c#,##0,K`` -> 1000 (Thousands).
    A lone scale letter with no commas falls back to the letter's magnitude.
    Percent and unscaled formats return 1 (Display units = None)."""
    if not tab:
        return 1
    s = tab.split(";", 1)[0].strip()
    if not s or s.startswith("p") or s.endswith("%"):
        return 1
    if s[:1] in ("c", "n"):
        s = s[1:]
    s = s.replace('"$"', "").replace("$", "").strip()
    letter = None
    mlet = re.search(r'"?([KMBT])"?\s*$', s)
    if mlet:
        letter = mlet.group(1)
        s = s[:mlet.start()].rstrip()
    mc = re.search(r"0(,+)(?:\.\d*)?\s*$", s)
    if mc:
        return 1000 ** len(mc.group(1))
    return _SCALE_LETTER.get(letter, 1)


def _format_indices(ir: Dict):
    """Index Tableau field formats from worksheet measure pills + measure tooltip
    fields. Returns ``(by_ws, by_field, by_col)`` where ``by_ws`` maps a worksheet
    name to its primary measure pill's format (the most authoritative match for a
    same-named measure, e.g. the ``Total Funded Amount`` card's currency format).
    First non-null format wins for the field/column indices."""
    by_ws: Dict[str, str] = {}
    by_field: Dict[str, str] = {}
    by_col: Dict[str, str] = {}
    for ws in ir.get("worksheets", []):
        pills = list(ws.get("measures") or [])
        pills += [t for t in (ws.get("tooltipFields") or []) if t.get("isMeasure")]
        wsname = ws.get("name")
        if wsname and pills:
            primary = _tableau_to_pbi_format((ws.get("measures") or [{}])[0].get("format")
                                             if ws.get("measures") else None)
            if primary:
                by_ws.setdefault(wsname, primary)
        for p in pills:
            fmt = _tableau_to_pbi_format(p.get("format"))
            if not fmt:
                continue
            field = p.get("field")
            col = p.get("column") or p.get("field")
            if field:
                by_field.setdefault(field, fmt)
            if col:
                by_col.setdefault(col, fmt)
    return by_ws, by_field, by_col


def _apply_tableau_formats(measures: List[Dict], cols: List[Dict], ir: Dict) -> None:
    """Override measure/column formatStrings with the faithful Tableau number
    format captured in the IR (percent / currency / scaled units), in place.

    Measures: matched by same-named worksheet (most authoritative), then by field
    name, then by the single column of a one-aggregation DAX (so
    ``Total Funded Amount`` = ``SUM(loan[loan_amount])`` picks up its currency
    format). Complex DAX (ratios, RANKX, …) without a name match is left alone so a
    nested column reference can never mis-format the measure.

    Columns: only a percent format is applied, since that is what drives an
    inline-aggregation card (e.g. ``AVG(int_rate)`` shown as ``13.08%``); other raw
    columns are left to their aggregating measures so they are not over-formatted.
    """
    by_ws, by_field, by_col = _format_indices(ir)
    for m in measures:
        fmt = by_ws.get(m.get("name")) or by_field.get(m.get("name"))
        if not fmt:
            mm = _SINGLE_AGG_MEASURE_RE.match(m.get("dax", "") or "")
            if mm:
                fmt = by_col.get(mm.group(1))
        if fmt:
            m["formatString"] = fmt
    for c in cols:
        if c.get("dataType") not in ("integer", "real"):
            continue
        pf = by_col.get(c.get("name"))
        if pf and pf.endswith("%"):
            c["format"] = pf


def _raw_format_indices(ir: Dict):
    """Like ``_format_indices`` but keeps the *raw* Tableau format codes (not the
    converted Power BI format strings), so the scale divisor can be derived from
    the trailing thousands separators. Returns ``(by_ws, by_field, by_col)``."""
    by_ws: Dict[str, str] = {}
    by_field: Dict[str, str] = {}
    by_col: Dict[str, str] = {}
    for ws in ir.get("worksheets", []):
        pills = list(ws.get("measures") or [])
        pills += [t for t in (ws.get("tooltipFields") or []) if t.get("isMeasure")]
        wsname = ws.get("name")
        if wsname and ws.get("measures"):
            praw = (ws["measures"][0] or {}).get("format")
            if praw:
                by_ws.setdefault(wsname, praw)
        for p in pills:
            raw = p.get("format")
            if not raw:
                continue
            field = p.get("field")
            col = p.get("column") or p.get("field")
            if field:
                by_field.setdefault(field, raw)
            if col:
                by_col.setdefault(col, raw)
    return by_ws, by_field, by_col


def measure_display_units(measures: List[Dict], ir: Dict) -> Dict[str, int]:
    """Map each measure name to its Power BI Display-units divisor (1 / 1000 /
    1_000_000 / …), derived from the same authoritative Tableau format the model
    formatString came from. The report emitter sets ``labelDisplayUnits`` to this
    value on every visual that plots the measure, so a measure whose Tableau format
    scaled by millions (e.g. ``Total Funded Amount``) renders as ``$4,166.07M``
    instead of the full unscaled number — without relying on the unreliable
    trailing-comma scaling in the format string."""
    by_ws, by_field, by_col = _raw_format_indices(ir)
    out: Dict[str, int] = {}
    for m in measures:
        tab = by_ws.get(m.get("name")) or by_field.get(m.get("name"))
        if not tab:
            mm = _SINGLE_AGG_MEASURE_RE.match(m.get("dax", "") or "")
            if mm:
                tab = by_col.get(mm.group(1))
        out[m.get("name")] = _tableau_scale_divisor(tab) if tab else 1
    return out


def _repoint_dax_table(dax: str, old: str, new: str) -> str:
    """Rewrite the table qualifier inside a DAX expression from ``old`` to ``new``.

    Deterministic measures in ``dax-partial.json`` are emitted against the single-
    flat placeholder table token (the workbook/model name). When the agent designs
    a star schema, the real fact table gets a different name (e.g. ``loan``). The
    measure's *host* table is re-pointed at merge time, but the column references
    inside the DAX (``Model[col]``) would still target a table that does not exist
    -> the measure errors in Power BI. This rewrites ``Old[`` and ``'Old'[``
    qualifiers to the resolved host table so the DAX resolves.
    """
    if not dax or not old or old == new:
        return dax
    new_tok = new if re.fullmatch(r"\w+", new) else f"'{new}'"
    # Quoted form first: 'Old Name'[  ->  new_tok[
    dax = re.sub(rf"'{re.escape(old)}'\s*\[", f"{new_tok}[", dax)
    # Unquoted form: Old[  ->  new_tok[  (word-bounded, not already quoted)
    dax = re.sub(rf"(?<![\w'])({re.escape(old)})\s*\[", f"{new_tok}[", dax)
    return dax


def merge_partial_measures(decisions: Dict, analysis_path: str) -> Dict:
    """Defense-in-depth: fold deterministically-translated measures from
    dax-partial.json into ``decisions['measures']`` so they are emitted even if
    the agent omitted them. Agent-authored measures always win on a name clash.
    """
    partial_path = os.path.join(
        os.path.dirname(os.path.abspath(analysis_path)), "dax-partial.json")
    if not os.path.isfile(partial_path):
        return decisions
    try:
        det = load_json(partial_path).get("measures", [])
    except (OSError, json.JSONDecodeError):
        return decisions
    if not det:
        return decisions
    measures = decisions.setdefault("measures", [])
    existing = {_norm_name(m.get("name", "")) for m in measures}
    table_names = {t.get("name") for t in decisions.get("tables", [])}
    fact = next((t["name"] for t in decisions.get("tables", [])
                 if t.get("role") == "fact"), None)
    added = 0
    for m in det:
        key = _norm_name(m.get("name", ""))
        if not key or key in existing:
            continue  # agent already authored this measure -> keep theirs
        home = m.get("table") if m.get("table") in table_names else (fact or m.get("table"))
        if home not in table_names:
            continue  # no valid host table -> let reconciliation flag it instead
        # The deterministic DAX is written against the placeholder table token
        # (m['table']). When re-homed onto a differently-named fact table, rewrite
        # the qualifier inside the DAX so column refs resolve to the real table.
        dax = _repoint_dax_table(m["dax"], m.get("table", ""), home)
        measures.append({
            "table": home,
            "name": m["name"],
            "dax": dax,
            "formatString": m.get("formatString"),
            "displayFolder": m.get("displayFolder", "Base Measures"),
            "description": m.get("description"),
            "source": "deterministic",
        })
        existing.add(key)
        added += 1
    if added:
        print(f"  merged {added} deterministic measure(s) from dax-partial.json")
    return decisions


def reassign_orphan_measures(decisions: Dict) -> Dict:
    """Guarantee every measure lands on an emitted table (no silent drops).

    ``build_table_file`` writes a measure into a table only when ``m["table"]``
    EXACTLY equals that table's name. A measure whose ``table`` does not match any
    emitted table — wrong case/spacing, a stale or guessed name, or a multi-
    datasource naming mismatch (e.g. Midnight Census exposes both
    "Midnight Census NEW" and "Midnight_Census_Template") — would otherwise be
    dropped from the model with no error. This normalizes each measure's ``table``
    to the matching emitted table, and routes any still-unmatched measure to the
    fact table (else the first table) with a warning, so a measure can never
    vanish without a trace.
    """
    tables = decisions.get("tables", [])
    measures = decisions.get("measures", [])
    if not measures:
        return decisions
    # Valid hosts: regular tables + calculated tables (real tables that can carry
    # measures). Field-parameter tables are intentionally excluded as hosts.
    host_names = [t["name"] for t in tables]
    host_names += [ct["name"] for ct in decisions.get("calculatedTables", [])]
    if not host_names:
        raise ValueError(
            "decisions.json defines measures but no tables to host them; "
            "cannot emit measures.")
    by_norm = {_norm_name(n): n for n in host_names}
    fact = next((t["name"] for t in tables if t.get("role") == "fact"), None)
    fallback = fact or host_names[0]
    reassigned = 0
    for m in measures:
        want = m.get("table", "")
        if want in host_names:
            continue  # already an exact, valid host
        match = by_norm.get(_norm_name(want))
        if match:
            m["table"] = match  # fix case/spacing/punctuation mismatch
            continue
        print(f"  WARNING: measure '{m.get('name')}' targets unknown table "
              f"'{want}' -> routed to '{fallback}'")
        m["table"] = fallback
        reassigned += 1
    if reassigned:
        print(f"  reassigned {reassigned} orphan measure(s) to '{fallback}'")
    return decisions


def model_dir(analysis_path: str, model_name: str) -> str:
    base = os.path.dirname(os.path.abspath(analysis_path))
    path = os.path.join(base, f"{model_name}.SemanticModel")
    os.makedirs(os.path.join(path, "definition", "tables"), exist_ok=True)
    return path


def write(path: str, text: str) -> None:
    # Always write UTF-8 WITHOUT BOM — Power BI Desktop rejects BOM in TMDL/PBIR files.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def build_table_file(table: Dict, ir: Dict, decisions: Dict, seq: int) -> str:
    """Assemble one table TMDL file: header, measures, columns, partition."""
    name = table["name"]
    lines = [f"table {B.quote(name)}", f"{B.TAB}lineageTag: {B.lineage(seq)}"]
    if table.get("role") == "date":
        lines.append(f"{B.TAB}dataCategory: Time")
    lines.append("")
    measures = [m for m in decisions.get("measures", []) if m["table"] == name]
    cols = _columns_for(table, ir, decisions)
    _apply_tableau_formats(measures, cols, ir)
    col_names = {c["name"].lower() for c in cols}
    clash = sorted(m["name"] for m in measures if m["name"].lower() in col_names)
    if clash:
        raise ValueError(f"table '{name}': measure name(s) collide with columns: {clash}")
    for i, m in enumerate(measures):
        lines += [B.measure_block(m, seq * 100 + i + 1), ""]
    # Track every column name emitted on this table (case-insensitive) so no two
    # sources ever declare the same column. Power BI's TMDL loader hard-fails with
    # "objects cannot be merged because both declare the same property: expression"
    # when a calculated column name is duplicated (e.g. an agent time-intelligence
    # helper 'Order Date (Year)' AND the derived date-part column of the same name).
    # Seed with measure names too: Power BI also hard-fails with "the '<name>'
    # measure cannot be created because a column with the same name already exists"
    # when an auto-generated date-part / calendar / calc column duplicates an
    # explicit measure name. Explicit measures win; the redundant auto column is
    # suppressed.
    seen: set = {m["name"].lower() for m in measures}
    for i, col in enumerate(cols):
        lines += [B.column_block(col, seq * 1000 + i + 1), ""]
        seen.add(col["name"].lower())
    # Author-supplied calculated columns (decisions.calculatedColumns) for this
    # table — e.g. a Peak/Normal flag used as a chart legend.
    calc_cols = [c for c in decisions.get("calculatedColumns", [])
                 if c.get("table") == name]
    for j, c in enumerate(calc_cols):
        if c["name"].lower() in seen:
            continue
        lines += [B.calc_column_block(c["name"], c["dax"],
                                      c.get("dataType", "string"),
                                      c.get("formatString"), seq * 1000 + 700 + j), ""]
        seen.add(c["name"].lower())
    # Calendar date table: Date key column + date intelligence calculated columns
    if table.get("role") == "date" and table.get("sourceType") == "calendar":
        lines += [B.date_key_column_block(seq * 1000 + 1), ""]
        for j, (col_name, dax, dtype, fmt) in enumerate(_CALENDAR_CALC_COLS):
            if col_name.lower() in seen:
                continue
            lines += [B.calc_column_block(col_name, dax, dtype, fmt, seq * 1000 + 10 + j), ""]
            seen.add(col_name.lower())
    for j, part in enumerate(_date_part_columns(table, cols, ir)):
        if part["name"].lower() in seen:
            continue
        dax = D.part_dax(part["baseColumn"], name, part["level"])
        lines += [B.calc_column_block(part["name"], dax, part["dataType"],
                                      part["format"], seq * 1000 + 500 + j), ""]
        seen.add(part["name"].lower())
    # Tableau drill-path hierarchies whose levels all resolve to columns on THIS
    # table -> emit a Power BI model hierarchy. A hierarchy whose levels span
    # several tables (no single owner) is skipped here rather than emitted with a
    # dangling column reference that would break Power BI Desktop.
    emitted_col_names = [c["name"] for c in cols] + [c["name"] for c in calc_cols]
    for k, hb in enumerate(_hierarchies_for_table(emitted_col_names, ir, seq * 1000 + 800)):
        lines += [hb, ""]
    lines.append(_partition_for(table, ir, cols, decisions))
    return "\n".join(lines)


def _norm_ident(text: str) -> str:
    """Normalise a column/level identifier for tolerant matching."""
    return re.sub(r"[\s_/\-]", "", text or "").lower()


def _hierarchies_for_table(col_names: List[str], ir: Dict, seq: int) -> List[str]:
    """Return TMDL hierarchy blocks for IR drill-paths owned by this table.

    A hierarchy is "owned" by a table only when EVERY level resolves to a real
    column on that table (matched case-insensitively, tolerant of space/_/-/​/
    differences between the Tableau caption and the emitted column name). Levels
    are mapped back to the actual emitted column name so ``column:`` is always
    valid. Hierarchies whose levels do not all resolve here are left for whichever
    table owns them — or skipped entirely if no single table owns them all.
    """
    by_norm = {_norm_ident(c): c for c in col_names}
    blocks: List[str] = []
    for h, hier in enumerate(ir.get("hierarchies", []) or []):
        levels = hier.get("levels") or []
        if len(levels) < 2:
            continue
        resolved: List[tuple] = []
        for lvl in levels:
            col = by_norm.get(_norm_ident(lvl))
            if col is None:
                resolved = []
                break
            resolved.append((lvl, col))
        if resolved:
            blocks.append(B.hierarchy_block(hier.get("name") or "Hierarchy",
                                            resolved, seq + h * 32))
    return blocks



def _date_part_columns(table: Dict, cols: List[Dict], ir: Dict) -> List[Dict]:
    """Date-part derived columns this table must expose (month/year/etc.)."""
    if table.get("role") != "fact":
        return []
    date_cols = {c["name"] for c in cols if c["dataType"] in ("date", "datetime")}
    if not date_cols:
        return []
    return D.needed_parts(ir.get("worksheets", []), date_cols)


# Calculated columns added to every CALENDAR() date dimension table.
_CALENDAR_CALC_COLS = [
    ("Year",         "YEAR([Date])",                        "integer", "0"),
    ("Month Number", "MONTH([Date])",                       "integer", "0"),
    ("Month",        'FORMAT([Date], "MMMM")',               "string",  None),
    ("Quarter",      '"Q" & FORMAT(QUARTER([Date]), "0")',   "string",  None),
    ("Year-Month",   'FORMAT([Date], "YYYY-MM")',            "string",  None),
]


def _dim_source_column(table_name: str, key_col: str, decisions: Dict) -> str:
    """Find the fact-side column that maps to this dim's key via relationships."""
    for rel in decisions.get("relationships", []):
        to_t, to_c = rel["toColumn"].split(".", 1)
        if to_t == table_name and to_c == key_col:
            _, from_c = rel["fromColumn"].split(".", 1)
            return from_c
    return key_col  # fallback: same name as key


def _date_source_column(table_name: str, decisions: Dict):
    """Return (fact_table, date_col) for a DimDate calendar table via relationships."""
    for rel in decisions.get("relationships", []):
        to_t, _to_c = rel["toColumn"].split(".", 1)
        if to_t == table_name:
            from_t, from_c = rel["fromColumn"].split(".", 1)
            return from_t, from_c
    tables = decisions.get("tables", [])
    fact = next((t["name"] for t in tables if t.get("role") == "fact"), "Table")
    return fact, "date_added"


def _columns_for(table: Dict, ir: Dict, decisions: Dict) -> List[Dict]:
    role = table.get("role")
    # Dim with dedupKey: load ALL CSV columns so attributes are available.
    # Fall back to key-only when CSV cannot be probed.
    if role == "dim" and table.get("dedupKey"):
        key = (table.get("keyColumns") or [table["dedupKey"]])[0]
        key = _logical_col(table, ir, key)
        probe = _probe_for_table(table, ir)
        if probe and probe["columns"]:
            return probe["columns"]
        return [{"name": key, "dataType": "string", "role": "dimension", "format": None}]
    # Calendar date table: no regular columns (Date key + calc columns added separately)
    if role == "date" and table.get("sourceType") == "calendar":
        return []
    if role == "param" and table.get("datatable"):
        return [{"name": c["name"], "dataType": c.get("dataType", "string"),
                 "role": "dimension", "format": None}
                for c in table["datatable"]["columns"]]
    ds = table.get("sourceDatasource")
    cols = [c for c in ir["columns"] if ds is None or c["datasource"] == ds]
    cols = cols or ir["columns"]
    ds = table.get("sourceDatasource")
    cols = [c for c in ir["columns"] if ds is None or c["datasource"] == ds]
    cols = cols or ir["columns"]
    # A workbook can expose several datasources that share column names (e.g. a
    # secondary "... NEW" extract alongside the CSV). Prefer the datasource whose
    # name matches this table so its columns win before we deduplicate. [from main]
    if ds is None:
        preferred = table.get("name")
        if any(c.get("datasource") == preferred for c in cols):
            cols = ([c for c in cols if c.get("datasource") == preferred]
                    + [c for c in cols if c.get("datasource") != preferred])
    cols = _dedupe_columns(cols)
    # Anchor non-date tables on the physical CSV header: this ADDS physical
    # columns Tableau omitted, DROPS invented logical ones, and scopes a federated
    # fact to its own CSV (superset of main's probe fact-scoping). [from HEAD]
    if table.get("role") != "date":
        cols = _reconcile_with_csv(table, ir, cols)
    return cols


def _reconcile_with_csv(table: Dict, ir: Dict, ir_cols: List[Dict]) -> List[Dict]:
    """Rebuild the column list from the CSV's physical header.

    The Tableau metadata often omits physical columns and invents logical ones
    (calc fields, ':Measure Names') that are not in the file. Anchoring on the
    real header guarantees every emitted column maps to a column that actually
    exists, so the model load never fails with 'column X wasn't found'. IR types
    are kept for known columns; unknown columns get a light sampled inference.
    """
    path = table.get("sourceFile") or _first_csv(ir)
    if not str(path).lower().endswith(".csv"):
        return ir_cols
    abs_path = _abs_csv(ir, path)
    header, rows = B.read_csv_sample(abs_path)
    if not header:
        return ir_cols
    ir_by_key = {c["name"].strip().lower(): c for c in ir_cols}
    out, seen = [], set()
    for idx, raw_name in enumerate(header):
        key = raw_name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        match = ir_by_key.get(key)
        if match:
            col = dict(match)
            col["name"] = raw_name
        else:
            col = {"name": raw_name, "dataType": B.infer_csv_type(rows, idx),
                   "role": "dimension", "format": None}
        out.append(col)
    return out


def _dedupe_columns(cols: List[Dict]) -> List[Dict]:
    """Drop duplicate column names (case-insensitive), keeping first occurrence.

    A workbook can expose the same column from several datasources (e.g. a Hyper
    extract and its CSV template both define 'Census Date'). A single flat table
    must declare each column once or TMDL load fails with 'objects cannot be
    merged because both declare the same property: dataType'.
    """
    seen, out = set(), []
    for c in cols:
        key = c["name"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _parse_settings_for(table: Dict, ir: Dict) -> Dict:
    """Return the metadata CSV parse settings for a table (delimiter/codepage/...).

    Looks up ir.physicalTables by the table's source-file basename so the emitter
    can take the delimiter and encoding from WORKBOOK METADATA instead of sniffing
    the data file. Returns {} when not found (caller falls back to probing).
    """
    src = table.get("sourceFile") or _first_csv(ir)
    base = _normalize_name(os.path.basename(str(src)))
    pts = ir.get("physicalTables", [])
    for pt in pts:
        if _normalize_name(pt.get("name", "")) == base:
            return pt.get("parse") or {}
    if len(pts) == 1:
        return pts[0].get("parse") or {}
    return {}


def _partition_for(table: Dict, ir: Dict, cols: List[Dict], decisions: Dict) -> str:
    role = table.get("role")
    parse = _parse_settings_for(table, ir)
    codepage = parse.get("codepage") or 65001
    # Dim with dedupKey: full M partition — load all CSV columns, rename, dedup.
    if role == "dim" and table.get("dedupKey"):
        key = (table.get("keyColumns") or [table["dedupKey"]])[0]
        key = _logical_col(table, ir, key)
        path = _abs_csv(ir, table.get("sourceFile") or _first_csv(ir))
        probe = _probe_for_table(table, ir)
        # Delimiter from metadata first, then the file probe, then comma.
        delim = parse.get("delimiter") or (probe["delimiter"] if probe else ",")
        if probe and probe["columns"]:
            return B.dim_partition(table["name"], path, delim,
                                   key, probe["columns"], codepage)
        # Fallback: key-only slim partition when CSV cannot be probed
        src_col = _dim_source_column(table["name"], key, decisions)
        all_cols = [{"name": key, "csv_name": src_col, "dataType": "string"}]
        return B.dim_partition(table["name"], path, delim, key, all_cols, codepage)
    # Calendar date table: DAX CALENDAR calculated partition
    if role == "date" and table.get("sourceType") == "calendar":
        fact_tbl, date_col = _date_source_column(table["name"], decisions)
        return B.calendar_partition(table["name"], fact_tbl, date_col)
    if role == "param" and table.get("datatable"):
        dt = table["datatable"]
        return B.datatable_partition(table["name"], dt["columns"], dt["rows"])
    if table.get("mExpression"):
        return B.raw_partition(table["name"], table["mExpression"])
    # Non-CSV source (Excel / Parquet / relational DB): generate the matching M
    # connector from the detected datasource. CSV and Hyper fall through to the
    # CSV path below (Hyper is materialised from its CSV sibling in these books).
    src = _source_for_table(table, ir)
    if src and src["sourceType"].lower() in _NONCSV_SOURCES:
        return B.source_partition(table["name"], src, cols)
    # Fact / other: delimiter from metadata (fallback to probe); robust parsing
    # for European CSVs (non-comma delimiter or comma decimal separator).
    path = table.get("sourceFile") or _first_csv(ir)
    abs_path = _abs_csv(ir, path)
    probe = _probe_for_table(table, ir)
    delimiter = parse.get("delimiter") or (probe["delimiter"] if probe else ",")
    if probe and probe["columns"]:
        csv_names_norm = {_normalize_name(c["csv_name"]) for c in probe["columns"]}
        cols = [c for c in cols if _normalize_name(c["name"]) in csv_names_norm]
    european = delimiter != "," or parse.get("decimalChar") == ","
    if european:
        date_cs = [c["name"] for c in cols if c.get("dataType") in ("date", "datetime")]
        decimal_cs = [c["name"] for c in cols if c.get("dataType") == "real"]
        return B.robust_csv_partition(table["name"], abs_path, cols, delimiter,
                                      date_cs, decimal_cs, codepage)
    return B.csv_partition(table["name"], abs_path, cols, delimiter, codepage)


def _abs_csv(ir: Dict, path: str) -> str:
    """Resolve a CSV filename to an absolute path that exists on THIS machine.

    ``File.Contents`` needs an absolute path. A path baked into decisions.json may
    come from another machine, so an absolute path is only trusted when it exists
    locally; otherwise the CSV's basename is resolved against the local workspace
    (next to the workbook, then anywhere under the repo ``Data/`` tree).
    """
    # 1) Trust an absolute path only if it actually exists on this machine.
    if path and os.path.isabs(path) and os.path.isfile(path):
        return path

    base = os.path.basename(path) if path else ""
    wb = ir.get("workbook", {}).get("sourcePath", "")
    wb_dir = os.path.dirname(wb if os.path.isabs(wb) else os.path.join(_REPO_ROOT, wb))

    candidates = []
    if base:
        candidates.append(os.path.join(wb_dir, base))  # 2) next to the workbook
    data_root = os.path.join(_REPO_ROOT, "Data")
    if base and os.path.isdir(data_root):  # 3) search the workspace Data/ tree
        for root, _dirs, files in os.walk(data_root):
            if base in files:
                candidates.append(os.path.join(root, base))
                break
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)

    # 4) Fallbacks: keep prior behavior so a valid absolute path still works.
    if path and os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(wb_dir, base))


# Source types whose partitions are built by B.source_partition (non-CSV).
# CSV and Hyper deliberately excluded: CSV uses the physical-header path; Hyper
# is materialised from its CSV sibling. Custom/unmapped sources reach the ODBC
# stub inside source_partition, or use a decisions ``mExpression`` override.
_NONCSV_SOURCES = {
    "excel", "parquet", "sqlserver", "postgresql",
    "mysql", "oracle", "odbc",
    "snowflake", "databricks", "bigquery",
}


def _source_for_table(table: Dict, ir: Dict) -> Dict | None:
    """Resolve the connection facts for a table's backing datasource.

    Honours decisions overrides (sourceDatasource / sourceTable / sourceSchema /
    sourceSheet / sourceFile) and falls back to the first active non-Parameters
    datasource. Returns None when the IR has no usable datasource.
    """
    dss = [d for d in ir.get("dataSources", []) if d.get("internalName") != "Parameters"]
    if not dss:
        return None
    want = table.get("sourceDatasource")
    ds = None
    if want:
        ds = next((d for d in dss
                   if d.get("name") == want or d.get("internalName") == want), None)
    if ds is None:
        ds = next((d for d in dss if d.get("active")), None) or dss[0]
    file = table.get("sourceFile") or (ds.get("files") or [None])[0]
    tables = ds.get("tables") or []
    relations = ds.get("relations") or []
    table_rel = next((r for r in relations if r.get("type") == "table"), None)
    custom = next((r for r in relations if r.get("type") == "customSql"), None)
    return {
        "sourceType": ds.get("sourceType", "") or "",
        "server": ds.get("server"),
        "database": ds.get("database"),
        "schema": (table.get("sourceSchema") or (table_rel or {}).get("schema")
                   or ds.get("schema")),
        "table": (table.get("sourceTable") or (table_rel or {}).get("table")
                  or (tables[0] if tables else table["name"])),
        "file": _abs_csv(ir, file) if file else "",
        "sheet": table.get("sourceSheet") or table.get("sourceTable"),
        "query": table.get("sourceQuery") or ds.get("customSql") or (custom or {}).get("sql"),
    }


def _first_csv(ir: Dict) -> str:
    for ds in ir.get("dataSources", []):
        csvs = [f for f in ds.get("files", []) if ds.get("active") and f.lower().endswith(".csv")]
        if csvs:
            return csvs[0]
    return "PATH_TO_DATA.csv"


def _normalize_name(s: str) -> str:
    """Normalize a column name for fuzzy matching: underscores/slashes/hyphens/spaces
    all collapse to a single space, then lowercase.  Handles CSV underscore headers
    (Customer_ID) matching logical names (Customer ID) and slash variants (Country/Region).
    """
    return re.sub(r"[_/\-\s]+", " ", s).strip().lower()


def _build_csv_columns(csv_headers: List[str], ir_columns: List[Dict]) -> List[Dict]:
    """Match each CSV header to an IR logical column using normalized comparison.
    Returns a list of {name, csv_name, dataType} dicts ordered by CSV column order.
    Unmatched headers keep their raw name with dataType='string'.
    """
    ir_by_norm = {_normalize_name(c["name"]): c for c in ir_columns}
    result = []
    for h in csv_headers:
        n = _normalize_name(h)
        if n in ir_by_norm:
            col = ir_by_norm[n]
            result.append({"name": col["name"], "csv_name": h,
                           "dataType": col.get("dataType", "string")})
        else:
            result.append({"name": h, "csv_name": h, "dataType": "string"})
    return result


def _probe_for_table(table: Dict, ir: Dict) -> Dict | None:
    """Probe the CSV backing a table.  Returns {delimiter, columns} or None."""
    raw_path = table.get("sourceFile") or _first_csv(ir)
    path = _abs_csv(ir, raw_path)
    result = CP.probe(path)
    if result is None:
        return None
    ir_cols = ir.get("columns", [])
    # Scope the IR columns used for header->logical matching to THIS table's
    # physical CSV. Otherwise columns that share a normalized name across tables
    # (e.g. fact 'Customer ID' vs dim 'Customer_ID', 'Postal Code' vs
    # 'Postal_Code') collide and the later one wins, renaming a fact's foreign
    # key to the dim's spelling and breaking the relationship in Power BI.
    base = _normalize_name(os.path.basename(str(raw_path)))
    scoped = [c for c in ir_cols
              if _normalize_name(str(c.get("physicalTable") or "")) == base]
    if scoped:
        match_cols = scoped
    else:
        # Nothing resolved to this physical table. Only fall back to the global
        # column set when it is collision-free: a single physical table, or no
        # lineage at all (a genuinely single-flat source). With two-or-more
        # physical tables the global fallback is exactly what corrupts a fact's
        # foreign keys, so refuse it and match the raw headers instead.
        phys = {_normalize_name(str(c.get("physicalTable")))
                for c in ir_cols if c.get("physicalTable")}
        match_cols = ir_cols if len(phys) <= 1 else []
    return {
        "delimiter": result["delimiter"],
        "columns": _build_csv_columns(result["headers"], match_cols),
    }


def _logical_col(table: Dict, ir: Dict, col: str) -> str:
    """Map a physical CSV column name to its logical (model) name.

    A dim's emitted column is renamed from the CSV header (e.g. ``Customer_ID``)
    to the IR/logical name (``Customer ID``).  Decisions authored with the raw
    CSV name would then point at a column that does not exist, so relationships
    fail to resolve in Power BI Desktop.  This normalises either spelling to the
    logical name; unknown names pass through unchanged.
    """
    if table is None or table.get("role") not in ("dim", "fact"):
        return col
    probe = _probe_for_table(table, ir)
    if not probe:
        return col
    for c in probe["columns"]:
        if c.get("csv_name") == col and c["name"] != col:
            return c["name"]
    return col


def build_model_file(decisions: Dict, role_names: List[str] | None = None) -> str:
    tables = [t["name"] for t in decisions.get("tables", [])]
    tables += [fp["name"] for fp in decisions.get("fieldParameters", [])]
    refs = "\n".join(f"ref table {B.quote(t)}" for t in tables)
    if role_names:
        refs += "\n" + "\n".join(f"ref role {B.quote(r)}" for r in role_names)
    return ("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tdiscourageImplicitMeasures\n\tsourceQueryCulture: en-US\n\tdataAccessOptions\n"
        "\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
        f"annotation PBI_QueryOrder = {json.dumps(tables)}\n\n"
        "annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
        "annotation PBI_ProTooling = [\"DevMode\"]\n\n"
        f"{refs}\n")


# Tableau-style user-identity column names that translate to a Power BI RLS
# filter `[col] = USERPRINCIPALNAME()`. Mirrors the detector in parse_twb.py.
_RLS_USER_COL_RE = re.compile(
    r"(user\s?name|user[_ ]?id|e-?mail|login\s?name|\bupn\b|account)", re.IGNORECASE)

# Friendly role name per detected RLS pattern.
_RLS_ROLE_NAMES = {
    "Mapping-table": "User Security",
    "Dynamic": "Dynamic Security",
    "Group-based": "Group Security",
}


def _emitted_columns(ir: Dict, decisions: Dict) -> Dict[str, tuple]:
    """Map normalised column name -> (table_name, actual_col_name) for every
    column the model actually emits, so an RLS filter can only ever reference a
    column that exists (otherwise Desktop fails the role with 'column not found')."""
    out: Dict[str, tuple] = {}
    for table in decisions.get("tables", []):
        try:
            cols = _columns_for(table, ir, decisions)
        except Exception:
            cols = []
        for c in cols:
            key = re.sub(r"[\s_]+", "", c["name"]).lower()
            out.setdefault(key, (table["name"], c["name"]))
    return out


def _resolve_rls_target(ir: Dict, decisions: Dict) -> tuple:
    """Pick the (table, column) the RLS filter restricts on, or (None, None).

    Prefers the user-identity column the detector named; otherwise falls back to
    any user-identity column present in the emitted model."""
    rls = ir.get("rls") or {}
    emitted = _emitted_columns(ir, decisions)
    user_col = rls.get("userColumn")
    if user_col:
        key = re.sub(r"[\s_]+", "", user_col).lower()
        if key in emitted:
            return emitted[key]
    for _key, (tbl, col) in emitted.items():
        if _RLS_USER_COL_RE.search(col):
            return tbl, col
    return None, None


def build_roles(ir: Dict, decisions: Dict) -> tuple:
    """Build the RLS role TMDL when the parser detected row-level security.

    Returns (role_name, tmdl_text) or (None, None). When a user-identity column
    is resolvable the role carries a working `[col] = USERPRINCIPALNAME()`
    filter (Tableau USERNAME()/mapping-table -> Power BI dynamic RLS); otherwise
    it emits a valid read-permission scaffold for the operator to complete."""
    rls = ir.get("rls") or {}
    if not rls.get("detected"):
        return None, None
    role_name = _RLS_ROLE_NAMES.get(rls.get("type"), "Row-Level Security")
    tbl, col = _resolve_rls_target(ir, decisions)
    lines = [f"role {B.quote(role_name)}", f"{B.TAB}modelPermission: read", ""]
    if tbl and col:
        expr = f"{B.quote(tbl)}[{col}] = USERPRINCIPALNAME()"
        lines.append(f"{B.TAB}tablePermission {B.quote(tbl)} = {expr}")
    return role_name, "\n".join(lines) + "\n"


def build_relationships_file(decisions: Dict, ir: Dict) -> str:
    by_name = {t["name"]: t for t in decisions.get("tables", [])}
    blocks = []
    for i, rel in enumerate(decisions.get("relationships", [])):
        from_t, from_c = rel["fromColumn"].split(".", 1)
        to_t, to_c = rel["toColumn"].split(".", 1)
        from_c = _logical_col(by_name.get(from_t), ir, from_c)
        to_c = _logical_col(by_name.get(to_t), ir, to_c)
        block = (f"relationship {B.lineage(0xf000 + i)}\n"
                 f"\tfromColumn: {B.quote(from_t)}.{B.quote(from_c)}\n"
                 f"\ttoColumn: {B.quote(to_t)}.{B.quote(to_c)}\n")
        block += "\tcrossFilteringBehavior: bothDirections\n" if rel.get("crossFilter") == "both" else ""
        block += "\tisActive: false\n" if rel.get("active") is False else ""
        blocks.append(block)
    return "\n".join(blocks) + ("\n" if blocks else "")


def _build_calculated_table_tmdl(ct: Dict, lineage_base: int) -> str:
    """Build TMDL text for a DAX calculated table (no circular-ref risk).

    ct = {"name": "...", "dax": "...", "columns": [{"name", "dataType", "formatString", "summarizeBy"}]}
    Calculated tables are safe alternatives to calculated columns that reference
    other tables — they don't participate in the relationship-based processing cycle.
    """
    t = ct["name"]
    tag = f"a1000000-0000-4000-9000-{lineage_base:012x}"
    lines = [f"table {t!r}" if " " in t or not t[0].isalpha() else f"table {t}",
             f"\tlineageTag: {tag}",
             ""]
    for i, col in enumerate(ct.get("columns", []), start=1):
        ctag = f"a1000000-0000-4000-9000-{lineage_base + i:012x}"
        lines += [
            f"\tcolumn {col['name']!r}" if (" " in col["name"] or not col["name"][0].isalpha())
            else f"\tcolumn {col['name']}",
            f"\t\tdataType: {col.get('dataType', 'string')}",
        ]
        if col.get("formatString"):
            lines.append(f"\t\tformatString: {B.safe_format_string(col['formatString'])}")
        lines += [
            f"\t\tlineageTag: {ctag}",
            f"\t\tsummarizeBy: {col.get('summarizeBy', 'none')}",
            f"\t\tsourceColumn: [{col['name']}]",
            "",
            "\t\tannotation SummarizationSetBy = Automatic",
            "",
        ]
    dax = ct["dax"].strip()
    lines += [
        f"\tpartition {t} = calculated",
        "\t\tmode: import",
        "\t\tsource =",
        f"\t\t\t\t{dax}",
    ]
    return "\n".join(lines) + "\n"


def emit(ir: Dict, decisions: Dict, analysis_path: str) -> str:
    model_name = decisions.get("modelName") or ir["workbook"]["pascalName"]
    root = model_dir(analysis_path, model_name)
    defin = os.path.join(root, "definition")
    # Clean the definition/ tree before regenerating so that tables (or roles)
    # renamed or removed since a previous run do not leave stale TMDL files
    # behind. Stale files reuse the same lineageTag sequence as the new files
    # and cause Power BI Desktop to fail loading with a duplicate lineage-tag
    # ("An object with lineage-tag '...' already exists in the collection").
    if os.path.isdir(defin):
        shutil.rmtree(defin, ignore_errors=True)
    os.makedirs(os.path.join(defin, "tables"), exist_ok=True)
    write(os.path.join(defin, "database.tmdl"), "database\n\tcompatibilityLevel: 1600\n")
    role_name, role_text = build_roles(ir, decisions)
    write(os.path.join(defin, "model.tmdl"),
          build_model_file(decisions, [role_name] if role_name else None))
    write(os.path.join(defin, "relationships.tmdl"), build_relationships_file(decisions, ir))
    for i, table in enumerate(decisions.get("tables", []), start=1):
        fname = re.sub(r"[\\/:*?\"<>|]+", "_", table["name"])
        write(os.path.join(defin, "tables", f"{fname}.tmdl"),
              build_table_file(table, ir, decisions, i))
    for j, fp in enumerate(decisions.get("fieldParameters", []), start=1):
        fname = re.sub(r"[\\/:*?\"<>|]+", "_", fp["name"])
        write(os.path.join(defin, "tables", f"{fname}.tmdl"),
              FP.table_tmdl(fp, 0x7000 + j))
    # Calculated tables from decisions (avoids circular-reference issues with
    # calculated columns on dim tables that reference other tables via relationships)
    for k, ct in enumerate(decisions.get("calculatedTables", []), start=1):
        fname = re.sub(r"[\\/:*?\"<>|]+", "_", ct["name"])
        write(os.path.join(defin, "tables", f"{fname}.tmdl"),
              _build_calculated_table_tmdl(ct, 0xA000 + k))
    if role_name:
        rfname = re.sub(r"[\\/:*?\"<>|]+", "_", role_name)
        write(os.path.join(defin, "roles", f"{rfname}.tmdl"), role_text)
    write(os.path.join(root, "definition.pbism"), json.dumps(PBISM, indent=2))
    write(os.path.join(root, "diagramLayout.json"), json.dumps({"version": "1.1.0", "diagrams": []}))
    write(os.path.join(root, ".platform"), platform_file("SemanticModel", model_name, model_name + ".sm"))
    write(os.path.join(os.path.dirname(root), f"{model_name}.pbip"), pbip_root(model_name))
    return root


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Emit TMDL semantic model from IR + decisions.")
    parser.add_argument("analysis", help="path to analysis.json")
    parser.add_argument("--decisions", required=True, help="path to decisions.json")
    args = parser.parse_args(argv)
    for p in (args.analysis, args.decisions):
        if not os.path.isfile(p):
            print(f"ERROR: file not found: {p}", file=sys.stderr); return 2
    ir, decisions = load_json(args.analysis), load_json(args.decisions)
    decisions = merge_partial_measures(decisions, args.analysis)
    decisions = reassign_orphan_measures(decisions)
    root = emit(ir, decisions, args.analysis)
    print(f"Wrote semantic model: {root}\n  tables: {len(decisions.get('tables', []))}  "
          f"measures: {len(decisions.get('measures', []))}  "
          f"relationships: {len(decisions.get('relationships', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
