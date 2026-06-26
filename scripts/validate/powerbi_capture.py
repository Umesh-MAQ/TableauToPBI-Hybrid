"""powerbi_capture.py — capture REAL Power BI report screenshots from a .pbip.

There is no headless/CLI way to render a Power BI ``.pbip`` to an image, so the
only genuine Power BI screenshot is one taken from Power BI Desktop itself. This
module:

  1. launches Power BI Desktop on the ``.pbip`` (via the file association),
  2. waits for the report window to appear and finish its first render,
  3. captures the window bitmap with the Win32 ``PrintWindow`` API
     (``PW_RENDERFULLCONTENT`` so the GPU-accelerated report canvas is included),
  4. writes one PNG per report *page* (Power BI renders whole pages, not single
     visuals), and
  5. closes Power BI Desktop.

Each visual/filter that lives on a page reuses that page's real capture as its
Power BI evidence. A user-supplied capture with the same filename is never
overwritten, so a manual per-visual crop can override the page capture.

Windows + Power BI Desktop + Pillow only (all already required). No third-party
automation libraries. If anything is missing/blocks (e.g. a sign-in prompt) the
caller falls back to a labelled placeholder.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from typing import Dict, List, Optional

import screenshots as SS

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# PrintWindow flags
PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2  # required for DirectComposition/GPU report canvas

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


# --------------------------------------------------------------------------- #
# window discovery
# --------------------------------------------------------------------------- #
_EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_rect(hwnd: int) -> wt.RECT:
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def _pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _find_pbi_window(report_hint: str = "") -> Optional[int]:
    """Return the HWND of the Power BI Desktop *report* window for this report.

    Power BI Desktop titles its main window with just the report name (e.g.
    ``NetfixWorkbook``) — not ``... - Power BI Desktop``. We only accept a window
    that (a) is owned by a ``PBIDesktop.exe`` process, (b) is a real top-level
    report window (>= 600x400, so tooltips/splash/notification popups like the
    143x18 frame we saw are rejected), and (c) has the report name in its title.

    Crucially we do NOT fall back to "any Power BI window" — when several reports
    are open (Netflix, Sales, …) a generic match would capture the wrong report,
    so callers that need a window for a freshly launched report should use
    ``_pbi_report_windows`` and diff against a pre-launch snapshot instead.
    """
    best: List[int] = []
    hint = _norm(report_hint)
    if not hint:
        return None

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        r = _window_rect(hwnd)
        w, h = r.right - r.left, r.bottom - r.top
        if w < 600 or h < 400:           # skip tooltips/splash/popups
            return True
        if _process_name(_pid_of(hwnd)).lower() != "pbidesktop.exe":
            return True
        if hint in _norm(_window_title(hwnd)):
            best.append(hwnd)
        return True

    user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    return best[0] if best else None


def _pbi_report_windows() -> List[int]:
    """All currently-visible, real (>=600x400) PBIDesktop report windows."""
    found: List[int] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        r = _window_rect(hwnd)
        if (r.right - r.left) < 600 or (r.bottom - r.top) < 400:
            return True
        if _process_name(_pid_of(hwnd)).lower() == "pbidesktop.exe":
            found.append(hwnd)
        return True

    user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    return found


def _client_rect(hwnd: int) -> wt.RECT:
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect


# --------------------------------------------------------------------------- #
# bitmap capture (PrintWindow -> Pillow image)
# --------------------------------------------------------------------------- #
def _capture_window(hwnd: int):
    from PIL import Image  # local import so the module loads without Pillow

    rect = _client_rect(hwnd)
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    hdc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem_dc, bmp)

    ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0  # BI_RGB

    buf_len = w * h * 4
    buffer = (ctypes.c_char * buf_len)()
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buffer, ctypes.byref(bmi),
                    DIB_RGB_COLORS)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hdc)

    if not ok:
        return None
    img = Image.frombuffer("RGBA", (w, h), buffer, "raw", "BGRA", 0, 1)
    return img.convert("RGB")


def _looks_blank(img) -> bool:
    """Heuristic: a still-loading PBI canvas is almost uniformly one colour."""
    small = img.resize((40, 40))
    colors = small.getcolors(40 * 40) or []
    if not colors:
        return False
    dominant = max(c[0] for c in colors)
    return dominant > (40 * 40 * 0.97)


# --------------------------------------------------------------------------- #
# full-page capture (scroll + stitch at native resolution)
#
# Power BI Desktop renders the page Fit-to-Width (it ignores the report's
# ``FitToPage`` setting in authoring view), so a page taller than the canvas
# viewport overflows and only its top is captured — the GPU canvas composes only
# the on-screen region. To capture the WHOLE page at full (readable) resolution
# we scroll the canvas down a little at a time and stitch the tiles into one tall
# page image. Anchors come from the sentinel-painted outspace: the first tile is
# anchored at the page top (sentinel ABOVE the page), and the page bottom is
# reached when sentinel reappears BELOW the page in a tile. Middle tiles are
# aligned by matching their overlap with the running strip. As a last resort
# (e.g. scrolling produced nothing) the page is widened so Fit-to-Width shrinks
# the whole height into one viewport — lower resolution but complete.
# --------------------------------------------------------------------------- #
MOUSEEVENTF_WHEEL = 0x0800
VK_HOME = 0x24
VK_NEXT = 0x22                      # Page Down
VK_CONTROL = 0x11
KEYEVENTF_KEYUP = 0x0002


def _ctrl_wheel(hwnd: int, sx: int, sy: int, notches: int) -> None:
    """Ctrl+wheel at a screen point — Power BI Desktop zooms the report canvas.

    Zooming is UNIFORM (unlike widening the page), so the whole page can be made
    to fit the viewport while every visual keeps its aspect ratio and relative
    position, which is what lets per-visual crops stay aligned. One call = one
    zoom step (Power BI snaps to discrete zoom levels).
    """
    _force_foreground(hwnd)
    user32.SetCursorPos(int(sx), int(sy))
    time.sleep(0.15)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_WHEEL, int(sx), int(sy), int(notches) * 120, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)



def _client_to_screen(hwnd: int, x: int, y: int) -> tuple:
    pt = wt.POINT(int(x), int(y))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def _wheel(hwnd: int, sx: int, sy: int, notches: int) -> None:
    """Wheel-scroll the canvas at screen point (negative notches = down).

    Power BI's report canvas is a child window (DirectComposition surface) that
    only scrolls when the wheel message reaches it. ``mouse_event`` posts to the
    focused control, which is often a visual that eats the scroll, so we also
    PostMessage ``WM_MOUSEWHEEL`` straight to the deepest child window under the
    point — that reliably scrolls the page itself.
    """
    _force_foreground(hwnd)
    user32.SetCursorPos(int(sx), int(sy))
    time.sleep(0.15)
    delta = int(notches) * 120
    # 1) classic synthesized wheel at the cursor
    user32.mouse_event(MOUSEEVENTF_WHEEL, int(sx), int(sy), delta, 0)
    # 2) targeted wheel to the child window under the point
    pt = wt.POINT(int(sx), int(sy))
    child = user32.WindowFromPoint(pt)
    if child:
        wparam = (delta & 0xFFFF) << 16
        lparam = (int(sy) << 16) | (int(sx) & 0xFFFF)
        user32.PostMessageW(child, 0x020A, wparam, lparam)  # WM_MOUSEWHEEL
    time.sleep(0.05)


def _widen_target(img, page_w: float, page_h: float) -> Optional[float]:
    """Logical page width that makes the full page height fit the viewport.

    Power BI fits the page to the viewport WIDTH, so to make a page of logical
    size ``page_w × page_h`` fit vertically we need its aspect ratio to be at
    least as wide as the viewport's. The widened width that achieves this is
    ``page_h * (vp_w / vp_h)``; a small margin guarantees the bottom outspace
    stays visible (our "page complete" sentinel signal).
    """
    try:
        import crop as CROP
    except ImportError:
        return None
    vp = CROP.find_canvas_viewport(img)
    if not vp:
        return None
    L, T, R, B = vp
    vp_w, vp_h = (R - L), (B - T)
    if vp_w <= 0 or vp_h <= 0:
        return None
    return max(page_w, page_h * (vp_w / float(vp_h)) * 1.08)


def _set_page_width(page_json: str, width: float) -> None:
    """Set the page logical width (preserving the sentinel outspace already
    written)."""
    try:
        with open(page_json, encoding="utf-8") as fh:
            data = json.load(fh)
        data["width"] = int(round(width))
        with open(page_json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


# --------------------------------------------------------------------------- #
# grow-window capture (native resolution, no scrolling)
#
# Power BI Desktop renders the page Fit-to-WIDTH, so a page taller than the
# canvas viewport is clipped at the fold. The most reliable way to capture the
# whole page on a small monitor is a Ctrl+wheel ZOOM-OUT, which shrinks the page
# uniformly into the viewport (every visual keeps its position/aspect, so crops
# stay aligned). ``_resize_window`` is kept as a helper for callers that need it.
# --------------------------------------------------------------------------- #
def _resize_window(hwnd: int, width: int, height: int) -> None:
    r = _window_rect(hwnd)
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    user32.SetWindowPos(hwnd, 0, r.left, 0, int(width), int(height),
                        SWP_NOZORDER | SWP_NOACTIVATE)


def _narrow_window_capture(hwnd: int, page_w: float, page_h: float, CROP):
    """Capture the whole page by NARROWING the Power BI Desktop window.

    Power BI Desktop fits the page to the canvas WIDTH and reflows live on every
    window resize (no relaunch needed). A page taller than the canvas is clipped,
    but if we make the window NARROWER the canvas gets narrower too, so the
    fit-to-width page renders SMALLER and therefore SHORTER — past a point the
    whole page fits the canvas height and is captured complete. The fit is
    UNIFORM (every visual keeps its aspect/position), so per-visual crops stay
    aligned, and the window stays fully on-screen (top-left fixed) so PrintWindow
    captures all of it. Fully deterministic — uses only SetWindowPos, no flaky
    keyboard/mouse injection. Returns the LARGEST complete ``(page_img, scale)``
    or None.

    The original window geometry is always restored before returning.
    """
    dbg = bool(os.environ.get("PBI_DEBUG"))
    r = _window_rect(hwnd)
    orig_w, orig_h = r.right - r.left, r.bottom - r.top
    if orig_w <= 0 or orig_h <= 0:
        return None
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    logical_aspect = page_w / float(page_h)

    def _resize(w: int) -> None:
        user32.SetWindowPos(hwnd, 0, r.left, r.top, int(w), int(orig_h),
                            SWP_NOZORDER | SWP_NOACTIVATE)

    best = None                                  # (page_img, scale, width_px)
    try:
        # Narrow the window in steps. The page is only guaranteed COMPLETE once
        # it is fully surrounded by the magenta outspace on every side — i.e.
        # ``find_page_by_sentinel`` locates a tight rectangle whose aspect matches
        # the logical page (a clipped page is too wide for its height). Scanning
        # widest→narrowest, the FIRST fully-framed level is the largest (highest
        # resolution) complete capture, so we take it and stop. Fully
        # deterministic (only SetWindowPos), no flaky keyboard/mouse injection,
        # and the fit is uniform so per-visual crops stay aligned.
        for frac in (0.94, 0.88, 0.82, 0.76, 0.70, 0.64, 0.58, 0.52, 0.46, 0.40):
            w = int(orig_w * frac)
            if w < 480:
                break
            _resize(w)
            time.sleep(1.1)
            img = _capture_window(hwnd)
            if img is None:
                continue
            vp = CROP.find_canvas_viewport(img)
            rect = CROP.find_page_by_sentinel(img)
            if not vp or not rect:
                continue
            cL, cT, cR, cB = vp
            L, T, Rr, B = rect
            pw, ph = (Rr - L), (B - T)
            aspect = pw / float(ph) if ph else 0
            framed = (T > cT + 4 and B < cB - 4 and L > cL - 2 and Rr < cR + 2)
            uniform = ph > 0 and abs(aspect - logical_aspect) / logical_aspect < 0.12
            if dbg:
                print(f"      [narrow] win={w}px page={pw}x{ph} "
                      f"aspect={aspect:.2f}/{logical_aspect:.2f} "
                      f"framed={framed} uniform={uniform}")
            if framed and uniform and pw >= 120:
                page_img = CROP._neutralize_sentinel(img.crop((L, T, Rr, B)))
                best = (page_img, pw / float(page_w), pw)
                break                            # largest fully-framed capture
        return (best[0], best[1]) if best else None
    finally:
        user32.SetWindowPos(hwnd, 0, r.left, r.top, orig_w, orig_h,
                            SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.8)


def _page_bbox_in_viewport(img, vp, CROP):
    """Tight bbox of the report page = non-sentinel pixels inside the canvas vp.

    Inside the Desktop canvas viewport there is only the page plus its magenta
    sentinel outspace (chrome lives outside ``vp``). So the page is simply the
    bounding box of every non-sentinel sample — robust at ANY zoom level, even
    when the page is small and surrounded by a large magenta margin (where the
    row/column-fraction detectors fail). Returns ``(L, T, R, B)`` or None.
    """
    cL, cT, cR, cB = vp
    px = img.load()
    sx = max(1, (cR - cL) // 300)
    sy = max(1, (cB - cT) // 300)
    xmin = ymin = None
    xmax = ymax = -1
    for y in range(cT, cB, sy):
        for x in range(cL, cR, sx):
            if not CROP._is_sentinel(px[x, y]):
                if xmin is None or x < xmin:
                    xmin = x
                if x > xmax:
                    xmax = x
                if ymin is None or y < ymin:
                    ymin = y
                if y > ymax:
                    ymax = y
    if xmin is None or xmax - xmin < 60 or ymax - ymin < 30:
        return None
    return xmin, ymin, min(cR, xmax + sx), min(cB, ymax + sy)


def _zoom_out_capture(hwnd: int, page_w: float, page_h: float, CROP):
    """Capture the whole page by Ctrl+wheel zooming OUT until it fits.

    Power BI Desktop renders Fit-to-Width by default (ignoring the page's
    FitToPage option in authoring view), clipping a tall page. A Ctrl+wheel
    zoom-out shrinks the WHOLE page uniformly into the viewport; once the page is
    small enough it is fully framed by the magenta outspace on every side, which
    ``find_page_by_sentinel`` locates exactly. We stop at the first zoom level
    where the page is fully framed (highest-resolution complete capture).
    Returns ``(page_img, scale)`` or None.
    """
    dbg = bool(os.environ.get("PBI_DEBUG"))
    logical_aspect = page_w / float(page_h)
    base = _capture_window(hwnd)
    if base is None:
        return None
    vp = CROP.find_canvas_viewport(base)
    if not vp:
        if dbg:
            print("      [zoom] no canvas viewport")
        return None
    cL, cT, cR, cB = vp
    zx, zy = _client_to_screen(hwnd, (cL + cR) // 2, (cT + cB) // 2)

    best = None                                # (page_img, scale, width)
    prev_w = None
    stable = 0
    for step in range(12):
        rect = CROP.find_page_by_sentinel(base)
        if rect:
            L, T, R, B = rect
            pw, ph = (R - L), (B - T)
            aspect = pw / float(ph) if ph else 0
            # Fully framed ⇒ the page sits strictly inside the canvas viewport
            # (magenta margin on top AND bottom) and its aspect matches the
            # logical page (a clipped page is too tall ⇒ aspect too small).
            framed = (T > cT + 6 and B < cB - 6)
            uniform = ph > 0 and abs(aspect - logical_aspect) / logical_aspect < 0.12
            if dbg:
                print(f"      [zoom] step {step}: rect={rect} {pw}x{ph} "
                      f"aspect={aspect:.2f}/{logical_aspect:.2f} "
                      f"framed={framed} uniform={uniform}")
            if framed and uniform and pw >= 120:
                # Keep the LARGEST fully-framed uniform capture (best resolution).
                if best is None or pw > best[2]:
                    page_img = CROP._neutralize_sentinel(base.crop((L, T, R, B)))
                    best = (page_img, pw / float(page_w), pw)
            # Stop once zooming no longer changes the page size (min zoom / stuck)
            # or the page has shrunk well past the first framed level.
            if prev_w is not None and abs(pw - prev_w) <= 2:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev_w = pw
            if best is not None and pw < best[2] * 0.85:
                break                          # already past the best level
        elif dbg:
            print(f"      [zoom] step {step}: no page rect")
        _ctrl_wheel(hwnd, zx, zy, -1)          # zoom out one notch
        time.sleep(1.4)
        nb = _capture_window(hwnd)
        if nb is None:
            break
        base = nb
        if dbg:
            base.save(f"_zoom_{step}.png")

    if best is not None:
        if dbg:
            print(f"      [zoom] chosen width={best[2]} scale={best[1]:.3f}")
        return best[0], best[1]
    return None


# --------------------------------------------------------------------------- #
# native-resolution scroll + stitch
#
# When a page is taller than the canvas viewport, Power BI Desktop clips it at
# the fold (Fit-to-Width). Rather than widening the page (which shrinks the whole
# height into one viewport at LOW resolution), we keep the native Fit-to-Width
# scale and capture the page in vertical tiles, scrolling the canvas down a bit
# at a time and stitching the tiles into one tall, full-resolution page image.
# Tiles are aligned by 1-D correlation of per-row brightness signatures, so no
# fixed scroll step is assumed. This works on ANY monitor size.
# --------------------------------------------------------------------------- #
_SIG_SAMPLES = 64          # horizontal samples per row signature
_VSCALE = 2                # vertical downscale factor for correlation
_OVERLAP_MAX_NORM = 16.0   # reject a tile match worse than this avg L1/pixel


def _capture_after_scroll(hwnd: int, settle: float = 0.8):
    """Capture the window after letting a scroll settle (no re-foreground wait)."""
    time.sleep(settle)
    return _capture_window(hwnd)


def _scroll_to_top(hwnd: int, sx: int, sy: int) -> None:
    for _ in range(12):
        _wheel(hwnd, sx, sy, 3)            # positive notches = up
        time.sleep(0.05)
    time.sleep(0.3)


def _col_xs(Lx: int, Rx: int):
    step = max(1, (Rx - Lx) // 120)
    return list(range(Lx, Rx, step))


def _page_bounds_in_canvas(img, vp, CROP):
    """Locate the report page inside the Desktop canvas viewport ``vp``.

    The page outspace is painted the magenta sentinel, so the page is the block
    of canvas rows/columns NOT dominated by sentinel. Unlike
    ``find_page_by_sentinel`` this does NOT require the page to be framed on all
    sides — it works on a clipped/scrolled tile where outspace appears on only
    some edges (e.g. a band above a Fit-to-Width page scrolled to its top).

    Returns ``(Lx, Ty, Rx, By, top_out, bot_out)`` where ``(Lx,Ty,Rx,By)`` is the
    visible page rectangle and ``top_out``/``bot_out`` say whether sentinel
    outspace borders the page above/below (i.e. its true top/bottom is on screen),
    or None when no page content is visible.
    """
    cL, cT, cR, cB = vp
    px = img.load()
    sxs = max(1, (cR - cL) // 160)
    sys_ = max(1, (cB - cT) // 160)
    xs = list(range(cL, cR, sxs))
    ys = list(range(cT, cB, sys_))
    if len(xs) < 4 or len(ys) < 4:
        return None

    def row_s(y):
        return sum(1 for x in xs if CROP._is_sentinel(px[x, y])) / len(xs)

    def col_s(x):
        return sum(1 for y in ys if CROP._is_sentinel(px[x, y])) / len(ys)

    cols = [x for x in xs if col_s(x) < 0.6]
    rows = [y for y in ys if row_s(y) < 0.6]
    if not cols or not rows:
        return None
    Lx, Rx = cols[0], min(cR, cols[-1] + sxs)
    Ty, By = rows[0], min(cB, rows[-1] + sys_)
    top_out = Ty > cT + 2 * sys_
    bot_out = By < cB - 2 * sys_
    if Rx - Lx < 80 or By - Ty < 30:
        return None
    return Lx, Ty, Rx, By, top_out, bot_out


def _row_sigs(pil_img):
    """Per-row brightness signature (list of rows, each ``_SIG_SAMPLES`` ints).

    Horizontally averaged to ``_SIG_SAMPLES`` columns and vertically downscaled
    by ``_VSCALE`` for fast, robust 1-D tile alignment.
    """
    h = max(1, pil_img.height // _VSCALE)
    g = pil_img.convert("L").resize((_SIG_SAMPLES, h))
    px = g.load()
    return [[px[x, y] for x in range(_SIG_SAMPLES)] for y in range(h)]


def _best_overlap(strip_sigs, tile_sigs, min_ov: int = 6):
    """Rows of ``tile_sigs`` (top) that overlap ``strip_sigs`` (bottom).

    Returns the overlap in signature-rows, or None when no reliable match is
    found (so the caller stops stitching rather than corrupt the page).
    """
    Hs, Ht = len(strip_sigs), len(tile_sigs)
    max_ov = min(Hs, Ht)
    if max_ov < min_ov or not tile_sigs:
        return None
    n = len(tile_sigs[0])
    best = None
    for ov in range(min_ov, max_ov + 1):
        s = strip_sigs[Hs - ov:]
        t = tile_sigs[:ov]
        cost = 0
        for ar, br in zip(s, t):
            for p, q in zip(ar, br):
                cost += p - q if p > q else q - p
        norm = cost / (ov * n)
        if best is None or norm < best[0]:
            best = (norm, ov)
    if best is None or best[0] > _OVERLAP_MAX_NORM:
        return None
    return best[1]


def _scroll_stitch_capture(hwnd: int, page_w: float, page_h: float, CROP):
    """Capture a clipped page at native Fit-to-Width resolution by scrolling the
    canvas and stitching the tiles. Returns ``(page_img, scale)`` or None."""
    from PIL import Image

    dbg = bool(os.environ.get("PBI_DEBUG"))
    base = _capture_window(hwnd)
    if base is None:
        return None
    if dbg:
        base.save("_stitch_base.png")
    vp = CROP.find_canvas_viewport(base)
    if not vp:
        if dbg:
            print("      [stitch] no canvas viewport")
        return None
    cL, cT, cR, cB = vp
    # Wheel over the right scrollbar gutter, not the page centre: a wheel event
    # over an interactive visual (chart/table) is consumed by that visual and the
    # page never scrolls. The gutter just inside the canvas right edge always
    # scrolls the page itself.
    gx, gy = _client_to_screen(hwnd, cR - 8, (cT + cB) // 2)

    _scroll_to_top(hwnd, gx, gy)
    img = _capture_after_scroll(hwnd, 1.0)
    if img is None:
        return None
    if dbg:
        img.save("_stitch_tile0.png")
    b = _page_bounds_in_canvas(img, vp, CROP)
    if not b:
        if dbg:
            print("      [stitch] no page bounds in tile0")
        return None
    Lx, Ty, Rx, By, _top_out, bot_out = b
    scale = (Rx - Lx) / float(page_w)
    target_h = int(round(scale * page_h))
    if dbg:
        print(f"      [stitch] vp={vp} bounds={b} scale={scale:.3f} "
              f"target_h={target_h}")

    strip = CROP._neutralize_sentinel(img.crop((Lx, Ty, Rx, By)))
    if bot_out:                                # whole page already visible
        if dbg:
            print(f"      [stitch] whole page in one tile ({strip.height}px)")
        return strip, scale
    strip_sigs = _row_sigs(strip)

    last_h = -1
    reached_bottom = False
    stuck = 0
    for i in range(60):
        _wheel(hwnd, gx, gy, -2)               # scroll down over the scrollbar
        img = _capture_after_scroll(hwnd, 0.7)
        if img is None:
            break
        if dbg and i == 0:
            img.save("_stitch_tile1.png")
        tb = _page_bounds_in_canvas(img, vp, CROP)
        if not tb:
            break
        tLx, tTy, tRx, tBy, _to, t_bot = tb
        tile = img.crop((Lx, tTy, Rx, tBy))
        if tile.height < 8:
            if t_bot:
                reached_bottom = True
                break
            continue
        tile_sigs = _row_sigs(tile)
        ov_rows = _best_overlap(strip_sigs, tile_sigs)
        if ov_rows is None:
            if dbg:
                print(f"      [stitch] no overlap at step {i} "
                      f"(strip={strip.height} tile={tile.height})")
            break                              # unreliable match — stop
        ov_full = ov_rows * _VSCALE
        if dbg:
            print(f"      [stitch] step {i}: tile={tile.height} ov={ov_full} "
                  f"t_bot={t_bot} strip={strip.height}")
        if ov_full >= tile.height - 1:         # no forward progress this step
            if t_bot:
                reached_bottom = True
                break
            stuck += 1
            if stuck >= 3:                      # canvas won't scroll — give up
                break
            continue
        stuck = 0
        add = tile.crop((0, ov_full, tile.width, tile.height))
        new = Image.new("RGB", (strip.width, strip.height + add.height))
        new.paste(strip, (0, 0))
        new.paste(add, (0, strip.height))
        strip = new
        strip_sigs = strip_sigs + tile_sigs[ov_rows:]
        if t_bot:
            reached_bottom = True
            break
        if target_h and strip.height >= target_h * 1.25:
            reached_bottom = True
            break
        if strip.height == last_h:             # stuck (cannot scroll further)
            break
        last_h = strip.height

    # Only accept the stitch if we actually captured (almost) the whole page.
    # If the canvas never scrolled we have just the first tile — return None so
    # the caller falls back to the widen path (complete, mid-resolution).
    if not reached_bottom and (not target_h or strip.height < target_h * 0.95):
        if dbg:
            print(f"      [stitch] incomplete ({strip.height}/{target_h}px) — "
                  "falling back to widen")
        return None
    if target_h and strip.height > target_h + 4:
        strip = strip.crop((0, 0, strip.width, target_h))
    return CROP._neutralize_sentinel(strip), scale


def _widen_capture(img, page_json: str, page_w: float, page_h: float,
                   pbip_path: str, model_name: str, launch_timeout: int,
                   render_settle: float, CROP):
    """Capture the COMPLETE page by widening its logical width and relaunching.

    Power BI Desktop fits the page to the viewport WIDTH, so a page taller than
    the canvas is clipped at the fold. Widening the page (adding empty margin on
    the right) lowers the fit-to-width scale until the FULL logical height fits
    the viewport — giving a single uniform-scale capture of every visual. The
    target width is chosen so the resulting scale is the best possible that still
    fits the height (≈ ``viewport_h / page_h``), which is far sharper than
    shrinking the window. The page's logical width is ALWAYS restored before
    returning, so a temporary widen can never persist into the emitted report.

    Returns ``(page_img, scale, eff_width)`` or None.
    """
    target = _widen_target(img, page_w, page_h)
    if not target or target <= page_w + 1:
        return None
    try:
        for attempt in range(4):
            print(f"    widening page to fit (attempt {attempt + 1}, "
                  f"width={int(round(target))})")
            _set_page_width(page_json, target)
            # Force a genuinely fresh render: the on-disk width change is only
            # picked up by a brand-new window, so close any open report first and
            # wait for a NEW window (never reattach to the stale native render).
            hwnd2 = _launch_and_wait(pbip_path, model_name, launch_timeout,
                                     force_fresh=True)
            if not hwnd2:
                break
            img2 = _render_and_capture(hwnd2, render_settle)
            if img2 is None:
                target *= 1.3
                continue
            r2 = CROP.clean_page_full(img2, page_h, target)
            if not r2:
                target *= 1.3                  # page rect not found — widen more
                continue
            page_img, scale, complete = r2
            if complete:                       # full height ⇒ scale is correct
                eff_w = (page_img.width / scale) if scale else target
                print(f"    full page captured (scale={scale:.3f}, "
                      f"{page_img.height}px tall)")
                return page_img, scale, eff_w
            # Not complete yet — widen further. We never return a PARTIAL widen:
            # its scale is derived from the widened logical width but the render
            # is still clipped, so using it would misplace the bottom visuals.
            # If widening can't complete (e.g. side panes shrink the canvas), the
            # caller falls back to the narrow-window capture, which keeps the page
            # at native width and therefore always crops the correct visual.
            target *= 1.3
        print("    widen could not capture the full height; "
              "falling back to window-fit capture")
        return None
    finally:
        _set_page_width(page_json, page_w)     # never leave the page widened


def _full_page_capture(img, page_json: str, page_w: float, page_h: float,
                       pbip_path: str, model_name: str, launch_timeout: int,
                       render_settle: float, CROP, hwnd: Optional[int] = None):
    """Return ``(page_image, scale, eff_width)`` for the WHOLE page.

    Order of preference (correct alignment first, then resolution):
      1. the native Fit-to-Width render, if it already shows the full page;
      2. SCROLL + STITCH — scroll the native Fit-to-Width canvas and stitch the
         tiles. This keeps the page at native resolution (~0.71 scale, sharp) AND
         at its native logical width, so every visual crops correctly at full
         quality. Preferred whenever the canvas can scroll;
      3. narrow-window / zoom-out — deterministic window resizes (no relaunch)
         that keep the page at native logical width, so crops stay aligned even
         though the page is rendered smaller (lower resolution but correct);
      4. WIDEN + relaunch — last resort. Only ever accepted when the capture is
         verified COMPLETE (full height with a bottom sentinel margin), never as
         a partial — a partial widen would misplace the bottom visuals.

    The returned ``scale`` is height-consistent so every visual crops at its
    original logical rectangle.
    """
    full = CROP.clean_page_full(img, page_h, page_w)
    if full and full[2]:                       # native render already complete
        print(f"    page fits viewport natively (scale={full[1]:.3f}, "
              f"{full[0].height}px tall)")
        return full[0], full[1], page_w

    if hwnd:                                    # native-resolution scroll+stitch
        try:                                    # (sharp, complete, aligned)
            stitched = _scroll_stitch_capture(hwnd, page_w, page_h, CROP)
        except Exception as exc:               # noqa: BLE001 — fall through
            print(f"    scroll-stitch failed ({exc})")
            stitched = None
        if stitched is not None:
            page_img, scale = stitched
            print(f"    scroll-stitched full page at native resolution "
                  f"(scale={scale:.3f}, {page_img.height}px tall)")
            return page_img, scale, page_w

    if hwnd:                                    # narrow the window so the page
        try:                                    # fits the canvas height (uniform,
            narrowed = _narrow_window_capture(hwnd, page_w, page_h, CROP)
        except Exception as exc:               # noqa: BLE001 — fall through
            print(f"    narrow-window failed ({exc})")
            narrowed = None
        if narrowed is not None:
            page_img, scale = narrowed
            print(f"    narrowed window to capture full page (scale={scale:.3f}, "
                  f"{page_img.height}px tall)")
            return page_img, scale, page_w

    if hwnd:                                    # zoom out uniformly so the whole
        try:                                    # page fits the viewport
            zoomed = _zoom_out_capture(hwnd, page_w, page_h, CROP)
        except Exception as exc:               # noqa: BLE001 — fall through
            print(f"    zoom-out failed ({exc})")
            zoomed = None
        if zoomed is not None:
            page_img, scale = zoomed
            print(f"    zoomed out to capture full page (scale={scale:.3f}, "
                  f"{page_img.height}px tall)")
            return page_img, scale, page_w

    # Last resort before a clipped crop: WIDEN + relaunch. This invalidates the
    # original window handle, so it runs only after the window-based methods. It
    # returns a result ONLY when verified COMPLETE (never a misaligned partial).
    try:
        widened = _widen_capture(img, page_json, page_w, page_h, pbip_path,
                                 model_name, launch_timeout, render_settle, CROP)
    except Exception as exc:                    # noqa: BLE001 — fall through
        print(f"    widen failed ({exc})")
        widened = None
    if widened is not None:
        return widened

    # Last resort: keep whatever native page crop we can get.
    ci = CROP.clean_page_image(img, page_w)
    if ci:
        print("    page taller than viewport; using clipped native crop")
        return ci[0], ci[1], page_w
    return None


def _report_pages(out_dir: str, model_name: str) -> List[str]:
    base = os.path.join(out_dir, f"{model_name}.Report", "definition", "pages")
    order = os.path.join(base, "pages.json")
    names: List[str] = []
    if os.path.isfile(order):
        try:
            with open(order, encoding="utf-8") as fh:
                data = json.load(fh)
            names = [p for p in data.get("pageOrder", [])]
        except (OSError, json.JSONDecodeError):
            names = []
    if not names and os.path.isdir(base):
        names = [d for d in os.listdir(base)
                 if os.path.isdir(os.path.join(base, d))]
    return names


# --------------------------------------------------------------------------- #
# sentinel-outspace helpers — make the report page a detectable rectangle so
# each visual can be cropped out of the whole-page capture (see crop.py).
# --------------------------------------------------------------------------- #
def _page_json_path(out_dir: str, model_name: str, page_dir: str) -> str:
    return os.path.join(out_dir, f"{model_name}.Report", "definition",
                        "pages", page_dir, "page.json")


def _page_dims(page_json: str) -> tuple:
    try:
        with open(page_json, encoding="utf-8") as fh:
            d = json.load(fh)
        return float(d.get("width") or 1280), float(d.get("height") or 720)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 1280.0, 720.0


def _is_transparent_bg(bg_obj) -> bool:
    """True if a page ``objects.background`` entry is effectively transparent.

    A transparent page background lets the magenta outspace show *through* the
    page, making the page indistinguishable from its surround. We detect this so
    we can temporarily force an opaque background for capture.
    """
    if not bg_obj:
        return True
    try:
        props = bg_obj[0].get("properties", {})
    except (AttributeError, IndexError, TypeError):
        return True
    # high transparency percentage
    try:
        tv = props["transparency"]["expr"]["Literal"]["Value"]
        pct = float(str(tv).rstrip("Dd"))
        if pct >= 50:
            return True
    except (KeyError, TypeError, ValueError):
        pass
    # colour literal with a zero alpha component (e.g. #00000000)
    try:
        val = props["color"]["solid"]["color"]["expr"]["Literal"]["Value"]
        hexv = str(val).strip().strip("'").lstrip("#")
        if len(hexv) == 8 and hexv[-2:] == "00":
            return True
        if len(hexv) == 8 and hexv[:2] == "00":
            return True
    except (KeyError, TypeError):
        pass
    return False


def _set_page_outspace(page_json: str, hex_color: str) -> Optional[str]:
    """Paint the page outspace ``hex_color``; return the original file text.

    Also forces an OPAQUE page background when the existing one is transparent
    (so the magenta outspace cannot bleed through the page itself). Pages that
    already have an opaque background — including dark themes — are left as-is so
    their real appearance is captured.
    """
    try:
        with open(page_json, encoding="utf-8") as fh:
            raw = fh.read()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    solid = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_color}'"}}}}}
    objs = data.setdefault("objects", {})
    objs["outspace"] = [{"properties": {"color": solid}}]
    if _is_transparent_bg(objs.get("background")):
        white = {"solid": {"color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}}}
        objs["background"] = [{
            "properties": {
                "color": white,
                "transparency": {"expr": {"Literal": {"Value": "0D"}}},
            }
        }]
    try:
        with open(page_json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        return None
    return raw


def _restore_text(path: str, raw: Optional[str]) -> None:
    if raw is None:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def _render_and_capture(hwnd: int, render_settle: int):
    """Foreground the window, wait for the canvas to render, and capture it.

    Returns a non-blank window image of a sensible size, or ``None`` if the
    report never finished rendering in time.
    """
    _force_foreground(hwnd)
    time.sleep(render_settle)
    img = None
    for _ in range(20):          # wait for data + visuals to render
        _force_foreground(hwnd)
        img = _capture_window(hwnd)
        if img is not None and not _looks_blank(img):
            break
        time.sleep(4)
    if img is None or _looks_blank(img) or img.size[0] < 600 or img.size[1] < 400:
        return None
    return img


def _native_fit_layer(img, page_w: float, page_h: float, CROP):
    """High-resolution Fit-to-Width layer, aligned to the page's true top-left.

    Power BI Desktop fits the page to the canvas WIDTH and, on a fresh render,
    scrolls it to the top, so the page sits at the top of the canvas with its
    magenta outspace forming a margin around it (a page taller than the canvas is
    clipped at the fold ⇒ no bottom margin). ``clean_page_image`` locates the
    page precisely from that sentinel margin, so its top-left is the page's
    logical (0, 0) and per-visual crops line up. We return that sharp page image
    plus the visible logical height (``page_px_height / scale``) so the cropper
    knows which visuals are present (the rest fall back to the complete layer).
    Returns ``(page_img, scale, max_logical_y)`` or None.
    """
    hi = CROP.clean_page_image(img, page_w)
    if not hi:
        return None
    page_img, scale = hi
    if not scale:
        return None
    max_y = page_img.height / scale
    return page_img, scale, max_y


def _full_page_layers(img, page_json: str, page_w: float, page_h: float,
                      pbip_path: str, model_name: str, launch_timeout: int,
                      render_settle: float, CROP, hwnd: Optional[int] = None):
    """Return one or more ``(page_img, scale, max_logical_y)`` capture LAYERS.

    Each visual is later cropped from the HIGHEST-resolution layer in which it is
    fully visible (``visual_bottom <= layer.max_logical_y``). This gives sharp
    per-visual crops on a small monitor instead of squeezing the whole page into
    one tiny image:

      * Layer 1 — the native Fit-to-Width render. Top-aligned at logical (0, 0),
        so crops are correctly placed; sharp (~0.7 scale) but a page taller than
        the canvas is clipped at the fold, so only the upper visuals are present.
      * Layer 2 — a complete whole-page capture (narrow-window / widen fallback).
        Lower resolution but the FULL height, so the clipped bottom visuals still
        get a real (if smaller) crop.

    On a large enough monitor layer 1 is already complete, so layer 2 is the same
    page and every visual crops from the sharp layer. Layers are ordered
    highest-resolution first.
    """
    layers = []
    hi = _native_fit_layer(img, page_w, page_h, CROP)   # sharp, top-aligned
    if hi:
        layers.append(hi)
    full = _full_page_capture(img, page_json, page_w, page_h, pbip_path,
                              model_name, launch_timeout, render_settle,
                              CROP, hwnd)
    if full:
        page_img, scale, _ = full
        # +1 so the comparison ``visual_bottom <= max_y`` always holds for the
        # complete layer (it covers the whole logical height).
        layers.append((page_img, scale, page_h + 1))
    return layers or None


def capture_report(out_dir: str, pbip_path: str, model_name: str,
                   page_display_names: Optional[Dict[str, str]] = None,
                   launch_timeout: int = 150, render_settle: int = 25,
                   keep_open: bool = False) -> Dict[str, str]:
    """Open the .pbip in Power BI Desktop and capture each report page.

    Power BI Desktop opens on the page named by ``activePageName`` in
    ``pages.json`` and has no reliable keyboard/CLI way to switch pages, so we
    drive page selection deterministically: set ``activePageName`` to the target
    page, (re)launch Power BI Desktop, capture, then move on. The original
    ``activePageName`` is restored at the end.

    Returns ``{page_dir_name: png_path}`` for every page successfully captured.
    Writes ``validation/screenshots/page_<page>_powerbi.png``.
    """
    if os.name != "nt":
        return {}
    if not os.path.isfile(pbip_path):
        return {}

    pages = _report_pages(out_dir, model_name) or []
    pages_json = os.path.join(out_dir, f"{model_name}.Report", "definition",
                              "pages", "pages.json")
    original_active = _read_active_page(pages_json)

    d = SS.ensure_dir(out_dir)
    out: Dict[str, str] = {}
    page_list = pages or [original_active or "Page1"]
    single = len(page_list) <= 1

    try:
        import crop as CROP  # noqa: E402
    except ImportError:
        CROP = None

    try:
        for page in page_list:
            # Make the page outspace a sentinel colour and (re)launch so Power BI
            # renders it — this lets us crop each visual out of the page later.
            # A fresh load is required for the sentinel to take effect, so always
            # close any open window for this report first.
            page_json = _page_json_path(out_dir, model_name, page)
            page_w, page_h = _page_dims(page_json)
            _close_all_report_windows(model_name)
            if not single:
                _set_active_page(pages_json, page)
            orig_page_text = (_set_page_outspace(page_json, CROP.SENTINEL_HEX)
                              if CROP else None)

            try:
                hwnd = _launch_and_wait(pbip_path, model_name, launch_timeout)
                if not hwnd:
                    print("  Power BI Desktop window did not appear (sign-in "
                          "prompt?). Skipping remaining Power BI captures.")
                    break

                img = _render_and_capture(hwnd, render_settle)
                if img is None:
                    print(f"  Power BI page '{page}' did not render in time — "
                          "leaving a placeholder.")
                    continue

                # Clean the chrome + sentinel away to a page image whose top-left
                # is logical (0,0). Power BI Desktop renders Fit-to-WIDTH (it
                # ignores FitToPage), so a page taller than the viewport is cut
                # off at the fold. When that happens we deterministically WIDEN
                # the page's logical width: Fit-to-Width then shrinks the whole
                # (invariant) height into one viewport, revealing every visual.
                # The reliable scale is height-derived (page_px_height/page_h),
                # which is unaffected by the temporary width change, so each
                # visual still crops at its original logical rectangle.
                path = os.path.join(d, f"page_{_norm(page)}_powerbi.png")
                eff_w = page_w
                layers = _full_page_layers(
                    img, page_json, page_w, page_h, pbip_path, model_name,
                    launch_timeout, render_settle, CROP, hwnd) if CROP else None

                if layers:
                    # Save every layer; the primary (highest-res) keeps the
                    # canonical page filename so existing consumers still work.
                    primary_img, primary_scale, _ = layers[0]
                    primary_img.save(path)
                    layer_meta = [{"image": os.path.basename(path),
                                   "scale": primary_scale,
                                   "maxY": layers[0][2]}]
                    for i, (limg, lscale, lmaxy) in enumerate(layers[1:], 1):
                        lpath = os.path.join(
                            d, f"page_{_norm(page)}_powerbi_L{i}.png")
                        limg.save(lpath)
                        layer_meta.append({"image": os.path.basename(lpath),
                                           "scale": lscale, "maxY": lmaxy})
                    _write_page_meta(d, page, primary_scale, eff_w, page_h,
                                     layers=layer_meta)
                else:
                    img.save(path)        # fallback: whole window
                    _write_page_meta(d, page, None, eff_w, page_h)
                out[page] = path
            finally:
                _restore_text(page_json, orig_page_text)
    finally:
        if not keep_open:
            _close_all_report_windows(model_name)
        if not single and original_active is not None:
            _set_active_page(pages_json, original_active)

    return out


def _write_page_meta(screens_dir: str, page: str, scale: Optional[float],
                     page_w: float, page_h: float,
                     layers: Optional[List[Dict]] = None) -> None:
    """Persist the logical→pixel ``scale`` for a captured page (for crop.py).

    ``layers`` (optional) lists every resolution layer captured for the page —
    ``[{"image", "scale", "maxY"}, ...]`` highest-resolution first — so the
    per-visual cropper can pick the sharpest layer that still contains each
    visual. ``scale`` mirrors the primary layer for backward compatibility.
    """
    meta = os.path.join(screens_dir, f"page_{_norm(page)}_powerbi.json")
    payload = {"scale": scale, "pageWidth": page_w,
               "pageHeight": page_h, "cropped": scale is not None}
    if layers:
        payload["layers"] = layers
    try:
        with open(meta, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# isolated per-visual capture
# --------------------------------------------------------------------------- #
def _dbg() -> bool:
    return bool(os.environ.get("PBI_DEBUG"))


def _rmtree(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _write_temp_page_json(path: str, name: str, page_w: float, page_h: float,
                          src_page_json: Optional[str], CROP) -> None:
    """Write a temp single-visual page.json (sentinel outspace painted)."""
    objects = {}
    if src_page_json and os.path.isfile(src_page_json):
        try:
            with open(src_page_json, encoding="utf-8") as fh:
                objects = json.load(fh).get("objects", {}) or {}
        except (OSError, json.JSONDecodeError):
            objects = {}
    data = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                   "report/definition/page/2.0.0/schema.json",
        "name": name,
        "displayName": name,
        "displayOption": "FitToPage",
        "height": int(round(page_h)),
        "width": int(round(page_w)),
        "objects": objects,
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        return
    _set_page_outspace(path, CROP.SENTINEL_HEX)   # paint sentinel for cropping


def _copy_visual_to(src_vf: str, dst_vf: str, vw: float, vh: float) -> bool:
    """Copy a visual folder and reposition the visual to fill (0,0,vw,vh)."""
    import shutil
    try:
        shutil.copytree(src_vf, dst_vf)
    except OSError:
        return False
    vj = os.path.join(dst_vf, "visual.json")
    try:
        with open(vj, encoding="utf-8") as fh:
            data = json.load(fh)
        pos = data.get("position", {}) or {}
        pos["x"], pos["y"] = 0, 0
        pos["width"], pos["height"] = int(round(vw)), int(round(vh))
        pos.setdefault("z", 0)
        data["position"] = pos
        with open(vj, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except (OSError, json.JSONDecodeError):
        return False
    return True


def capture_visuals_isolated(out_dir: str, pbip_path: str, model_name: str,
                             targets: List[Dict], *, launch_timeout: int = 150,
                             render_settle: int = 25) -> set:
    """Capture each requested visual COMPLETE + sharp, alone on a temp page.

    A tall report page is clipped at the fold on a small monitor, so visuals
    below the fold cannot be captured from the whole-page render. This renders
    each visual ALONE on a temporary page: the visual is placed at logical
    ``(0, 0)`` and the page is padded on the right to a WIDE aspect ratio so
    Power BI's Fit-to-Width shows the visual's full height within the viewport.
    The single-visual page is captured complete, the visual is cropped out, and
    saved as ``<key>_powerbi.png`` (never overwriting a user-supplied file).

    ``targets`` is a list of ``{"key", "name", "position": {x,y,width,height}}``
    where ``name`` is the visual's PBIR folder name. The temp page and the
    original ``pages.json`` are always removed/restored before returning, so the
    emitted report is never altered. Returns the set of keys captured.
    """
    done: set = set()
    if os.name != "nt" or not targets or not os.path.isfile(pbip_path):
        return done
    try:
        import crop as CROP  # noqa: E402
    except ImportError:
        return done

    pages_base = os.path.join(out_dir, f"{model_name}.Report", "definition",
                              "pages")
    pages_json = os.path.join(pages_base, "pages.json")
    if not os.path.isfile(pages_json) or not os.path.isdir(pages_base):
        return done
    try:
        with open(pages_json, encoding="utf-8") as fh:
            pages_raw = fh.read()
        pages_data = json.loads(pages_raw)
    except (OSError, json.JSONDecodeError):
        return done

    screens = SS.ensure_dir(out_dir)
    TEMP = "__isocap"
    temp_dir = os.path.join(pages_base, TEMP)

    def _find_visual_folder(name: str) -> Optional[str]:
        for pd in os.listdir(pages_base):
            if pd == TEMP:
                continue
            vf = os.path.join(pages_base, pd, "visuals", name)
            if os.path.isfile(os.path.join(vf, "visual.json")):
                return vf
        return None

    src_page_json = None
    for pd in os.listdir(pages_base):
        pj = os.path.join(pages_base, pd, "page.json")
        if os.path.isfile(pj):
            src_page_json = pj
            break

    print(f"  capturing {len(targets)} below-fold visual(s) in isolation for a "
          "complete, uncropped render (one relaunch each)…")
    try:
        for t in targets:
            key = t.get("key")
            name = t.get("name")
            pos = t.get("position") or {}
            vw = float(pos.get("width") or 0)
            vh = float(pos.get("height") or 0)
            if not key or not name or vw <= 0 or vh <= 0:
                continue
            dst = os.path.join(screens, f"{key}_powerbi.png")
            if os.path.isfile(dst):
                done.add(key)
                continue                       # never overwrite a supplied file
            src_vf = _find_visual_folder(name)
            if not src_vf:
                print(f"    · {key}: source visual '{name}' not found — skipped")
                continue

            captured = False
            reason = "?"
            aspect = 3.1                       # > viewport aspect ⇒ height fits
            for _attempt in range(4):
                _rmtree(temp_dir)
                os.makedirs(os.path.join(temp_dir, "visuals"), exist_ok=True)
                page_w = max(vw, vh * aspect)
                page_h = vh
                _write_temp_page_json(os.path.join(temp_dir, "page.json"),
                                      TEMP, page_w, page_h, src_page_json, CROP)
                if not _copy_visual_to(src_vf,
                                       os.path.join(temp_dir, "visuals", name),
                                       vw, vh):
                    reason = "could not stage visual folder"
                    break
                pd2 = dict(pages_data)
                pd2["pageOrder"] = [TEMP]
                pd2["activePageName"] = TEMP
                try:
                    with open(pages_json, "w", encoding="utf-8") as fh:
                        json.dump(pd2, fh, indent=2)
                except OSError:
                    reason = "could not rewrite pages.json"
                    break

                hwnd = _launch_and_wait(pbip_path, model_name, launch_timeout,
                                        force_fresh=True)
                if not hwnd:
                    reason = "Power BI window did not open"
                    break
                img = _render_and_capture(hwnd, render_settle)
                if img is None:
                    reason = "PrintWindow returned nothing"
                    aspect *= 1.3
                    continue
                if _dbg():
                    try:
                        img.save(os.path.join(screens,
                                 f"__iso_{key}_a{_attempt}_raw.png"))
                    except OSError:
                        pass
                r = CROP.clean_page_full(img, page_h, page_w)
                if not r:
                    reason = "page sentinel not found in capture"
                    aspect *= 1.3
                    continue
                page_img, scale, complete = r
                if not complete:
                    reason = (f"still clipped at aspect {aspect:.1f} "
                              f"(scale {scale:.3f})")
                    aspect *= 1.3              # still clipped — widen the padding
                    continue
                crop_img = CROP.crop_visual(
                    page_img, scale,
                    {"x": 0, "y": 0, "width": vw, "height": vh})
                if crop_img is None:
                    reason = "crop_visual returned None"
                    aspect *= 1.3
                    continue
                crop_img.save(dst)
                done.add(key)
                captured = True
                print(f"    · {key}: captured {crop_img.size[0]}x"
                      f"{crop_img.size[1]} (complete)")
                break
            if not captured:
                print(f"    · {key}: isolated capture failed — {reason}")
    finally:
        _rmtree(temp_dir)
        _restore_text(pages_json, pages_raw)
        _close_all_report_windows(model_name)
    return done


def _close_all_report_windows(model_name: str, timeout: int = 30) -> None:
    """Close every Power BI Desktop window for this report and wait until gone."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_pbi_window(model_name)
        if not hwnd:
            return
        _close_pbi(hwnd)
        time.sleep(3)



