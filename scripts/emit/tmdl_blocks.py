"""tmdl_blocks.py — reusable TMDL text builders.

Each function returns a TMDL fragment (tab-indented, as Power BI expects).
Indentation is semantic in TMDL: one tab per nesting level. These builders are
pure string formatting consumed by emit_tmdl.py — no parsing, no LLM.
"""
from __future__ import annotations

import csv as _csv
from typing import Dict, List, Optional, Tuple

TAB = "\t"

# IR dataType -> (TMDL dataType, summarizeBy, isDateTime)
TYPE_MAP = {
    "string": ("string", "none", False),
    "text": ("string", "none", False),
    "integer": ("int64", "sum", False),
    "int": ("int64", "sum", False),
    "int64": ("int64", "sum", False),
    "real": ("double", "sum", False),
    "double": ("double", "sum", False),
    "float": ("double", "sum", False),
    "decimal": ("double", "sum", False),
    "boolean": ("boolean", "none", False),
    "bool": ("boolean", "none", False),
    "date": ("dateTime", "none", True),
    "datetime": ("dateTime", "none", True),
}


def quote(name: str) -> str:
    """Quote a TMDL identifier only when it needs it."""
    if name and (name[0].isdigit() or any(c in name for c in " '\"-./()&")):
        return f"'{name}'"
    return name


def safe_format_string(fmt: Optional[str]) -> Optional[str]:
    """Make a TMDL formatString safe for Power BI Desktop to load.

    A formatString value that STARTS with a double quote makes the TMDL parser
    treat the whole value as a quoted string (requiring every internal quote to
    be doubled). Power BI Desktop then rejects it with
    ``InvalidValueFormat ... un-escaped quote marker`` — and the static
    ``tmdl-validate`` linter does NOT catch this. Convert a leading quoted
    literal (e.g. the ``"$"`` in ``"$"#,##0.00``) into backslash-escaped
    characters (``\\$#,##0.00``) — Power BI renders both identically. Mid-string
    quotes (e.g. ``#,##0,"K"``) are valid and left untouched.
    """
    if not fmt or not isinstance(fmt, str) or not fmt.startswith('"'):
        return fmt
    end = fmt.find('"', 1)
    if end == -1:
        # Malformed unterminated leading quote — drop it defensively so the value
        # never starts with a quote marker.
        return fmt[1:]
    literal = fmt[1:end]
    rest = fmt[end + 1:]
    escaped = "".join("\\" + c for c in literal)
    return escaped + rest


def lineage(seq: int) -> str:
    """Deterministic lineage tag from a sequence number."""
    return f"a1000000-0000-4000-9000-{seq:012x}"


def column_block(col: Dict, seq: int) -> str:
    """Build a TMDL column block from an IR column dict."""
    name = col["name"]
    tmdl_type, summarize, is_date = TYPE_MAP.get(col["dataType"], ("string", "none", False))
    if col["dataType"] in ("integer", "real") and summarize == "sum":
        summarize = "sum" if col.get("role") == "measure" else "none"
    lines = [f"{TAB}column {quote(name)}"]
    lines.append(f"{TAB}{TAB}dataType: {tmdl_type}")
    fmt = col.get("format") or ("m/d/yyyy" if is_date else None)
    if col["dataType"] == "integer":
        fmt = fmt or "#,0"
    lines.append(f"{TAB}{TAB}lineageTag: {lineage(seq)}")
    if fmt:
        lines.insert(2, f"{TAB}{TAB}formatString: {safe_format_string(fmt)}")
    lines.append(f"{TAB}{TAB}summarizeBy: {summarize}")
    lines.append(f"{TAB}{TAB}sourceColumn: {name}")
    lines.append("")
    lines.append(f"{TAB}{TAB}annotation SummarizationSetBy = Automatic")
    if is_date:
        lines.append("")
        lines.append(f"{TAB}{TAB}annotation UnderlyingDateTimeDataType = Date")
    return "\n".join(lines)


