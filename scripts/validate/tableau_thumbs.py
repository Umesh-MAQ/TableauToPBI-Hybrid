"""tableau_thumbs.py — extract REAL Tableau-rendered thumbnails from a workbook.

Every Tableau workbook embeds a ``<thumbnails>`` block whose ``<thumbnail>`` children
hold base64-encoded PNG images of each worksheet and dashboard, rendered by Tableau
Desktop at save time. Those are genuine Tableau renders (low resolution — Tableau
stores them at ~192px) and are the only *actual* Tableau images available without
driving Tableau Desktop.

This module decodes those thumbnails and writes them as ``<key>_tableau.png`` into
``validation/screenshots/`` using the same normalised-key convention the comparators
emit, so the validation workbook embeds real Tableau evidence (not a re-draw).

``.twb`` is plain XML; ``.twbx`` is a zip that contains the ``.twb`` — both are
handled. A user-supplied capture with the same filename is never overwritten.
"""
from __future__ import annotations

import base64
import io
import os
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, Optional

import screenshots as SS

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _iter_thumbnails(xml_bytes: bytes) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for thumb in root.iter("thumbnail"):
        name = thumb.get("name")
        data = (thumb.text or "").strip()
        if not name or not data:
            continue
        try:
            png = base64.b64decode("".join(data.split()))
        except (ValueError, base64.binascii.Error):
            continue
        if png[:8] == _PNG_MAGIC:
            out[name] = png
    return out


def load_thumbnails(twb_path: str) -> Dict[str, bytes]:
    """Return ``{worksheet-or-dashboard-name: PNG bytes}`` from a .twb/.twbx."""
    if not twb_path or not os.path.isfile(twb_path):
        return {}
    try:
        if twb_path.lower().endswith(".twbx"):
            with zipfile.ZipFile(twb_path) as zf:
                inner = next((n for n in zf.namelist()
                              if n.lower().endswith(".twb")), None)
                if not inner:
                    return {}
                return _iter_thumbnails(zf.read(inner))
        with open(twb_path, "rb") as fh:
            return _iter_thumbnails(fh.read())
    except (OSError, zipfile.BadZipFile):
        return {}


def _match(thumbs: Dict[str, bytes], name: Optional[str]) -> Optional[bytes]:
    if not name:
        return None
    if name in thumbs:
        return thumbs[name]
    low = {k.lower(): v for k, v in thumbs.items()}
    return low.get(name.lower())


def save_thumbnail(out_dir: str, key: str, tableau_name: str,
                   thumbs: Dict[str, bytes], overwrite: bool = False,
                   fallback_names=None) -> str:
    """Write ``<key>_tableau.png`` from the best available Tableau render.

    Preference order:
      1. the worksheet's OWN thumbnail — a genuine per-visual Tableau render
         carrying the real data (a true visual-by-visual match);
      2. any ``fallback_names`` (e.g. the dashboard the worksheet lives on) —
         a real Tableau render that *contains* the visual, used only when the
         worksheet itself has no embedded thumbnail. This is the analogue of the
         whole-page Power BI capture and beats showing a blank placeholder.

    Returns ``"own"`` if the worksheet's own thumbnail was written, ``"context"``
    if a fallback dashboard render was written, or ``""`` if nothing matched.
    Existing files (e.g. a real full-res capture the user dropped in) are never
    overwritten and report ``"own"``.
    """
    png = _match(thumbs, tableau_name)
    kind = "own"
    if png is None:
        for alt in (fallback_names or []):
            png = _match(thumbs, alt)
            if png is not None:
                kind = "context"
                break
    if png is None:
        return ""
    d = SS.ensure_dir(out_dir)
    path = os.path.join(d, f"{key}_tableau.png")
    if os.path.isfile(path) and not overwrite:
        return "own"
    with open(path, "wb") as fh:
        fh.write(png)
    return kind


def worksheet_dashboard_map(analysis: dict) -> Dict[str, str]:
    """Map each worksheet name → a dashboard it appears on (from dashboard zones).

    Lets a worksheet without its own embedded thumbnail fall back to the real
    Tableau render of the dashboard that contains it.
    """
    out: Dict[str, str] = {}

    def _walk(node):
        names = []
        if isinstance(node, dict):
            ws = node.get("worksheet")
            if ws:
                names.append(ws)
            for v in node.values():
                names += _walk(v)
        elif isinstance(node, list):
            for v in node:
                names += _walk(v)
        return names

    for dash in analysis.get("dashboards", []):
        dname = dash.get("name")
        if not dname:
            continue
        for ws in set(filter(None, _walk(dash.get("zones")))):
            out.setdefault(ws, dname)
    return out


def available_names(twb_path: str) -> Dict[str, bytes]:
    """Convenience: the thumbnail name→bytes map (for diagnostics/tests)."""
    return load_thumbnails(twb_path)
