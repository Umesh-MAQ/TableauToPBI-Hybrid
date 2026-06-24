"""twb_datasources.py — extract datasources, columns, calculated fields, parameters.

Pure deterministic extraction from the .twb XML into IR fragments. The DAX
complexity classifier lives in scripts/dax/map_dax.py; here we only flag whether
a calc *looks* like a table calc (WINDOW_/RUNNING_/INDEX/LOOKUP) for the IR.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twb_xml as X  # noqa: E402

TABLE_CALC_RE = re.compile(
    r"\b(WINDOW_\w+|RUNNING_\w+|INDEX|RANK|FIRST|LAST|LOOKUP|TOTAL|PREVIOUS_VALUE)\s*\(",
    re.IGNORECASE,
)

# Level-of-Detail expressions ({FIXED|INCLUDE|EXCLUDE ...}). These are NOT table
# calcs (different Tableau concept, no <table-calc> element) but the LLM needs to
# know about them to write CALCULATE + ALLEXCEPT-style DAX.
LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)


def extract_datasources(root: ET.Element, active_names: set) -> List[Dict]:
    """Return IR dataSources list (excluding the inline Parameters store)."""
    result: List[Dict] = []
    for ds in X.iter_datasources(root):
        name = X.attr(ds, "name", "")
        if name == "Parameters":
            continue
        caption = X.datasource_caption(ds)
        conn = _first_connection(ds)
        conn_class = X.attr(conn, "class")
        files = _connection_files(ds)
        relations = _relations(ds)
        custom_sql = next((r["sql"] for r in relations if r.get("type") == "customSql"), None)
        result.append({
            "name": caption,
            "internalName": name,
            "connectionClass": conn_class or "",
            "sourceType": X.resolve_source_type(conn_class),
            "files": files,
            "server": X.attr(conn, "server"),
            "database": X.attr(conn, "dbname") or X.attr(conn, "database"),
            "schema": X.attr(conn, "schema"),
            "tables": _relation_tables(ds),
            "relations": relations,
            "joins": _joins(ds),
            "customSql": custom_sql,
            "connectionMode": _connection_mode(ds, conn),
            "active": caption in active_names or name in active_names,
        })
    return result


def _first_connection(ds: ET.Element) -> Optional[ET.Element]:
    """Return the most informative physical connection of a datasource.

    A datasource is usually wrapped in a federated <connection> that holds one or
    more <named-connection>s. Prefer a named-connection whose inner <connection>
    actually carries a server/dbname (the real source), so cross-database or
    multi-connection datasources resolve to a populated endpoint rather than an
    empty federated shell. Falls back to the first named-connection, then the
    outer connection.
    """
    outer = X.find(ds, "connection")
    if outer is None:
        return None
    named = outer.findall("named-connections/named-connection/connection")
    for c in named:
        if c.get("server") or c.get("dbname") or c.get("database"):
            return c
    return named[0] if named else outer


def _split_schema_table(table_attr: str) -> tuple:
    """Split a Tableau relation table like [dbo].[Orders] into (schema, name)."""
    full = (table_attr or "").replace("[", "").replace("]", "")
    parts = full.split(".")
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, full


def _relations(ds: ET.Element) -> List[Dict]:
    """Return the physical relations backing a datasource.

    Each entry is either a table reference {schema, table, type:"table"} or a
    custom-SQL block {type:"customSql", sql:<query>}. Custom SQL (relation
    type="text") is what lets the emitter build a native-query partition instead
    of a table navigation — without it the query logic would be lost.
    """
    out: List[Dict] = []
    seen_sql = set()
    for rel in ds.iter("relation"):
        table_attr = rel.get("table")
        if table_attr:
            schema, name = _split_schema_table(table_attr)
            entry = {"schema": schema, "table": name, "type": "table"}
            if entry not in out:
                out.append(entry)
        elif rel.get("type") == "text":
            sql = (rel.text or "").strip()
            if sql and sql not in seen_sql:
                seen_sql.add(sql)
                out.append({"schema": None, "table": rel.get("name") or "CustomSQL",
                            "type": "customSql", "sql": sql})
    return out


def _join_col(op: Optional[str]) -> Optional[str]:
    """Return the column name from a Tableau join operand like [Orders].[CustID]."""
    if not op:
        return None
    cleaned = op.replace("[", "").replace("]", "")
    return cleaned.split(".")[-1].strip() or None


def _joins(ds: ET.Element) -> List[Dict]:
    """Return the physical joins between tables inside a datasource.

    Tableau models multi-table sources as <relation type="join" join="inner|left|
    right|full"> nodes whose two child <relation> operands are the joined tables
    and whose <clause>/<expression op="="> carries the key columns. Capturing the
    join type and key pair lets the star-schema/model layer recreate the physical
    join (or a model relationship) instead of treating each table as standalone.
    Both the modern clause form and the legacy left/right attribute form are read.
    """
    out: List[Dict] = []
    for rel in ds.iter("relation"):
        if rel.get("type") != "join":
            continue
        operands = rel.findall("relation")
        left_tbl = right_tbl = None
        if len(operands) >= 2:
            left_tbl = operands[0].get("name") or _split_schema_table(operands[0].get("table") or "")[1]
            right_tbl = operands[1].get("name") or _split_schema_table(operands[1].get("table") or "")[1]
        left_col = right_col = None
        clause = rel.find("clause")
        top = clause.find("expression") if clause is not None else None
        if top is not None:
            ops = top.findall("expression")
            if len(ops) == 2:
                left_col = _join_col(ops[0].get("op"))
                right_col = _join_col(ops[1].get("op"))
        if left_tbl is None and rel.get("left"):
            left_tbl = (rel.get("left") or "").replace("[", "").replace("]", "").strip() or None
        if right_tbl is None and rel.get("right"):
            right_tbl = (rel.get("right") or "").replace("[", "").replace("]", "").strip() or None
        entry = {
            "joinType": (rel.get("join") or "inner").lower(),
            "leftTable": left_tbl, "rightTable": right_tbl,
            "leftColumn": left_col, "rightColumn": right_col,
        }
        if any(entry[k] for k in ("leftTable", "rightTable")) and entry not in out:
            out.append(entry)
    return out


def _connection_mode(ds: ET.Element, conn: Optional[ET.Element]) -> str:
    """Infer 'directQuery' vs 'import' from the Tableau connection.

    A live DB connection with no extract maps to DirectQuery intent; an extract
    (<extract>) or a file/text relation maps to Import. Advisory IR signal only —
    the emitter still defaults to import unless told otherwise.
    """
    if ds.find(".//extract") is not None:
        return "import"
    if any(rel.get("type") == "text" for rel in ds.iter("relation")):
        return "import"
    mode = (X.attr(conn, "mode") or "").lower()
    if "live" in mode:
        return "directQuery"
    return "import"


def _connection_files(ds: ET.Element) -> List[str]:
    files: List[str] = []
    for conn in ds.iter("connection"):
        for key in ("filename", "directory"):
            val = conn.get(key)
            if val and val not in files:
                files.append(val)
    return files


def _relation_tables(ds: ET.Element) -> List[str]:
    tables: List[str] = []
    for rel in ds.iter("relation"):
        tbl = rel.get("table") or rel.get("name")
        if tbl:
            clean = X.strip_brackets(tbl)
            if clean and clean not in tables:
                tables.append(clean)
    return tables


def extract_columns(root: ET.Element) -> List[Dict]:
    """Return non-calc <column> IR records, one per (datasource, name).

    A column appears several times in a .twb: a bare definition inside the
    physical ``<relation><columns>`` (and again in ``<object-graph>``) carrying
    only name/datatype, plus a richer definition in the datasource body that adds
    the friendly ``caption``, ``semantic-role`` (geo role) and number ``format``.
    Earlier this kept the FIRST occurrence and silently dropped the richer one,
    losing every caption and geo role. We now MERGE occurrences: the first seen
    record is the base and any later one fills fields it left empty (never
    overwriting real data). Lookup stays O(1) via a dict, so this is no slower
    than the previous set-based dedupe.
    """
    by_key: Dict[tuple, Dict] = {}
    order: List[tuple] = []
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        ds_caption = X.datasource_caption(ds)
        ordinal = _ordinal_map(ds)
        for col in ds.iter("column"):
            if col.find("calculation") is not None:
                continue
            name = X.attr(col, "name", "")
            if "__tableau_internal" in name or "_object_id__" in name:
                continue
            record = _column_record(col, ds_caption, ordinal)
            key = (record["datasource"], record["name"])
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = record
                order.append(key)
            else:
                _merge_column(existing, record)
    return [by_key[k] for k in order]


def _merge_column(base: Dict, extra: Dict) -> None:
    """Fold a duplicate column occurrence into ``base`` without losing data.

    Fills caption/semanticRole/format/ordinal only when the base left them empty
    (the datasource-body definition enriches the bare relation column), and lets a
    later definition upgrade the role to 'measure'. Existing non-empty values are
    never overwritten, so the merge is strictly additive.
    """
    for field in ("caption", "semanticRole", "format", "ordinal"):
        if base.get(field) in (None, "") and extra.get(field) not in (None, ""):
            base[field] = extra[field]
    if extra.get("role") == "measure":
        base["role"] = "measure"


def _ordinal_map(ds: ET.Element) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    meta = ds.find("connection/metadata-records")
    if meta is None:
        return mapping
    for rec in meta.findall("metadata-record"):
        local = rec.findtext("local-name")
        ordinal = rec.findtext("ordinal")
        if local and ordinal and ordinal.isdigit():
            mapping[X.strip_brackets(local)] = int(ordinal)
    return mapping


def _column_record(col: ET.Element, ds_caption: str, ordinal: Dict[str, int]) -> Dict:
    name = X.strip_brackets(X.attr(col, "name"))
    return {
        "datasource": ds_caption,
        "name": name,
        "caption": X.attr(col, "caption"),
        "dataType": X.resolve_datatype(X.attr(col, "datatype")),
        "role": "measure" if X.attr(col, "role") == "measure" else "dimension",
        "semanticRole": X.attr(col, "semantic-role"),
        "format": _format_string(col),
        "ordinal": ordinal.get(name),
    }


def _format_string(col: ET.Element) -> Optional[str]:
    """Return the column's Tableau number/date format.

    Prefers a nested ``<format>`` element, then falls back to the column's own
    ``default-format`` attribute (where Tableau stores currency/percent/decimal
    masks like ``$#,##0`` or ``p0``). Capturing this lets the emitter set a
    matching Power BI formatString so numbers look like the source workbook.
    """
    fmt = col.find("./format")
    if fmt is not None and X.attr(fmt, "format"):
        return X.attr(fmt, "format")
    return X.attr(col, "default-format")


def extract_calculated_fields(root: ET.Element) -> List[Dict]:
    """Return IR calculatedFields with verbatim decoded formulas.

    Parameters are NOT calculated fields. The inline 'Parameters' datasource and
    any column carrying a 'param-domain-type' are skipped so a parameter never
    appears twice in the IR (once here as a constant-formula calc and again in
    parameters[]). Without this skip every Start Date/End Date/list parameter is
    duplicated and can collide with a parameter table in the emitted model.

    Dedup is keyed on the GLOBALLY-UNIQUE internal field id (the column ``name``,
    e.g. ``Calculation_2234..``), NOT the display caption. Tableau repeats a calc's
    <column> inside every worksheet-local datasource copy with the same internal
    name, so collapsing on the id keeps exactly one definition per real field.
    Two DISTINCT fields that merely share a display caption -- routine in workbooks
    full of renamed "(copy)" fields -- both survive; the second's caption is given
    a numeric suffix so its derived measure name stays unique all the way through
    classify and the merge-stage name dedup. Keying on the caption instead would
    silently DROP one of two same-named fields and lose its measure.
    """
    fields: List[Dict] = []
    seen_fields = set()        # internal field ids already captured (collapse copies)
    used_captions: Dict[str, int] = {}  # base caption -> count, to disambiguate
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") == "Parameters":
            continue
        for col in ds.iter("column"):
            if col.get("param-domain-type") is not None:
                continue
            calc = col.find("calculation")
            if calc is None or calc.get("formula") is None:
                continue
            field_name = X.attr(col, "name")
            key_field = (field_name or "").strip("[]")
            if key_field and key_field in seen_fields:
                continue
            if key_field:
                seen_fields.add(key_field)
            caption = X.attr(col, "caption") or X.strip_brackets(field_name)
            # Disambiguate a caption already owned by a different field so the
            # derived measure name is unique end to end (parse -> classify -> merge).
            n = used_captions.get(caption, 0)
            used_captions[caption] = n + 1
            if n:
                caption = f"{caption} ({n + 1})"
            formula = X.decode_entities(calc.get("formula"))
            # Faithful XML fact: Tableau records a table-calc with a real
            # <table-calc> element inside <calculation>. Prefer that element;
            # only fall back to a formula token scan when it is absent. We do NOT
            # judge 'complexity' or a DAX kind here -- those are Power BI decisions
            # made downstream (map_dax / the model emitter), not XML translation.
            is_tc = (calc.find("table-calc") is not None
                     or bool(TABLE_CALC_RE.search(formula)))
            is_lod = bool(LOD_RE.search(formula))
            fields.append({
                "caption": caption,
                "fieldName": X.attr(col, "name"),
                "dataType": X.resolve_datatype(X.attr(col, "datatype")),
                "role": X.attr(col, "role"),
                "formula": formula,
                "isTableCalc": is_tc,
                "isLOD": is_lod,
            })
    return fields


def extract_parameters(root: ET.Element) -> List[Dict]:
    """Return IR parameters from the inline Parameters datasource."""
    params: List[Dict] = []
    for ds in X.iter_datasources(root):
        if X.attr(ds, "name") != "Parameters":
            continue
        for col in ds.iter("column"):
            if col.get("param-domain-type") is None:
                continue
            member_els = col.findall("members/member")
            members = [X.attr(m, "value") for m in member_els]
            params.append({
                "name": X.attr(col, "caption") or X.strip_brackets(X.attr(col, "name")),
                "internalName": X.attr(col, "name"),
                "dataType": X.resolve_datatype(X.attr(col, "datatype")),
                "domainType": X.attr(col, "param-domain-type", "any"),
                "default": X.attr(col, "value"),
                "values": [v for v in members if v],
                "valueLabels": _param_value_labels(col, member_els) or None,
                "range": _param_range(col),
                "format": X.attr(col, "default-format") or _format_string(col),
            })
    return params


def _param_value_labels(col: ET.Element, member_els) -> Dict[str, str]:
    """Map a parameter's raw value -> friendly display alias.

    Two XML sources carry the alias and are merged: each
    ``<member alias='Adults 18+' value='\"Adults\"'/>`` and a sibling
    ``<aliases><alias key='\"Adults\"' value='Adults 18+'/></aliases>``. Keys are
    kept in the same quoted form as the ``values`` array so they line up, letting
    the Power BI slicer show 'Adults 18+' instead of the raw code '\"Adults\"'.
    """
    labels: Dict[str, str] = {}
    for m in member_els:
        val, alias = X.attr(m, "value"), X.attr(m, "alias")
        if val and alias:
            labels[val] = alias
    for al in col.findall("aliases/alias"):
        key, value = X.attr(al, "key"), X.attr(al, "value")
        if key and value:
            labels.setdefault(key, value)
    return labels


def _param_range(col: ET.Element) -> Optional[Dict]:
    """Return {min,max,granularity} for range parameters (What-If in Power BI)."""
    rng = col.find("range")
    if rng is None:
        return None
    return {
        "min": X.attr(rng, "min"),
        "max": X.attr(rng, "max"),
        "granularity": X.attr(rng, "granularity"),
    }