def hierarchy_block(name: str, levels: List[Tuple[str, str]], seq: int) -> str:
    """Build a TMDL hierarchy block from ordered (levelName, columnName) pairs.

    Hierarchies live at the table level (one tab in), with each ``level`` nested
    a further tab and pointing at a real column on the SAME table via ``column:``.
    Power BI Desktop rejects a hierarchy whose level references a missing column,
    so the caller must only pass levels that resolve to actual table columns.
    """
    lines = [
        f"{TAB}hierarchy {quote(name)}",
        f"{TAB}{TAB}lineageTag: {lineage(seq)}",
        "",
    ]
    for i, (level_name, column_name) in enumerate(levels):
        lines += [
            f"{TAB}{TAB}level {quote(level_name)}",
            f"{TAB}{TAB}{TAB}lineageTag: {lineage(seq * 16 + i + 1)}",
            f"{TAB}{TAB}{TAB}column: {quote(column_name)}",
            "",
        ]
    return "\n".join(lines).rstrip()


def calc_column_block(name: str, dax: str, data_type: str,
                      fmt: Optional[str], seq: int) -> str:
    """Build a TMDL calculated-column block (column Name = <DAX>).

    Multi-line DAX (VAR/RETURN) must be written as an indented body on its own
    lines, exactly like a measure — otherwise the continuation lines land at
    column 0 and Power BI Desktop rejects them ("VAR is not a supported property
    in the current context"). tmdl-validate does NOT catch this; only Desktop.
    """
    tmdl_type, _summ, is_date = TYPE_MAP.get(data_type, ("string", "none", False))
    fmt = fmt or ("m/d/yyyy" if is_date else None)
    dax = dax.strip()
    if "\n" in dax:
        body = "\n".join(f"{TAB}{TAB}{TAB}{ln}" for ln in dax.splitlines())
        lines = [f"{TAB}column {quote(name)} =", body]
    else:
        lines = [f"{TAB}column {quote(name)} = {dax}"]
    lines.append(f"{TAB}{TAB}dataType: {tmdl_type}")
    if fmt:
        lines.append(f"{TAB}{TAB}formatString: {safe_format_string(fmt)}")
    lines.append(f"{TAB}{TAB}lineageTag: {lineage(seq)}")
    lines.append(f"{TAB}{TAB}summarizeBy: none")
    lines.append("")
    lines.append(f"{TAB}{TAB}annotation SummarizationSetBy = Automatic")
    if is_date:
        lines.append("")
        lines.append(f"{TAB}{TAB}annotation UnderlyingDateTimeDataType = Date")
    return "\n".join(lines)


def measure_block(m: Dict, seq: int) -> str:
    """Build a TMDL measure block from a decisions measure dict."""
    lines = []
    if m.get("description"):
        lines.append(f"{TAB}/// {m['description']}")
    dax = m["dax"].strip()
    if "\n" in dax:
        body = "\n".join(f"{TAB}{TAB}{TAB}{ln}" for ln in dax.splitlines())
        lines.append(f"{TAB}measure {quote(m['name'])} =")
        lines.append(body)
    else:
        lines.append(f"{TAB}measure {quote(m['name'])} = {dax}")
    if m.get("formatString"):
        lines.append(f"{TAB}{TAB}formatString: {safe_format_string(m['formatString'])}")
    if m.get("displayFolder"):
        lines.append(f"{TAB}{TAB}displayFolder: {m['displayFolder']}")
    lines.append(f"{TAB}{TAB}lineageTag: {lineage(seq)}")
    return "\n".join(lines)