def _launch_and_wait(pbip_path: str, model_name: str,
                     launch_timeout: int, force_fresh: bool = False) -> Optional[int]:
    """Launch the .pbip (if not already open) and return its report window.

    When ``force_fresh`` is True any already-open window is closed first and the
    function waits for a genuinely NEW window to appear, so a relaunch that must
    pick up an on-disk change (e.g. a widened page width) never reattaches to the
    stale already-rendered window.
    """
    if force_fresh:
        _close_all_report_windows(model_name)
    else:
        hwnd = _find_pbi_window(model_name)
        if hwnd is not None:
            return hwnd
    pre = set(_pbi_report_windows())
    try:
        os.startfile(pbip_path)  # noqa: S606 (intended app launch)
    except OSError:
        return None
    deadline = time.time() + launch_timeout
    while time.time() < deadline:
        new = [w for w in _pbi_report_windows() if w not in pre]
        if new:
            return new[0]
        if not force_fresh:
            hwnd = _find_pbi_window(model_name)   # prefer the titled report window
            if hwnd:
                return hwnd
        time.sleep(2)
    # Fresh launch never produced a NEW window — fall back to any report window
    # so the caller still gets something rather than nothing.
    return _find_pbi_window(model_name)


def _read_active_page(pages_json: str) -> Optional[str]:
    try:
        with open(pages_json, encoding="utf-8") as fh:
            return json.load(fh).get("activePageName")
    except (OSError, json.JSONDecodeError):
        return None


