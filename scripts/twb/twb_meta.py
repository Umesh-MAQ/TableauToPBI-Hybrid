"""twb_meta.py — extract model-shaping metadata the base parser previously skipped.

These constructs feed the Power BI semantic model directly and were the largest
remaining gaps that forced the LLM to re-read raw XML:

  * relationships  — Tableau <object-graph> physical joins (fact<->dim, key, side)
  * hierarchies    — <drill-path> dimension drill levels (Power BI hierarchies)
  * columnTableMap — which physical table each field belongs to (fact vs dim split)
  * physicalTables — per-relation column lists (multi-CSV / multi-table sources)
  * groups / bins  — real <group ... (group)> and <calculation class='bin'> defs
  * theme          — workbook fonts + mark colour palette for report theming

Everything here is deterministic, None-safe, and never fabricates: an absent
construct yields an empty list / null, never a guessed value.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twb_xml as X  # noqa: E402


# --------------------------------------------------------------------------- #
# Relationships (logical model joins) — the key input for star-schema design
# --------------------------------------------------------------------------- #
def extract_relationships(root: ET.Element) -> List[Dict]:
    """Doe
    """
    rels: List[Dict] = []
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        id_to_table = _object_id_table_map(ds)
        for graph in ds.iter("object-graph"):
            for rel in graph.findall("relationships/relationship"):
                record = _relationship_record(rel, id_to_table)
                if record:
                    rels.append(record)
    return rels


def _object_id_table_map(ds: ET.Element) -> Dict[str, str]:
    """Map an object-graph object id -> its table caption/name."""
    mapping: Dict[str, str] = {}
    for obj in ds.iter("object"):
        oid = X.attr(obj, "id")
        if not oid:
            continue
        caption = X.attr(obj, "caption")
        rel = obj.find(".//relation")
        name = X.attr(rel, "name") if rel is not None else None
        mapping[oid] = caption or name or oid
    return mapping


def _relationship_record(rel: ET.Element, id_to_table: Dict[str, str]) -> Optional[Dict]:
    expr = rel.find("expression")
    operands = expr.findall("expression") if expr is not None else []
    operator = X.attr(expr, "op", "=") if expr is not None else "="
    if len(operands) < 2:
        return None
    from_col = X.strip_brackets(X.attr(operands[0], "op"))
    to_col = X.strip_brackets(X.attr(operands[1], "op"))
    first = rel.find("first-end-point")
    second = rel.find("second-end-point")
    from_id = X.attr(first, "object-id")
    to_id = X.attr(second, "object-id")
    return {
        "fromTable": id_to_table.get(from_id, from_id),
        "toTable": id_to_table.get(to_id, to_id),
        "fromColumn": from_col,
        "toColumn": to_col,
        "operator": operator,
        "toSideUnique": X.attr(second, "unique-key") == "true",
    }


# --------------------------------------------------------------------------- #
# Hierarchies — Tableau drill paths become Power BI hierarchies
# --------------------------------------------------------------------------- #
def extract_hierarchies(root: ET.Element) -> List[Dict]:
    """Return <drill-path> hierarchies (name + ordered level fields)."""
    out: List[Dict] = []
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        ds_caption = X.datasource_caption(ds)
        for path in ds.iter("drill-path"):
            levels = [X.strip_brackets(f.text) for f in path.findall("field") if f.text]
            if levels:
                out.append({
                    "name": X.attr(path, "name", "Hierarchy"),
                    "datasource": ds_caption,
                    "levels": levels,
                })
    return out


# --------------------------------------------------------------------------- #
# Physical tables + column->table map — drives fact / dimension assignment
# --------------------------------------------------------------------------- #
def extract_physical_tables(root: ET.Element) -> List[Dict]:
    """Return distinct physical relations (tables) with columns + CSV parse settings.

    Parse settings (delimiter, encoding, header, decimal/thousands chars) are read
    from the workbook METADATA — the <columns> wrapper attributes and the
    <metadata-record class='capability'> attributes — so the emitter never has to
    open the data file to sniff them. This keeps the pipeline metadata-only and
    makes European CSVs (';' delimiter, ',' decimals, windows-1252) correct.
    """
    caps = _capability_attrs(root)
    tables: List[Dict] = []
    seen = set()
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        for rel in ds.iter("relation"):
            if X.attr(rel, "type") != "table":
                continue
            name = X.strip_brackets(X.attr(rel, "name"))
            if not name or name in seen:
                continue
            cols_el = rel.find("columns")
            cols = [X.attr(c, "name") for c in rel.findall("columns/column")]
            cols = [c for c in cols if c]
            if cols:
                seen.add(name)
                tables.append({
                    "name": name,
                    "columns": cols,
                    "parse": _parse_settings(cols_el, caps.get(name, {})),
                })
    return tables


# character-set name -> Power Query / Windows code page (Encoding= in M).
_CHARSET_CODEPAGE = {
    "utf-8": 65001, "utf8": 65001, "windows-1252": 1252, "cp1252": 1252,
    "iso-8859-1": 28591, "latin1": 28591, "utf-16": 1200, "utf-16le": 1200,
    "us-ascii": 20127, "ascii": 20127,
}


def _parse_settings(cols_el, cap: Dict) -> Dict:
    """Build a CSV parse-settings dict from <columns> attrs + capability attrs.

    Prefers the explicit <columns> wrapper (separator/character-set/header), then
    fills decimal/thousands/locale from the capability metadata-record. Returns a
    delimiter, code page, header flag and number-format chars — all from metadata.
    """
    sep = (X.attr(cols_el, "separator") if cols_el is not None else None) \
        or cap.get("field-delimiter")
    charset = (X.attr(cols_el, "character-set") if cols_el is not None else None) \
        or cap.get("character-set")
    header = (X.attr(cols_el, "header") if cols_el is not None else None)
    has_header = (header == "yes") if header else (cap.get("header-row") == "true")
    locale = (X.attr(cols_el, "locale") if cols_el is not None else None) \
        or cap.get("locale")
    codepage = _CHARSET_CODEPAGE.get((charset or "").lower(), 65001)
    return {
        "delimiter": sep,
        "charset": charset,
        "codepage": codepage,
        "hasHeader": has_header,
        "decimalChar": cap.get("decimal-char"),
        "thousandsChar": cap.get("thousands-char"),
        "locale": locale,
    }


def _capability_attrs(root: ET.Element) -> Dict[str, Dict]:
    """Map physical table name -> {field-delimiter, character-set, decimal-char, ...}.

    Tableau writes one <metadata-record class='capability'> per CSV with an
    <attributes> block describing how it parsed that file. Values are quoted
    (e.g. '\"UTF-8\"'); we strip the quotes. Keyed by the parent-name table.
    """
    out: Dict[str, Dict] = {}
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        for rec in ds.iter("metadata-record"):
            if X.attr(rec, "class") != "capability":
                continue
            table = X.strip_brackets(rec.findtext("parent-name") or "")
            if not table:
                continue
            attrs: Dict[str, str] = {}
            for a in rec.findall("attributes/attribute"):
                key = X.attr(a, "name")
                val = (a.text or "").strip().strip('"')
                if key:
                    attrs[key] = val
            if attrs:
                out.setdefault(table, {}).update(attrs)
    return out


def extract_column_table_map(root: ET.Element) -> Dict[str, str]:
    """Map each logical column -> its physical table via <cols><map> entries.

    Tableau records `<map key='[col]' value='[table].[physcol]'/>` for every
    field in a multi-table source. We invert this to 'which table owns a column',
    which lets the schema stage place each field on the correct fact/dim table.
    """
    mapping: Dict[str, str] = {}
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        for cols in ds.iter("cols"):
            for m in cols.findall("map"):
                key = X.strip_brackets(X.attr(m, "key"))
                value = X.attr(m, "value") or ""
                # value looks like "[table.csv].[column]" -> take the table part
                if "].[" in value:
                    table = X.strip_brackets(value.split("].[")[0].lstrip("["))
                    if key and table:
                        mapping[key] = table
    return mapping


# --------------------------------------------------------------------------- #
# Column metadata (<metadata-records>) — authoritative physical lineage
# --------------------------------------------------------------------------- #
# Tableau records one <metadata-record class='column'> per physical column with
# its true storage type (local-type), default aggregation, owning physical table
# (parent-name) and original database/file column name (remote-name). This is a
# more reliable source than scraping <cols><map>, and gives the LLM the physical
# lineage (logical -> physical table + remote column) it otherwise can't see.
_MEASURE_AGGS = {"Sum", "Count", "CountD", "Average", "Avg", "Min", "Max", "Median"}


def extract_column_metadata(root: ET.Element) -> Dict[str, Dict]:
    """Map logical column name -> {remoteName, parentTable, metaType, aggregation, metaRole}.

    Deterministic and None-safe: a column absent from metadata-records simply has
    no entry. Never overwrites or guesses; consumers merge this additively.
    """
    out: Dict[str, Dict] = {}
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        for meta in ds.iter("metadata-records"):
            for rec in meta.findall("metadata-record"):
                if X.attr(rec, "class") != "column":
                    continue
                local = X.strip_brackets(rec.findtext("local-name") or "")
                if not local or local in out:
                    continue
                agg = (rec.findtext("aggregation") or "").strip() or None
                parent = X.strip_brackets(rec.findtext("parent-name") or "") or None
                remote = (rec.findtext("remote-name") or "").strip() or None
                local_type = (rec.findtext("local-type") or "").strip() or None
                out[local] = {
                    "remoteName": remote,
                    "parentTable": parent,
                    "metaType": local_type,
                    "aggregation": agg,
                    "metaRole": "measure" if agg in _MEASURE_AGGS else "dimension",
                }
    return out


def build_calc_name_map(root: ET.Element) -> Dict[str, str]:
    """Map EVERY calculated column's internal name -> its caption (no dedup).

    Unlike the caption-deduped calc list, this captures duplicated calcs whose
    internal name carries a `(copy)_<hash>` suffix, so inter-calc references in
    formulas can be inlined to readable captions for the LLM and DAX translator.
    """
    out: Dict[str, str] = {}
    for ds in X.iter_datasources(root):
        for col in ds.iter("column"):
            if col.find("calculation") is None:
                continue
            fn = X.strip_brackets(X.attr(col, "name"))
            if not fn:
                continue
            out[fn] = X.attr(col, "caption") or fn
    return out


# --------------------------------------------------------------------------- #
# Groups & Bins — real user-defined constructs only (never auto-generated ones)
# --------------------------------------------------------------------------- #
def extract_groups(root: ET.Element) -> List[Dict]:
    """Return user groups: <group name='[Field (group)]'> alias buckets.

    Skips Tableau's hidden auto-columns (set inclusions, level-member helpers).
    """
    groups: List[Dict] = []
    for ds in X.iter_datasources(root):
        for grp in ds.iter("group"):
            name = X.attr(grp, "name", "")
            if X.attr(grp, "hidden") == "true" or grp.get("user:auto-column"):
                continue
            if "(group)" not in name:
                continue
            clean = X.strip_brackets(name)
            source = clean.split("(group)")[0].strip() or None
            aliases = _group_aliases(grp)
            groups.append({
                "name": clean,
                "sourceField": source,
                "aliases": aliases,
            })
    return groups


def _group_aliases(grp: ET.Element) -> Dict[str, List[str]]:
    """Collect alias -> [member values] from a group's nested <groupfilter>s."""
    aliases: Dict[str, List[str]] = {}
    for gf in grp.findall("groupfilter"):
        alias = X.attr(gf, "user:ui-marker") and X.attr(gf, "member")
        members = [X.strip_brackets(X.attr(m, "member"))
                   for m in gf.iter("groupfilter") if X.attr(m, "function") == "member"]
        members = [m for m in members if m]
        label = X.strip_brackets(X.attr(gf, "member") or "") or None
        if label and members:
            aliases[label] = members
    return aliases