def csv_partition(table: str, path: str, columns: List[Dict], delimiter: str = ",",
                  codepage: int = 65001) -> str:
    """Build an Import-mode CSV partition M block.

    The caller passes the detected ``delimiter`` and a column list already
    reconciled to the CSV's physical header (see emit_tmdl._reconcile_with_csv),
    so ``Columns=`` and the typed-column steps always match the file exactly.
    ``codepage`` is the Windows code page from workbook metadata (65001 = UTF-8,
    1252 = windows-1252).

    Date/datetime columns get per-row ``try Date.FromText … otherwise null``
    parsing instead of a bulk ``type date`` cast. A bulk cast makes the whole
    refresh fail if a single value (e.g. a stray blank or an odd format) cannot
    be parsed; the per-row form turns unparseable values into null and lets the
    rest of the table load. Long month-name dates like "September 9, 2019" parse
    under en-US, dd/MM/yyyy under en-GB — both are tried.
    """
    date_cols = [c for c in columns if c.get("dataType") in ("date", "datetime")]
    other_cols = [c for c in columns if c.get("dataType") not in ("date", "datetime")]
    typed = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}{{"{ c["name"]}", {_m_type(c["dataType"])}}}'
        for c in other_cols
    )
    steps = (
        f"{TAB}{TAB}{TAB}let\n"
        f'{TAB}{TAB}{TAB}{TAB}Source = Csv.Document(File.Contents("{path}"), '
        f'[Delimiter="{delimiter}", Columns={len(columns)}, Encoding={codepage}, QuoteStyle=QuoteStyle.Csv]),\n'
        f"{TAB}{TAB}{TAB}{TAB}Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])"
    )
    prev = "Promoted"
    if date_cols:
        date_transforms = ",\n".join(
            f'{TAB}{TAB}{TAB}{TAB}\t{{"{ c["name"]}", '
            + (
                'each try Date.FromText(_, "en-US") otherwise '
                'try Date.FromText(_, "en-GB") otherwise null, type date}'
                if c.get("dataType") == "date" else
                'each try DateTime.FromText(_, "en-US") otherwise '
                'try DateTime.FromText(_, "en-GB") otherwise null, type datetime}'
            )
            for c in date_cols
        )
        steps += (
            f",\n{TAB}{TAB}{TAB}{TAB}WithDates = Table.TransformColumns({prev}, {{\n"
            f"{date_transforms}\n"
            f"{TAB}{TAB}{TAB}{TAB}}})"
        )
        prev = "WithDates"
    if typed:
        steps += (
            f",\n{TAB}{TAB}{TAB}{TAB}Typed = Table.TransformColumnTypes({prev}, {{\n{typed}\n"
            f'{TAB}{TAB}{TAB}{TAB}}}, "en-US")'
        )
        prev = "Typed"
    steps += f"\n{TAB}{TAB}{TAB}in\n{TAB}{TAB}{TAB}{TAB}{prev}\n"
    return (
        f"{TAB}partition {quote(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{steps}"
    )


def detect_delimiter(path: str) -> str:
    """Sniff the field delimiter from a CSV's header line.

    Returns one of ',', ';', '\\t', '|'. Falls back to ',' when the file is
    unreadable or no candidate appears. Used by read_csv_header / read_csv_sample
    so physical-header reconciliation parses non-comma CSVs correctly.
    """
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            line = fh.readline()
    except OSError:
        return ","
    candidates = [",", ";", "\t", "|"]
    best = max(candidates, key=line.count)
    return best if line.count(best) > 0 else ","


def read_csv_header(path: str) -> List[str]:
    """Return the physical header names of a CSV, or [] if it cannot be read."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return next(_csv.reader(fh, delimiter=detect_delimiter(path)))
    except (OSError, StopIteration):
        return []


def read_csv_sample(path: str, max_rows: int = 100) -> Tuple[List[str], List[List[str]]]:
    """Return (header, sample-rows) from a CSV for light type inference."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = _csv.reader(fh, delimiter=detect_delimiter(path))
            header = next(reader)
            rows = []
            for i, row in enumerate(reader):
                if i >= max_rows:
                    break
                rows.append(row)
            return header, rows
    except (OSError, StopIteration):
        return [], []


def infer_csv_type(rows: List[List[str]], idx: int) -> str:
    """Infer an IR dataType ('integer'/'real'/'string') for one CSV column."""
    vals = [r[idx].strip() for r in rows if idx < len(r) and r[idx].strip() != ""]
    if not vals:
        return "string"
    if all(_is_int(v) for v in vals):
        return "integer"
    if all(_is_float(v) for v in vals):
        return "real"
    return "string"


def _is_int(v: str) -> bool:
    v = v.replace(",", "").strip()
    if v[:1] in ("+", "-"):
        v = v[1:]
    return v.isdigit()


def _is_float(v: str) -> bool:
    try:
        float(v.replace(",", "").strip())
        return True
    except ValueError:
        return False


