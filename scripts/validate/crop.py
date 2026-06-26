"""crop.py — cut an individual Power BI visual out of a full-page Desktop capture.

The Power BI panes are REAL Power BI Desktop window captures (see
``powerbi_capture``). A whole-window capture also contains Desktop chrome (the
ribbon, the left mode strip, the right Filters/Visualizations/Fields panes) and
the report page sits somewhere inside it, surrounded by the page *outspace*.

To show **per-visual** evidence (not the same whole page on every row) the
capture step paints the page outspace a unique SENTINEL colour (magenta) before
screenshotting. That makes the report page a crisply-bounded rectangle no matter
what theme/background the page uses, so we can:

1. find the Power BI Desktop canvas viewport from report-INDEPENDENT chrome
   (the light-grey gutter on the left, the light pane band on the right), then
2. inside that viewport take the bounding box of every non-sentinel pixel — that
   is the report page rectangle, with its top-left at logical ``(0, 0)``.

Cropping to that rectangle yields a clean, chrome-free page image whose pixel
scale is ``page_px_width / pageWidth``; each visual is then a simple
logical→pixel rectangle. Visuals scrolled below the captured page bottom
(Fit-to-width authoring view) cannot be cropped and fall back to the page image.

Standard library + Pillow only.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

# Painted onto the page outspace before capture; must not occur in real content.
SENTINEL_RGB = (255, 0, 254)
SENTINEL_HEX = "#FF00FE"


def _is_gutter(c) -> bool:
    r, g, b = c[:3]
    return abs(r - g) < 6 and abs(g - b) < 6 and 238 <= (r + g + b) // 3 <= 247


def _is_pane(c) -> bool:
    r, g, b = c[:3]
    return abs(r - g) < 6 and abs(g - b) < 6 and (r + g + b) // 3 >= 249


def _is_sentinel(c, tol: int = 45) -> bool:
    r, g, b = c[:3]
    return (r >= 255 - tol and g <= tol + 12 and b >= 255 - tol - 6)


def _is_sentinelish(c) -> bool:
    """Looser magenta test for *cleanup* only (catches anti-aliased edges).

    GPU scaling blends the pure ``#FF00FE`` outspace toward the page background,
    producing softened magenta pixels like ``(237, 64, 236)`` that the strict
    ``_is_sentinel`` misses. Magenta is characterised by high red AND blue that
    are roughly equal, with green markedly lower — rare in real dashboards, so
    this is safe to recolour. Not used for page/viewport detection.
    """
    r, g, b = c[:3]
    return (r >= 140 and b >= 140 and abs(r - b) <= 40
            and g <= min(r, b) - 50)


def find_canvas_viewport(img) -> Optional[Tuple[int, int, int, int]]:
    """Return ``(left, top, right, bottom)`` of the Desktop canvas viewport.

    Uses only Power BI Desktop chrome: the light-grey canvas gutter (left edge +
    vertical span) and the light Filters/Visualizations/Fields pane band (right
    edge). Independent of the report theme.
    """
    W, H = img.size
    px = img.load()

    best_col = best_top = best_bottom = None
    best_run = 0
    for x in range(max(40, W // 40), min(W // 6, 160)):
        cur_top = None
        run = 0
        rtop = rbot = 0
        for y in range(H):
            if _is_gutter(px[x, y]):
                if cur_top is None:
                    cur_top = y
                if y - cur_top + 1 > run:
                    run, rtop, rbot = y - cur_top + 1, cur_top, y
            else:
                cur_top = None
        if run > best_run:
            best_run, best_col, best_top, best_bottom = run, x, rtop, rbot
    if best_col is None or best_run < H * 0.3:
        return None

    mid = (best_top + best_bottom) // 2
    left = best_col
    while left < W and _is_gutter(px[left, mid]):
        left += 1

    right = W - 1
    step = max(1, (best_bottom - best_top) // 40)
    x = W - 1
    found_pane = False
    while x > left:
        ys = list(range(best_top, best_bottom, step))
        frac = sum(1 for yy in ys if _is_pane(px[x, yy])) / max(1, len(ys))
        if frac > 0.6:
            right, found_pane = x, True
            x -= 1
        else:
            break
    if found_pane:
        right -= 1
    if right - left < 100:
        return None
    return (left, best_top, right, best_bottom)


def find_page_rect(img, viewport, sentinel=SENTINEL_RGB) -> Optional[Tuple[int, int, int, int]]:
    """Bounding box of the report page inside the viewport, via sentinel margins.

    The page outspace is painted with the sentinel colour, so the page is the
    block of rows/columns that are *not* mostly sentinel. Using per-row /
    per-column sentinel fractions (rather than a raw pixel bbox) ignores stray
    non-sentinel pixels in the margin (dividers, scrollbars, antialiasing).

    Returns ``(left, top, right, bottom)`` in image pixels, or None if no
    sentinel margin is present (so the caller falls back to the whole window).
    """
    if viewport is None:
        return None
    L, T, R, B = viewport
    px = img.load()
    xs = list(range(L, R, max(1, (R - L) // 300)))
    ys = list(range(T, B, max(1, (B - T) // 300)))
    if not xs or not ys:
        return None

    def row_sfrac(y):
        return sum(1 for x in xs if _is_sentinel(px[x, y])) / len(xs)

    # overall sentinel presence — if the paint didn't take, bail to fallback
    overall = sum(row_sfrac(y) for y in ys) / len(ys)
    if overall < 0.02:
        return None

    MARGIN = 0.85
    content_rows = [y for y in range(T, B) if row_sfrac(y) < MARGIN]
    if not content_rows:
        return None
    top, bottom = content_rows[0], content_rows[-1]

    def col_sfrac(x):
        yy = range(top, bottom, max(1, (bottom - top) // 200))
        n = len(list(yy))
        return sum(1 for y in range(top, bottom, max(1, (bottom - top) // 200))
                   if _is_sentinel(px[x, y])) / max(1, n)

    content_cols = [x for x in range(L, R) if col_sfrac(x) < MARGIN]
    if not content_cols:
        return None
    left, right = content_cols[0], content_cols[-1]
    if right - left < 100 or bottom - top < 40:
        return None
    return (left, top, right + 1, bottom + 1)


def find_page_by_sentinel(img) -> Optional[Tuple[int, int, int, int]]:
    """Bounding box of the report page found from the SENTINEL FRAME alone.

    Power BI Desktop honours ``FitToPage`` on a large enough window, rendering
    the WHOLE page surrounded by the magenta outspace. Depending on the page's
    aspect ratio vs the canvas, the outspace appears as a frame on all sides, a
    LEFT/RIGHT pillarbox (tall pages), or a TOP/BOTTOM letterbox (wide pages).
    This detector handles all three and is independent of Desktop chrome and the
    page's own background colour (works for dark themes too).

    Method: the magenta outspace lives only inside the canvas, so the bounding
    box of all sentinel samples IS the canvas. The page is the inner rectangle
    of that canvas whose rows/columns are NOT dominated by sentinel.

    Returns ``(left, top, right, bottom)`` in image pixels, or None.
    """
    if Image is None or img is None:
        return None
    W, H = img.size
    px = img.load()
    step_x = max(1, W // 600)
    step_y = max(1, H // 600)
    xs = list(range(0, W, step_x))
    ys = list(range(0, H, step_y))
    if len(xs) < 10 or len(ys) < 10:
        return None

    # Bounding box of every sentinel sample = the canvas (outspace + page).
    cx_min = cx_max = cy_min = cy_max = None
    total = 0
    for y in ys:
        for x in xs:
            if _is_sentinel(px[x, y]):
                total += 1
                if cx_min is None or x < cx_min:
                    cx_min = x
                if cx_max is None or x > cx_max:
                    cx_max = x
                if cy_min is None or y < cy_min:
                    cy_min = y
                if cy_max is None or y > cy_max:
                    cy_max = y
    if total < 20 or cx_min is None:
        return None

    canvas_xs = [x for x in xs if cx_min <= x <= cx_max]
    canvas_ys = [y for y in ys if cy_min <= y <= cy_max]
    if len(canvas_xs) < 4 or len(canvas_ys) < 4:
        return None

    # Page rows: rows inside the canvas not dominated by sentinel outspace.
    def row_sfrac(y):
        return sum(1 for x in canvas_xs if _is_sentinel(px[x, y])) / len(canvas_xs)

    page_rows = [y for y in canvas_ys if row_sfrac(y) < 0.5]
    if not page_rows:
        return None
    page_top, page_bottom = page_rows[0], page_rows[-1]

    # Page columns: over the page's vertical span, columns not dominated by
    # sentinel (the left/right outspace bars, if any).
    pspan = [y for y in canvas_ys if page_top <= y <= page_bottom]
    if not pspan:
        return None

    def col_sfrac(x):
        return sum(1 for y in pspan if _is_sentinel(px[x, y])) / len(pspan)

    page_cols = [x for x in canvas_xs if col_sfrac(x) < 0.5]
    if not page_cols:
        return None
    page_left, page_right = page_cols[0], page_cols[-1]
    if page_right - page_left < 80 or page_bottom - page_top < 40:
        return None
    return (page_left, page_top, page_right + step_x, page_bottom + step_y)


def _neutralize_sentinel(img, fill: Tuple[int, int, int] = (255, 255, 255)):
    """Replace any leftover sentinel-magenta pixels with ``fill`` (default white).

    Pages with a transparent/no page background (e.g. ``#00000000``) let the
    magenta outspace show through the empty page area, so cropped visuals would
    otherwise carry a magenta backdrop. Recolouring those pixels keeps the real
    visual content intact while giving a clean, neutral background.
    """
    if Image is None or img is None:
        return img
    img = img.convert("RGB")
    px = img.load()
    W, H = img.size
    for y in range(H):
        for x in range(W):
            if _is_sentinelish(px[x, y]):
                px[x, y] = fill
    return img


def clean_page_image(img, page_w: float):
    """Return ``(page_image, scale)`` cropped to the report page, or None.

    ``page_image`` has its top-left at logical ``(0, 0)`` and ``scale`` is
    ``page_px_width / page_w`` so a visual at logical ``x`` is at pixel
    ``x * scale`` in the returned image.
    """
    rect = find_page_by_sentinel(img)
    if rect is None:
        vp = find_canvas_viewport(img)
        rect = find_page_rect(img, vp)
    if not rect or not page_w:
        return None
    page_img = _neutralize_sentinel(img.crop(rect))
    scale = (rect[2] - rect[0]) / float(page_w)
    return page_img, scale


def clean_page_full(img, page_h: float, page_w: float = 0.0):
    """Return ``(page_image, scale, complete)`` for the captured report page.

    The page is located from the magenta sentinel frame (monitor/zoom
    independent). ``scale`` is ``page_px_width / page_w`` when ``page_w`` is
    given (Power BI fits the page uniformly, so width- and height-derived scales
    agree); otherwise it falls back to ``page_px_height / page_h``.

    ``complete`` means the WHOLE page height was captured (not clipped at the
    viewport fold). The reliable signal is a SENTINEL (magenta outspace) margin
    BELOW the page inside the canvas viewport: a fully-fit page is letter-/pillar-
    boxed by the outspace on every side, whereas a page clipped at the fold runs
    straight to the bottom edge of the canvas with no outspace beneath it. An
    aspect-ratio match alone is unreliable here — a 1700-wide page clipped at the
    fold has almost the same pixel aspect as a genuinely widened page — so we
    require the bottom margin to be present.
    """
    rect = find_page_by_sentinel(img)
    if rect is None:
        vp = find_canvas_viewport(img)
        rect = find_page_rect(img, vp)
    if not rect or not page_h:
        return None
    pl, pt, pr, pb = rect
    px_w, px_h = (pr - pl), (pb - pt)
    if px_h <= 0:
        return None
    if page_w and page_w > 0:
        scale = px_w / float(page_w)
    else:
        scale = px_h / float(page_h)
    complete = _has_bottom_margin(img, rect)
    page_img = _neutralize_sentinel(img.crop(rect))
    return page_img, scale, complete


def _has_bottom_margin(img, rect, band: int = 14) -> bool:
    """True when magenta outspace appears just BELOW the page rectangle.

    A complete (fully-fit) capture has the page surrounded by the sentinel
    outspace, so a band immediately under ``rect``'s bottom edge — but still
    inside the canvas — is dominated by sentinel pixels. A page clipped at the
    viewport fold instead runs to the canvas bottom edge, so that band is either
    off-image or non-sentinel. Robust to small monitors where the bottom margin
    is only a few percent of the page height.
    """
    if Image is None or img is None or not rect:
        return False
    W, H = img.size
    pl, pt, pr, pb = rect
    y0 = pb + 2
    y1 = min(H, pb + 2 + band)
    if y0 >= H or y1 - y0 < 3:
        return False                      # no room below ⇒ clipped at the fold
    px = img.load()
    step_x = max(1, (pr - pl) // 200)
    sentinel = total = 0
    for y in range(y0, y1):
        for x in range(pl, pr, step_x):
            total += 1
            if _is_sentinel(px[x, y]):
                sentinel += 1
    return total > 0 and (sentinel / total) >= 0.6



def crop_visual(page_img, scale: float, position: Dict):
    """Crop one visual from a clean page image (top-left = logical 0,0)."""
    if page_img is None or not scale:
        return None
    W, H = page_img.size
    vx = float(position.get("x", 0))
    vy = float(position.get("y", 0))
    vw = float(position.get("width", 0))
    vh = float(position.get("height", 0))
    if vw <= 0 or vh <= 0:
        return None
    pad = 2
    x0 = max(0, int(vx * scale) - pad)
    y0 = max(0, int(vy * scale) - pad)
    x1 = min(W, int((vx + vw) * scale) + pad)
    y1 = min(H, int((vy + vh) * scale) + pad)
    if x1 - x0 < 8 or y0 >= H - 4:
        return None
    if (y1 - y0) / max(1.0, vh * scale) < 0.4:
        return None  # mostly scrolled out of frame
    return _neutralize_sentinel(page_img.crop((x0, y0, x1, y1)))
