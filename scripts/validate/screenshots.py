"""screenshots.py — discover and embed side-by-side screenshot evidence.

Screenshots live in a conventional folder so the workbook is a standalone
evidence document (requirement #6) without any external app automation:

    Output/<Model>/validation/screenshots/

Naming convention (case-insensitive, any of these suffixes recognised):

    <key>_tableau.png      <key>_tab.png       <key>_t.png
    <key>_powerbi.png      <key>_pbi.png       <key>_p.png

``<key>`` is the normalised worksheet / filter / parameter / dashboard key the
comparators emit (e.g. ``filter_region``, ``param_metric``, ``netflixbycountry``).
When a capture is absent the workbook inserts a clearly labelled placeholder cell
that names the exact file to drop in, so evidence can be back-filled in a later
iteration without regenerating anything.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

SCREENSHOT_SUBDIR = os.path.join("validation", "screenshots")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")
_TABLEAU_SUFFIXES = ("_tableau", "_tab", "_t")
_POWERBI_SUFFIXES = ("_powerbi", "_pbi", "_pb", "_p")


def screenshots_dir(out_dir: str) -> str:
    return os.path.join(out_dir, SCREENSHOT_SUBDIR)


def ensure_dir(out_dir: str) -> str:
    d = screenshots_dir(out_dir)
    os.makedirs(d, exist_ok=True)
    return d


def _index(screens_dir: str) -> Dict[str, str]:
    """Map a lowercased filename-stem to its absolute path (one scan)."""
    idx: Dict[str, str] = {}
    if not os.path.isdir(screens_dir):
        return idx
    for fn in os.listdir(screens_dir):
        stem, ext = os.path.splitext(fn)
        if ext.lower() in _IMG_EXTS:
            idx[stem.lower()] = os.path.join(screens_dir, fn)
    return idx


def _find(idx: Dict[str, str], key: str, suffixes) -> Optional[str]:
    key = (key or "").lower()
    for suf in suffixes:
        hit = idx.get(f"{key}{suf}")
        if hit:
            return hit
    return None


def find_pair(out_dir: str, key: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (tableau_image_path, powerbi_image_path) for a key, None if absent."""
    idx = _index(screenshots_dir(out_dir))
    return (_find(idx, key, _TABLEAU_SUFFIXES),
            _find(idx, key, _POWERBI_SUFFIXES))


def expected_names(key: str) -> Tuple[str, str]:
    """The exact filenames to drop in for a missing pair (Tableau, Power BI)."""
    return (f"{key}_tableau.png", f"{key}_powerbi.png")