def _set_active_page(pages_json: str, page: str) -> None:
    try:
        with open(pages_json, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("activePageName") == page:
            return
        data["activePageName"] = page
        with open(pages_json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except (OSError, json.JSONDecodeError):
        pass



def _largest_monitor_rect() -> Optional[tuple]:
    """Work-area ``(x, y, w, h)`` of the monitor with the largest area.

    Capturing a tall report page is resolution-limited by the window size, so we
    always drive Power BI Desktop onto the biggest available monitor before
    maximizing. Multi-monitor setups commonly pair a small laptop panel with a
    large external display; using the largest one maximises crop fidelity.
    """
    best = [None]  # (area, x, y, w, h)
    MonitorEnumProc = ctypes.WINFUNCTYPE(
        wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    def _cb(hmon, hdc, lprc, lparam):  # noqa: ARG001
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            work = mi.rcWork
            w, h = work.right - work.left, work.bottom - work.top
            area = w * h
            if best[0] is None or area > best[0][0]:
                best[0] = (area, work.left, work.top, w, h)
        return True

    user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
    if best[0] is None:
        return None
    _, x, y, w, h = best[0]
    return (x, y, w, h)


def _move_to_largest_monitor(hwnd: int) -> None:
    """Move ``hwnd`` onto the largest monitor (so a later maximize uses it)."""
    rect = _largest_monitor_rect()
    if not rect:
        return
    x, y, w, h = rect
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                        SWP_NOZORDER | SWP_NOACTIVATE)


def _force_foreground(hwnd: int) -> None:
    """Best-effort bring ``hwnd`` to the foreground and maximize it.

    Uses the AttachThreadInput trick so SetForegroundWindow succeeds even when
    another process owns the foreground (common with several PBI windows open).
    """
    SW_RESTORE, SW_MAXIMIZE = 9, 3
    user32.ShowWindow(hwnd, SW_RESTORE)
    _move_to_largest_monitor(hwnd)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    try:
        fg = user32.GetForegroundWindow()
        cur_t = user32.GetWindowThreadProcessId(fg, None)
        tgt_t = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(cur_t, tgt_t, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(cur_t, tgt_t, False)
    except Exception:  # noqa: BLE001 — foreground is best-effort
        user32.SetForegroundWindow(hwnd)


def _close_pbi(hwnd: int) -> None:
    WM_CLOSE = 0x0010
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(2)
    # a "save changes?" prompt may appear; decline it (Alt+N)
    user32.keybd_event(0x12, 0, 0, 0)   # ALT down
    user32.keybd_event(0x4E, 0, 0, 0)   # 'N'
    user32.keybd_event(0x4E, 0, 0x2, 0)
    user32.keybd_event(0x12, 0, 0x2, 0)


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())
