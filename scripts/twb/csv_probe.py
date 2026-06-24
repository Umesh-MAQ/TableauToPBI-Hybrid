"""csv_probe.py — Detect CSV delimiter and column headers from the first line.

Used by emit_tmdl.py at generation time so partitions always use the correct
delimiter and actual CSV header names — no hardcoded "," assumptions.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

# Candidates ordered by specificity (semicolon before comma because comma
# appears inside comma-decimal numbers, causing false positive splits).
CANDIDATE_DELIMITERS = [";", "\t", "|", ","]


def detect(path: str) -> Tuple[str, List[str]]:
    """Return (delimiter, [header_names]) for a CSV file.

    Reads only the first line — fast and safe on large files.
    Returns (',', []) if the file cannot be opened.
    """
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            first = fh.readline().rstrip("\r\n")
    except OSError:
        return ",", []

    for delim in CANDIDATE_DELIMITERS:
        parts = first.split(delim)
        if len(parts) >= 2:
            return delim, [h.strip().strip('"') for h in parts]

    # Single-column CSV or unknown format — return as-is with comma
    return ",", [first.strip()]


def probe(path: str) -> Optional[Dict]:
    """Probe a CSV file and return {delimiter, headers, path} or None if not found."""
    if not os.path.isfile(path):
        return None
    delimiter, headers = detect(path)
    return {"delimiter": delimiter, "headers": headers, "path": os.path.abspath(path)}


def _norm_header(s: str) -> str:
    """Collapse underscores/slashes/hyphens/spaces to one space, lowercase.

    Lets a CSV header (``date_added``) match a logical field name (``date added``)
    the same way the model emitter's reconciliation does.
    """
    import re
    return re.sub(r"[\s_/\\-]+", " ", (s or "").strip()).strip().lower()


def distinct_count(path: str, header: str) -> int:
    """Count distinct non-empty values of one column in a CSV.

    Reads the whole file (callers use it on the modest CSVs these workbooks ship).
    The column is matched to a physical header by the same loose normalization
    ``detect`` strips with. Returns 0 when the file or column cannot be read — the
    caller treats 0 as "do not special-case this worksheet".
    """
    import csv as _csv
    delimiter, _headers = detect(path)
    target_norm = _norm_header(header)
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
            reader = _csv.DictReader(fh, delimiter=delimiter)
            field = next((f for f in (reader.fieldnames or [])
                          if _norm_header(f) == target_norm), None)
            if field is None:
                return 0
            seen = set()
            for row in reader:
                value = (row.get(field) or "").strip()
                if value:
                    seen.add(value)
            return len(seen)
    except OSError:
        return 0