def extract_bins(root: ET.Element) -> List[Dict]:
    """Return numeric bins: <column><calculation class='bin' .../></column>."""
    bins: List[Dict] = []
    for ds in X.iter_datasources(root):
        for col in ds.iter("column"):
            calc = col.find("calculation")
            if calc is None or X.attr(calc, "class") != "bin":
                continue
            size = (X.attr(calc, "decimal-bin-size")
                    or X.attr(calc, "bin-size") or X.attr(calc, "size"))
            source = (X.attr(calc, "source-field") or X.attr(calc, "field")
                      or X.attr(calc, "formula") or "")
            bins.append({
                "name": X.attr(col, "caption") or X.strip_brackets(X.attr(col, "name")),
                "sourceField": X.strip_brackets(source) or None,
                "size": size,
            })
    return bins


# --------------------------------------------------------------------------- #
# Theme — workbook fonts + mark colour palette for the report layer
# --------------------------------------------------------------------------- #
def extract_theme(root: ET.Element) -> Dict:
    """Return a small theme record from workbook-level <style> rules + palettes."""
    fonts: Dict[str, str] = {}
    colors: List[str] = []
    # Workbook-level formatting (font family, title size, etc.)
    for style in root.findall("./style"):
        for rule in style.findall("style-rule"):
            element = X.attr(rule, "element", "")
            for fmt in rule.findall("format"):
                attr = X.attr(fmt, "attr", "")
                value = X.attr(fmt, "value", "")
                if attr and value:
                    fonts[f"{element}.{attr}"] = value
    # Mark colour palette (datasource <style><style-rule element='mark'>)
    for enc in root.iter("encoding"):
        if X.attr(enc, "attr") != "color":
            continue
        for mp in enc.findall("map"):
            to = X.attr(mp, "to")
            if to and to.startswith("#") and to not in colors:
                colors.append(to)
    return {
        "fontFamily": fonts.get("all.font-family"),
        "titleFontSize": fonts.get("title.font-size"),
        "worksheetFontSize": fonts.get("worksheet.font-size"),
        "formatRules": fonts or None,
        "markColors": colors or None,
    }