def robust_csv_partition(
    table: str,
    path: str,
    columns: List[Dict],
    delimiter: str,
    date_cols: List[str],
    decimal_cols: List[str],
    codepage: int = 65001,
) -> str:
    """Build a fact-table partition for European-style CSVs (semicolon delimiter,
    dd/MM/yyyy dates, comma-decimal numbers).  Non-date/decimal columns are typed
    normally; date columns get en-GB fallback parsing; decimal columns get a
    comma→dot replacement before conversion."""
    non_special = [
        c for c in columns
        if c["name"] not in date_cols and c["name"] not in decimal_cols
    ]
    typed_base = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}\t{{"{ c["name"]}", {_m_type(c["dataType"])}}}'
        for c in non_special
    )
    date_transforms = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}\t{{"{ d}", '
        f'each try Date.FromText(_, "en-GB") otherwise try Date.FromText(_, "en-US") otherwise null, type date}}'
        for d in date_cols
    )
    decimal_transforms = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}\t{{"{ d}", '
        f'each try Number.FromText(Text.Replace(Text.From(_), ",", ".")) otherwise null, type number}}'
        for d in decimal_cols
    )
    # Build steps conditionally
    steps = (
        f"{TAB}{TAB}{TAB}let\n"
        f'{TAB}{TAB}{TAB}{TAB}Source = Csv.Document(File.Contents("{path}"), '
        f'[Delimiter="{delimiter}", Columns={len(columns)}, Encoding={codepage}, QuoteStyle=QuoteStyle.Csv]),\n'
        f"{TAB}{TAB}{TAB}{TAB}Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])"
    )
    prev = "Promoted"
    if date_transforms:
        steps += (
            f",\n{TAB}{TAB}{TAB}{TAB}WithDates = Table.TransformColumns({prev}, {{\n"
            f"{date_transforms}\n"
            f"{TAB}{TAB}{TAB}{TAB}}})")
        prev = "WithDates"
    if decimal_transforms:
        steps += (
            f",\n{TAB}{TAB}{TAB}{TAB}WithNums = Table.TransformColumns({prev}, {{\n"
            f"{decimal_transforms}\n"
            f"{TAB}{TAB}{TAB}{TAB}}})")
        prev = "WithNums"
    if typed_base:
        steps += (
            f",\n{TAB}{TAB}{TAB}{TAB}Typed = Table.TransformColumnTypes({prev}, {{\n"
            f"{typed_base}\n"
            f"{TAB}{TAB}{TAB}{TAB}}})"
        )
        prev = "Typed"
    steps += f"\n{TAB}{TAB}{TAB}in\n{TAB}{TAB}{TAB}{TAB}{prev}\n"
    return (
        f"{TAB}partition {quote(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{steps}"
    )


def raw_partition(table: str, m_body: str) -> str:
    """Build an Import-mode partition from a hand-authored M ``let … in`` body.

    Used when a single logical table is sourced from a custom Power Query (e.g.
    a join across several CSVs) that the deterministic ``csv_partition`` cannot
    express. The body is indented under ``source =`` exactly as TMDL expects.
    """
    body = "\n".join(f"{TAB}{TAB}{TAB}{ln}" for ln in m_body.strip("\n").splitlines())
    return (
        f"{TAB}partition {quote(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{body}\n"
    )


# --- Multi-source (non-CSV) partition builder --------------------------------
# Relational connectors: IR sourceType (lowercased) -> (M Source expression,
# navigation-record key). Schema-style connectors navigate with [Schema,Item];
# Name-style connectors navigate with [Name].
_DB_CONNECTOR = {
    "sqlserver":  ('Sql.Database("{server}", "{database}")', "schema"),
    "postgresql": ('PostgreSQL.Database("{server}", "{database}")', "schema"),
    "mysql":      ('MySQL.Database("{server}", "{database}")', "name"),
    "oracle":     ('Oracle.Database("{server}")', "schema"),
}
_DEFAULT_SCHEMA = {"sqlserver": "dbo", "postgresql": "public"}


def _source_head(source_type: str, source: Dict, table: str) -> Tuple[str, str]:
    """Return (indented M step lines without trailing comma, last-step name).

    Builds the connector + navigation steps for a non-CSV source. Unknown
    sources fall back to a clearly-labelled ODBC navigation the user can finish
    in Power BI Desktop.
    """
    st = source_type.lower()
    server = source.get("server") or ""
    database = source.get("database") or ""
    schema = source.get("schema") or _DEFAULT_SCHEMA.get(st, "")
    tbl = source.get("table") or table
    file = source.get("file") or ""
    sheet = source.get("sheet") or tbl
    query = source.get("query") or ""

    # Custom SQL (Tableau relation type="text") -> native query partition.
    if query and st in _DB_CONNECTOR:
        q = " ".join(query.split()).replace('"', '""')
        expr, _ = _DB_CONNECTOR[st]
        if st == "sqlserver":
            steps = [f'Source = Sql.Database("{server}", "{database}", [Query="{q}"])']
        else:
            steps = [f"Db = {expr.format(server=server, database=database)}",
                     f'Source = Value.NativeQuery(Db, "{q}")']
        return ",\n".join(f"{TAB}{TAB}{TAB}{TAB}{ln}" for ln in steps), "Source"

    if st == "excel":
        steps = [
            f'Source = Excel.Workbook(File.Contents("{file}"), null, true)',
            f'Navigation = Source{{[Item="{sheet}", Kind="Sheet"]}}[Data]',
            "Promoted = Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])",
        ]
        prev = "Promoted"
    elif st == "parquet":
        steps = [f'Source = Parquet.Document(File.Contents("{file}"))']
        prev = "Source"
    elif st in _DB_CONNECTOR:
        expr, nav_key = _DB_CONNECTOR[st]
        src = expr.format(server=server, database=database)
        if nav_key == "schema":
            nav = f'Navigation = Source{{[Schema="{schema}", Item="{tbl}"]}}[Data]'
        else:
            nav = f'Navigation = Source{{[Name="{tbl}"]}}[Data]'
        steps = [f"Source = {src}", nav]
        prev = "Navigation"
    else:
        # Generic ODBC / unrecognised source — emit a finishable stub.
        steps = [
            f'Source = Odbc.DataSource("dsn={database or server}", [HierarchicalNavigation=true])',
            f'Navigation = Source{{[Name="{tbl}"]}}[Data]',
        ]
        prev = "Navigation"

    indented = ",\n".join(f"{TAB}{TAB}{TAB}{TAB}{ln}" for ln in steps)
    return indented, prev


def source_partition(table: str, source: Dict, columns: List[Dict]) -> str:
    """Build an Import-mode partition for a NON-CSV source.

    ``source`` carries the detected connection facts:
      sourceType (excel/parquet/sqlserver/postgresql/mysql/oracle/odbc/…),
      server, database, schema, table, file, sheet.
    Routes on sourceType to the matching M connector, then applies IR column
    typing. Any unrecognised source degrades to an ODBC stub so the model still
    opens; precise/custom sources can always be supplied via ``mExpression``.
    """
    head, prev = _source_head(source.get("sourceType", ""), source, table)
    body = f"{TAB}{TAB}{TAB}let\n{head}"
    if columns:
        typed = ",\n".join(
            f'{TAB}{TAB}{TAB}{TAB}{TAB}{{"{c["name"]}", {_m_type(c.get("dataType", "string"))}}}'
            for c in columns
        )
        body += (
            f",\n{TAB}{TAB}{TAB}{TAB}Typed = Table.TransformColumnTypes({prev}, {{\n"
            f"{typed}\n{TAB}{TAB}{TAB}{TAB}}}, \"en-US\")"
        )
        prev = "Typed"
    body += f"\n{TAB}{TAB}{TAB}in\n{TAB}{TAB}{TAB}{TAB}{prev}\n"
    return (
        f"{TAB}partition {quote(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{body}"
    )


def datatable_partition(table: str, columns: List[Dict], rows: List[List]) -> str:
    """Build a disconnected DATATABLE partition (parameter selector table)."""
    coldef = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}"{c["name"]}", {c["type"].upper()}' for c in columns
    )
    rowdef = ",\n".join(
        f"{TAB}{TAB}{TAB}{TAB}{TAB}{{ " + ", ".join(_lit(v) for v in row) + " }"
        for row in rows
    )
    return (
        f"{TAB}partition {quote(table)} = calculated\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}DATATABLE (\n{coldef},\n"
        f"{TAB}{TAB}{TAB}{TAB}{{\n{rowdef}\n{TAB}{TAB}{TAB}{TAB}}}\n"
        f"{TAB}{TAB}{TAB})\n"
    )


