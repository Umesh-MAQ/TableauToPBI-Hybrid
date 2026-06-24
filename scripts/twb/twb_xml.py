"""twb_xml.py — XML loading and shared helpers for the Tableau parser.

Deterministic utilities used by every extraction module. No LLM involved.
Tableau .twb files are XML; ElementTree decodes standard entities automatically,
but Tableau also emits literal &#13;&#10; inside formulas which we normalise here.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

# Tableau connection class -> resolved IR sourceType
SOURCE_TYPE_MAP = {
    "textscan": "CSV",
    "textclean": "CSV",
    "excel-direct": "Excel",
    "excel": "Excel",
    "sqlserver": "SqlServer",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "oracle": "Oracle",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "bigquery": "BigQuery",
    "hyper": "Hyper",
}

# Tableau datatype -> IR dataType
DATATYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "real": "real",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
}


def load_twb(path: str) -> ET.Element:
    """Parse a .twb file and return the root <workbook> element."""
    tree = ET.parse(path)
    return tree.getroot()


def decode_entities(text: Optional[str]) -> str:
    """Normalise residual Tableau escape sequences in formulas/captions."""
    if not text:
        return ""
    out = text.replace("&#13;&#10;", "\n").replace("&#10;", "\n").replace("&#13;", "\n")
    # ElementTree already decodes &quot; &gt; &lt; &amp; — guard for raw leftovers
    out = (out.replace("&quot;", '"').replace("&gt;", ">")
              .replace("&lt;", "<").replace("&amp;", "&"))
    return out


def strip_brackets(name: Optional[str]) -> str:
    """Turn Tableau internal names like [Census Date] into Census Date."""
    if not name:
        return ""
    name = name.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    return name


# Tableau caption/title runs embed icon glyphs (e.g. Æ separators) and dynamic
# field tokens like <[Parameters].[Parameter 3]>. Strip both for clean text.
_DYNAMIC_TOKEN = re.compile(r"<[^>]*>")
_ICON_GLYPHS = {chr(0x00C6), chr(0x00E6), chr(0x2022), chr(0x25CF), chr(0x25A0)}


def clean_text(text: Optional[str]) -> str:
    """Remove Tableau icon glyphs / dynamic tokens and tidy whitespace."""
    if not text:
        return ""
    out = _DYNAMIC_TOKEN.sub("", text)
    out = "".join(
        ch for ch in out
        if ch not in _ICON_GLYPHS and not (0xE000 <= ord(ch) <= 0xF8FF)
    )
    lines = [ln.strip(" \t-–—|") for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def resolve_source_type(connection_class: Optional[str]) -> str:
    """Map a raw Tableau connection class to an IR sourceType."""
    if not connection_class:
        return "Unknown"
    return SOURCE_TYPE_MAP.get(connection_class.lower(), "ODBC")


def resolve_datatype(raw: Optional[str]) -> str:
    """Map a raw Tableau datatype to the IR dataType enum."""
    if not raw:
        return "string"
    return DATATYPE_MAP.get(raw.lower(), "string")


def to_pascal_case(text: str) -> str:
    """Convert an arbitrary workbook name to PascalCase (model/folder name)."""
    words = re.split(r"[^0-9A-Za-z]+", text)
    pascal = "".join(w[:1].upper() + w[1:] for w in words if w)
    return pascal or "Model"


def findall(elem: Optional[ET.Element], tag: str) -> Iterable[ET.Element]:
    """Safe findall that tolerates a None parent."""
    if elem is None:
        return []
    return elem.findall(tag)


def find(elem: Optional[ET.Element], tag: str) -> Optional[ET.Element]:
    """Safe find that tolerates a None parent."""
    if elem is None:
        return None
    return elem.find(tag)


def attr(elem: Optional[ET.Element], name: str, default: Optional[str] = None) -> Optional[str]:
    """Safe attribute getter."""
    if elem is None:
        return default
    return elem.get(name, default)


def int_attr(elem: Optional[ET.Element], name: str, default: int = 0) -> int:
    """Attribute getter coerced to int (Tableau zone coordinates)."""
    raw = attr(elem, name)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def datasource_caption(ds: ET.Element) -> str:
    """Best display caption for a datasource element."""
    return attr(ds, "caption") or attr(ds, "name") or "Unknown"


def iter_datasources(root: ET.Element) -> Iterable[ET.Element]:
    """Yield only top-level workbook datasources (not worksheet references)."""
    container = root.find("datasources")
    if container is None:
        return []
    return container.findall("datasource")
