"""rebind.py — DETERMINISTIC re-binding of a Tableau workbook's file connections
to the locally generated dummy CSVs (stage 4 of the datagen feature).

Why this stage exists
---------------------
A ``.twb`` stores, for every CSV/text/Excel source, an ABSOLUTE path on the
machine that authored it, e.g.::

    <connection class='textscan'
                directory='C:/Users/SomeoneElse/Downloads/.../Data/Loan/archive (1)'
                filename='loan.csv' password='' server='' />

On any other machine that path does not exist, so Tableau opens the workbook with
broken/"file not found" connections. ``dummy_data.py build`` already wrote CSVs
that reuse each source's ORIGINAL basename (``loan.csv``, ``customer.csv`` …), so
all we have to do to make the workbook open against the dummy data is repoint each
connection's ``directory`` (and confirm its ``filename``) at the folder that holds
those generated files.

Is this deterministic or agentic?  → 100 % DETERMINISTIC.
The generator preserves the original filename, so re-binding is a pure
basename match + attribute rewrite. There is no judgement to make, nothing to
invent, and the result is byte-for-byte reproducible. The AI is never involved.

Why a text rewrite (not ElementTree round-trip)
-----------------------------------------------
A ``.twb`` carries Tableau-specific formatting, comments and an XML declaration
that ``ElementTree`` would drop or reorder on re-serialisation (which can make
Tableau refuse the file). We therefore edit the raw XML text with targeted
attribute substitutions on just the ``<connection …>`` leaf tags, leaving every
other byte untouched.

Usage
-----
    # CSVs were written beside the workbook (dummy_data.py build --write-beside-source)
    python rebind.py "Data/Loan/Loan Portfolio Analysis.twb" --beside --in-place

    # CSVs live in the model dir's data/ folder; write a rebound copy next to the .twb
    python rebind.py "Data/Loan" --data-dir Output/datagen/LoanPortfolioAnalysis/data

    # Explicit output path
    python rebind.py "Data/Loan" --beside --out "Data/Loan/Loan (dummy).twb"
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple

# Reuse the parser helper for PascalCase model-dir resolution.
_TWB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "twb")
sys.path.insert(0, os.path.abspath(_TWB))
import twb_xml as X  # noqa: E402

# Tableau leaf connection classes that point at a single file on disk. We only
# rewrite tags that carry a ``filename`` attribute, so this set is advisory.
FILE_CONNECTION_CLASSES = {"textscan", "textclean", "excel-direct", "excel"}

# A single <connection …> opening/self-closing tag (paths never contain '>').
_CONN_TAG = re.compile(r"<connection\b[^>]*?/?>")

# A Tableau parameter is a <column …> with a param-domain-type. We reset the
# date-range ones so a date-filtered dashboard isn't left pointing at a window
# that the (freshly generated) dummy data never covers → blank visuals.
_PARAM_COL = re.compile(r"<column\b[^>]*?\bparam-domain-type=[^>]*?>")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# A range parameter also carries a <calculation class='tableau' formula='#date#'/>
# child that calc fields, titles and filters evaluate. We keep it in sync with
# the column's value so the old window can't leak back in via a calculation.
_PARAM_BLOCK = re.compile(
    r"(<column\b[^>]*?\bparam-domain-type=[^>]*?>)"
    r"(\s*<calculation\b[^>]*?\bformula=)(['\"])(.*?)\3",
    re.DOTALL,
)

# Caption keywords that mark a range parameter as the lower vs upper bound.
_START_WORDS = ("start", "from", "begin", "since", "min", "earliest", "after")
_END_WORDS = ("end", "thru", "through", "until", "to", "max", "latest", "before")

# A datasource may be materialised into a .hyper <extract enabled='true'>. If that
# extract file is missing (it lives on the author's machine), Tableau pops a
# "Locate Extract" dialog and the dashboard goes unavailable — even though we have
# rebound the live CSV connection. Disabling the extract makes Tableau fall back
# to the live (rebound) connection, so the workbook opens on our dummy data.
_EXTRACT_TAG = re.compile(r"<extract\b[^>]*?>")


def _find_twb(target: str) -> str:
    """Resolve a .twb path from either a direct file or a folder."""
    if os.path.isfile(target) and target.lower().endswith((".twb", ".twbx")):
        return target
    if os.path.isdir(target):
        for fn in sorted(os.listdir(target)):
            if fn.lower().endswith(".twb"):
                return os.path.join(target, fn)
    raise FileNotFoundError(f"No .twb workbook found at: {target}")


def _xml_attr_escape(value: str) -> str:
    """Escape a value for use inside a single-quoted XML attribute."""
    return (value.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace("'", "&apos;"))


def _get_attr(tag: str, name: str) -> Optional[str]:
    """Read an attribute value from a tag string (handles ' or " quoting)."""
    m = re.search(rf"\b{re.escape(name)}=(['\"])(.*?)\1", tag)
    return m.group(2) if m else None


def _set_attr(tag: str, name: str, value: str) -> str:
    """Replace an existing attribute's value, or insert it after ``<connection``.

    The inserted/!replaced value is XML-escaped and single-quoted.
    """
    esc = _xml_attr_escape(value)
    pat = re.compile(rf"(\b{re.escape(name)}=)(['\"])(.*?)\2")
    if pat.search(tag):
        return pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{esc}{m.group(2)}", tag, count=1)
    # Not present — insert right after the element name.
    return re.sub(r"(<connection\b)", rf"\1 {name}='{esc}'", tag, count=1)


def _available_files(data_dir: str) -> Dict[str, str]:
    """Map lower-cased basename -> actual filename for every file in ``data_dir``."""
    if not os.path.isdir(data_dir):
        return {}
    return {fn.lower(): fn for fn in os.listdir(data_dir)
            if os.path.isfile(os.path.join(data_dir, fn))}


def rebind_text(xml_text: str, data_dir: str,
                available: Optional[Dict[str, str]] = None) -> Tuple[str, List[Dict]]:
    """Repoint every file connection at ``data_dir``; return (new_text, changes).

    A connection is rewritten only when a generated file with the same basename
    exists in ``data_dir`` (case-insensitive). Unmatched connections (e.g. an
    Excel source with no generated CSV) are left untouched and reported.
    """
    if available is None:
        available = _available_files(data_dir)
    abs_dir = os.path.abspath(data_dir).replace("\\", "/")
    changes: List[Dict] = []

    def _rewrite(match: "re.Match") -> str:
        tag = match.group(0)
        filename = _get_attr(tag, "filename")
        if not filename:  # not a file (leaf) connection — e.g. a federated wrapper
            return tag
        base = os.path.basename(filename)
        actual = available.get(base.lower())
        cls = _get_attr(tag, "class") or ""
        old_dir = _get_attr(tag, "directory")
        if actual is None:
            changes.append({"filename": base, "class": cls, "status": "skipped",
                            "reason": "no generated file with this name", "oldDir": old_dir})
            return tag
        new_tag = _set_attr(tag, "directory", abs_dir)
        new_tag = _set_attr(new_tag, "filename", actual)
        changes.append({"filename": actual, "class": cls, "status": "rebound",
                        "oldDir": old_dir, "newDir": abs_dir})
        return new_tag

    new_text = _CONN_TAG.sub(_rewrite, xml_text)
    return new_text, changes


def reset_date_parameters(xml_text: str, date_min: str, date_max: str) -> Tuple[str, List[Dict]]:
    """Repoint date-range parameters at the generated data window [min, max].

    A dashboard often filters a date field with ``[date] >= [Start param] AND
    [date] <= [End param]``. If those parameters were saved at values outside the
    dummy data's date range, every row is filtered out and the visuals go blank.
    We rewrite each date range parameter's ``value`` so the lower-bound (Start)
    parameter becomes ``date_min`` and the upper-bound (End) becomes ``date_max``,
    and keep the parameter's ``<calculation>`` formula in sync so the stale window
    can't reappear through a calculated field, title or filter.
    Only date-typed range parameters whose caption clearly names a bound are
    touched; everything else is left exactly as-is.
    """
    changes: List[Dict] = []

    def _bound(col_tag: str):
        """Return (caption, current_value, new_value) for a date range bound, else None."""
        if (_get_attr(col_tag, "datatype") or "").lower() != "date":
            return None
        val = _get_attr(col_tag, "value")
        if not val or "#" not in val:
            return None
        caption = (_get_attr(col_tag, "caption") or "").lower()
        if any(w in caption for w in _END_WORDS):
            return caption, val, f"#{date_max}#"
        if any(w in caption for w in _START_WORDS):
            return caption, val, f"#{date_min}#"
        return None  # bound is ambiguous — don't guess

    # Pass 1 — the current value shown on the parameter control.
    def _rewrite(match: "re.Match") -> str:
        tag = match.group(0)
        b = _bound(tag)
        if not b:
            return tag
        caption, val, new_val = b
        if new_val == val:
            return tag
        changes.append({"caption": caption or "(date param)", "old": val, "new": new_val})
        return _set_attr(tag, "value", new_val)

    text = _PARAM_COL.sub(_rewrite, xml_text)

    # Pass 2 — the parameter's calculation formula (runs on the updated text).
    def _sync_calc(match: "re.Match") -> str:
        col_tag, prefix, quote, formula = match.group(1, 2, 3, 4)
        b = _bound(col_tag)
        if not b or not formula.strip().startswith("#"):
            return match.group(0)
        new_val = b[2]
        return f"{col_tag}{prefix}{quote}{new_val}{quote}"

    return _PARAM_BLOCK.sub(_sync_calc, text), changes


def disable_extracts(xml_text: str) -> Tuple[str, int]:
    """Switch every enabled .hyper extract to live so the rebound CSV is used.

    A workbook saved "with extract" stores ``<extract enabled='true'>`` plus a
    ``<connection class='hyper' dbname='…author machine…'>``. On another machine
    that .hyper is missing, so Tableau demands it ("Locate Extract") and the
    dashboard fails — ignoring the live CSV connection we just rebound. Flipping
    the extract to ``enabled='false'`` makes Tableau use the live connection.
    Returns (new_text, number_of_extracts_disabled).
    """
    count = 0

    def _rewrite(match: "re.Match") -> str:
        nonlocal count
        tag = match.group(0)
        if (_get_attr(tag, "enabled") or "").lower() != "true":
            return tag
        count += 1
        return _set_attr(tag, "enabled", "false")

    return _EXTRACT_TAG.sub(_rewrite, xml_text), count


def _resolve_data_dir(twb_path: str, data_dir: Optional[str], beside: bool,
                      out_root: str) -> str:
    """Pick the folder that holds the generated CSVs."""
    if data_dir:
        return data_dir
    if beside:
        return os.path.dirname(os.path.abspath(twb_path))
    pascal = X.to_pascal_case(os.path.splitext(os.path.basename(twb_path))[0])
    return os.path.join(out_root, pascal, "data")


def run(target: str, data_dir: Optional[str] = None, *, beside: bool = False,
        in_place: bool = False, out: Optional[str] = None,
        out_root: str = "Output/datagen",
        date_range: Optional[Tuple[str, str]] = None) -> Dict:
    """Rebind a workbook's connections to local dummy CSVs. Returns a result dict.

    If ``date_range`` (min_iso, max_iso) is given, date-range parameters are also
    reset to that window so date-filtered dashboards still render on the dummy data.
    """
    twb_path = _find_twb(target)
    ddir = _resolve_data_dir(twb_path, data_dir, beside, out_root)
    available = _available_files(ddir)
    if not available:
        raise FileNotFoundError(
            f"No generated data files found in: {ddir}\n"
            f"Run dummy_data.py build first (optionally --write-beside-source).")

    with open(twb_path, encoding="utf-8") as fh:
        original = fh.read()
    new_text, changes = rebind_text(original, ddir, available)

    param_changes: List[Dict] = []
    if date_range and date_range[0] and date_range[1]:
        new_text, param_changes = reset_date_parameters(new_text, date_range[0], date_range[1])

    new_text, extracts_disabled = disable_extracts(new_text)

    if out:
        out_path = out
    elif in_place:
        out_path = twb_path
        backup = twb_path + ".bak"
        if not os.path.exists(backup):  # keep the very first original safe
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(original)
    else:
        stem, ext = os.path.splitext(twb_path)
        out_path = f"{stem} (dummy){ext}"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)

    rebound = [c for c in changes if c["status"] == "rebound"]
    skipped = [c for c in changes if c["status"] == "skipped"]
    return {
        "twb": twb_path,
        "dataDir": os.path.abspath(ddir).replace("\\", "/"),
        "outPath": out_path,
        "reboundCount": len(rebound),
        "skippedCount": len(skipped),
        "changes": changes,
        "dateParams": param_changes,
        "extractsDisabled": extracts_disabled,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Re-bind a Tableau workbook's file connections to locally generated dummy CSVs.")
    ap.add_argument("target", help="Path to a .twb file or a folder containing one.")
    ap.add_argument("--data-dir", help="Folder holding the generated CSVs "
                    "(default: Output/datagen/<PascalName>/data).")
    ap.add_argument("--beside", action="store_true",
                    help="The CSVs sit next to the .twb (dummy_data build --write-beside-source).")
    ap.add_argument("--in-place", action="store_true",
                    help="Overwrite the original .twb (a one-time .twb.bak is kept).")
    ap.add_argument("--out", help="Explicit output .twb path.")
    ap.add_argument("--out-root", default="Output/datagen", help="Datagen output root.")
    args = ap.parse_args(argv)

    res = run(args.target, args.data_dir, beside=args.beside,
              in_place=args.in_place, out=args.out, out_root=args.out_root)
    print(f"[rebind] data dir : {res['dataDir']}")
    print(f"[rebind] rebound  : {res['reboundCount']} connection(s)")
    for c in res["changes"]:
        mark = "OK " if c["status"] == "rebound" else "-- "
        extra = "" if c["status"] == "rebound" else f"  ({c['reason']})"
        print(f"  {mark}{c['filename']} [{c['class']}]{extra}")
    if res["skippedCount"]:
        print(f"[rebind] skipped  : {res['skippedCount']} connection(s) (no matching CSV)")
    for p in res.get("dateParams", []):
        print(f"  ~~ date param '{p['caption']}': {p['old']} -> {p['new']}")
    if res.get("extractsDisabled"):
        print(f"[rebind] extracts : disabled {res['extractsDisabled']} "
              f".hyper extract(s) -> using live CSV")
    print(f"[rebind] wrote    : {res['outPath']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