def _lit(value) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _m_type(data_type: str) -> str:
    return {
        "integer": "Int64.Type", "real": "type number", "date": "type date",
        "datetime": "type datetime", "boolean": "type logical",
    }.get(data_type, "type text")


def dim_partition(
    table: str,
    path: str,
    delimiter: str,
    logical_key: str,
    all_columns: List[Dict],
    codepage: int = 65001,
) -> str:
    """Build a full-dimension partition: load ALL CSV columns, apply any renames
    (csv_name → logical_name), null-filter on key, deduplicate.

    Each entry in ``all_columns`` must have:
      - ``name``     – the logical (TMDL) column name
      - ``csv_name`` – the actual CSV header (may differ due to underscores etc.)
      - ``dataType`` – IR dataType string used by _m_type()
    """
    renames = [
        (c["csv_name"], c["name"])
        for c in all_columns
        if c.get("csv_name") and c["csv_name"] != c["name"]
    ]
    rename_pairs = ", ".join(f'{{"{csv}", "{log}"}}' for csv, log in renames)
    rename_step = (
        f"{TAB}{TAB}{TAB}{TAB}Renamed = Table.RenameColumns(Promoted, "
        f"{{{rename_pairs}}}, MissingField.Ignore),\n"
        if renames
        else ""
    )
    after_rename = "Renamed" if renames else "Promoted"
    typed_pairs = ",\n".join(
        f'{TAB}{TAB}{TAB}{TAB}\t{{"{c["name"]}", {_m_type(c.get("dataType", "string"))}}}'
        for c in all_columns
    )
    kref = (
        f'[#"{logical_key}"]'
        if any(ch in logical_key for ch in " '\"()")
        else f"[{logical_key}]"
    )
    return (
        f"{TAB}partition {quote(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}let\n"
        f'{TAB}{TAB}{TAB}{TAB}Source = Csv.Document(File.Contents("{path}"), '
        f'[Delimiter="{delimiter}", Encoding={codepage}, QuoteStyle=QuoteStyle.Csv]),\n'
        f"{TAB}{TAB}{TAB}{TAB}Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f"{rename_step}"
        f"{TAB}{TAB}{TAB}{TAB}Typed = Table.TransformColumnTypes({after_rename}, {{\n"
        f"{typed_pairs}\n"
        f"{TAB}{TAB}{TAB}{TAB}}}),\n"
        f"{TAB}{TAB}{TAB}{TAB}Filtered = Table.SelectRows(Typed, "
        f'each {kref} <> null and Text.Trim(Text.From({kref})) <> ""),\n'
        f'{TAB}{TAB}{TAB}{TAB}Distinct = Table.Distinct(Filtered, {{"{logical_key}"}})\n'
        f"{TAB}{TAB}{TAB}in\n"
        f"{TAB}{TAB}{TAB}{TAB}Distinct\n"
    )


