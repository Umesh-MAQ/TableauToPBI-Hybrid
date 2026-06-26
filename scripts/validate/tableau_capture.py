"""tableau_capture.py — capture REAL per-worksheet screenshots from Tableau Desktop.

Tableau Desktop has no headless/CLI way to export a worksheet image, so the only
genuine Tableau screenshot is one taken from Tableau Desktop itself. This module,
the Tableau analogue of ``powerbi_capture``:

  1. writes a throwaway copy of the ``.twb`` beside the original with
       * every text/CSV connection re-pointed at the data files that actually sit
         in the workbook's own folder (workbooks saved elsewhere otherwise open
         with a broken "data source unavailable" connection and render nothing),
       * ``maximized='true'`` moved onto ONE target worksheet so Tableau opens
         showing that sheet (the analogue of Power BI's per-page relaunch);
  2. launches Tableau Desktop on that copy (via the file association),
  3. waits for the window to finish its first render, switches to presentation
     mode (F7 — hides every pane/toolbar so only the viz fills the screen),
  4. captures the window with the Win32 ``PrintWindow`` API, and
  5. terminates Tableau and deletes the temp copy.

Each worksheet is captured at full-screen resolution (far sharper than the ~192px
thumbnails Tableau embeds in the ``.twb``) with no dashboard clipping. Tableau
Desktop is relaunched for every requested worksheet so the capture is always
taken fresh from the live report — a previously-saved image is never reused.

Windows + Tableau Desktop + Pillow only — no third-party automation libraries.
If anything is missing/blocks (Tableau not installed, a sign-in/locate-file
prompt, not Windows) the caller falls back to the embedded thumbnail or a
labelled placeholder.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Dict, List, Optional

import screenshots as SS

# Reuse the Win32 capture/window plumbing already written for Power BI.
from powerbi_capture import (user32, _window_rect, _pid_of, _process_name,
                             _EnumWindowsProc, _capture_window, _force_foreground,
                             _norm)

VK_F7 = 0x76
KEYEVENTF_KEYUP = 0x2


# --------------------------------------------------------------------------- #
# window / process discovery
# --------------------------------------------------------------------------- #
def _tableau_windows() -> List[int]:
    """All currently-visible, real (>=600x400) tableau.exe top-level windows."""
    found: List[int] = []

    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        r = _window_rect(hwnd)
        if (r.right - r.left) < 600 or (r.bottom - r.top) < 400:
            return True
        if _process_name(_pid_of(hwnd)).lower() == "tableau.exe":
            found.append(hwnd)
        return True

    user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    return found


def _kill_tableau() -> None:
    """Force-close every Tableau Desktop process (temp workbooks are throwaway).

    Mirrors how the validate wrapper clears PBIDesktop before a capture — the
    capture assumes a clean slate, so any leftover Tableau window is removed.
    """
    try:
        subprocess.run(["taskkill", "/F", "/IM", "tableau.exe"],
                       capture_output=True, check=False)
    except (OSError, ValueError):
        pass
    time.sleep(2)


# --------------------------------------------------------------------------- #
# temp workbook authoring (repoint data + select the worksheet to show)
# --------------------------------------------------------------------------- #
def _make_temp_twb(twb_path: str, sheet: str, tmp_path: str) -> bool:
    """Write ``tmp_path``: a copy of ``twb_path`` with data re-pointed locally and
    ``maximized='true'`` on ``sheet``. Returns False if the source can't be read."""
    try:
        with open(twb_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False

    twb_dir = os.path.dirname(twb_path)
    local = twb_dir.replace("\\", "/")

    def _fix_dir(m):
        fn = m.group("fn")
        if os.path.isfile(os.path.join(twb_dir, fn)):
            return f"directory='{local}' filename='{fn}'"
        return m.group(0)

    text = re.sub(r"directory='[^']*'\s+filename='(?P<fn>[^']+)'", _fix_dir, text)

    def _fix_csv(m):
        fn = os.path.basename(m.group(1))
        if os.path.isfile(os.path.join(twb_dir, fn)):
            return f"csvFile='{local}/{fn}'"
        return m.group(0)

    text = re.sub(r"csvFile='([^']+)'", _fix_csv, text)

    # show the target worksheet on open: strip any existing maximized flag, then
    # mark this worksheet's window maximized.
    text = re.sub(r"(<window class='[^']*') maximized='true'( name='[^']*')",
                  r"\1\2", text)
    needle = f"<window class='worksheet' name='{sheet}'>"
    if needle in text:
        text = text.replace(
            needle, f"<window class='worksheet' maximized='true' name='{sheet}'>")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# launch + capture
# --------------------------------------------------------------------------- #
def _launch_and_wait(twb_path: str, launch_timeout: int) -> Optional[int]:
    pre = set(_tableau_windows())
    try:
        os.startfile(twb_path)  # noqa: S606 (intended app launch)
    except OSError:
        return None
    deadline = time.time() + launch_timeout
    while time.time() < deadline:
        new = [w for w in _tableau_windows() if w not in pre]
        if new:
            return new[0]
        time.sleep(2)
    return None


def _present_and_capture(hwnd: int, render_settle: int):
    """Maximize, let the viz render, switch to presentation mode, capture."""
    _force_foreground(hwnd)
    time.sleep(render_settle)
    user32.keybd_event(VK_F7, 0, 0, 0)              # F7 -> presentation mode
    user32.keybd_event(VK_F7, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(6)                                   # let panes collapse + redraw
    return _capture_window(hwnd)


def _trim_border(img):
    """Trim a uniform 1-colour outer border (the presentation-mode window frame)
    so the saved image is just the worksheet. Conservative: only trims edges that
    are a single solid colour, never more than a small margin."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return img
    try:
        w, h = img.size
        bg = img.getpixel((0, 0))          # presumed border colour
        bg_img = Image.new(img.mode, img.size, bg)
        diff = ImageChops.difference(img, bg_img)
        bbox = diff.getbbox()
    except Exception:  # noqa: BLE001 — trimming is best-effort cosmetic
        return img
    if not bbox:
        return img
    l, t, r, b = bbox
    # only trim if the border is thin (< 6% of each dimension); otherwise the
    # corner colour was content, not a frame.
    if l > w * 0.06 or t > h * 0.06 or (w - r) > w * 0.06 or (h - b) > h * 0.06:
        return img
    return img.crop(bbox)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def capture_report(out_dir: str, twb_path: str, model_name: str,
                   worksheets: List[str], *, launch_timeout: int = 120,
                   render_settle: int = 16) -> Dict[str, str]:
    """Capture each named worksheet from Tableau Desktop.

    Returns ``{worksheet_name: page_<norm>_tableau.png path}``. Tableau Desktop is
    relaunched for EVERY requested worksheet so the capture is always taken fresh
    from the live report — no previously-saved image is reused. Best-effort: a
    worksheet that fails to capture is simply absent from the result.
    """
    if os.name != "nt" or not twb_path or not os.path.isfile(twb_path):
        return {}
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return {}

    screens = SS.ensure_dir(out_dir)
    twb_dir = os.path.dirname(os.path.abspath(twb_path))
    tmp_path = os.path.join(twb_dir, "__tabcap_tmp.twb")

    out: Dict[str, str] = {}
    pending: List[str] = []
    for ws in worksheets:
        if not ws or ws == "—":
            continue
        if ws not in pending:
            pending.append(ws)   # always recapture — never reuse a cached image

    if not pending:
        return out

    print(f"  launching Tableau Desktop to capture {len(pending)} worksheet(s) — "
          "this relaunches once per sheet (set TAB_CAPTURE=0 to skip)…")
    _kill_tableau()
    try:
        for ws in pending:
            try:
                if not _make_temp_twb(twb_path, ws, tmp_path):
                    continue
                hwnd = _launch_and_wait(tmp_path, launch_timeout)
                if hwnd is None:
                    print(f"    · {ws}: Tableau window never appeared — skipped")
                    _kill_tableau()
                    continue
                img = _present_and_capture(hwnd, render_settle)
                _kill_tableau()
                if img is None:
                    print(f"    · {ws}: capture failed — skipped")
                    continue
                img = _trim_border(img)
                cache = os.path.join(screens, f"page_{_norm(ws)}_tableau.png")
                img.save(cache)
                out[ws] = cache
                print(f"    · {ws}: captured {img.size[0]}x{img.size[1]}")
            except Exception as exc:  # noqa: BLE001 — capture is best-effort
                print(f"    · {ws}: capture error ({exc}) — skipped")
                _kill_tableau()
    finally:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    return out