def calendar_partition(table: str, fact_table: str, date_col: str) -> str:
    """Build a DAX CALENDAR calculated-table partition for a date dimension.
    Ranges from MIN to MAX of the fact table's date column."""
    return (
        f"{TAB}partition {quote(table)} = calculated\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}VAR MinDate = MIN('{fact_table}'[{date_col}])\n"
        f"{TAB}{TAB}{TAB}VAR MaxDate = MAX('{fact_table}'[{date_col}])\n"
        f"{TAB}{TAB}{TAB}RETURN CALENDAR(MinDate, MaxDate)\n"
    )


def date_key_column_block(seq: int) -> str:
    """Build the special Date key column for a CALENDAR() calculated table.
    Uses sourceColumn: [Date] (bracket form for calculated-table columns)."""
    return (
        f"{TAB}column Date\n"
        f"{TAB}{TAB}dataType: dateTime\n"
        f"{TAB}{TAB}isKey\n"
        f"{TAB}{TAB}formatString: m/d/yyyy\n"
        f"{TAB}{TAB}lineageTag: {lineage(seq)}\n"
        f"{TAB}{TAB}summarizeBy: none\n"
        f"{TAB}{TAB}sourceColumn: [Date]\n"
        f"\n"
        f"{TAB}{TAB}annotation SummarizationSetBy = Automatic\n"
        f"\n"
        f"{TAB}{TAB}annotation UnderlyingDateTimeDataType = Date"
    )
