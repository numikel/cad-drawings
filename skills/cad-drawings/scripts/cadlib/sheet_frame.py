"""Sheet frame, printable area, text boxes and plotted-PDF edges: the geometry behind ``qa``.

Everything here is a mechanical estimate that a script can decide; whether a sheet looks right
stays with the agent (see references/visual-qa.md).

* The frame of a layout is the largest closed axis-aligned rectangle in paper space (a closed
  polyline or four lines, also inside blocks) that covers most of the paper.
* ``PaperSetup`` turns the page setup into boxes in layout units: the paper, and the printable
  area. The layout origin is the corner of the printable area plus the plot origin offset.
* Text sizes are estimates. ezdxf's MTEXT box uses the stored reference rectangle, which goes
  stale after an edit, so the lines are wrapped here with the style's font.
* A plotted PDF is rasterised (pypdfium2) because the vector bounds include white background
  rectangles and ignore clipping paths. Pillow and pypdfium2 are imported only when a PDF is
  analysed, so importing this module needs neither.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ezdxf.bbox
from ezdxf.fonts import fonts as ezfonts
from ezdxf.lldxf import const
from ezdxf.math import Vec2, Vec3
from ezdxf.tools.text import text_wrap
from ezdxf.tools.text_size import get_font_name

from .printing import MAX_NESTING
from .util import Deadline

if TYPE_CHECKING:
    from PIL import Image

Box = tuple[float, float, float, float]

FRAME_MIN_COVERAGE = 0.60  # a frame covers at least this share of the paper
FRAME_EDGE_TOL_MM = 0.5
RECT_TOL_MM = 0.5  # how far from axis-aligned and closed a rectangle may be
PAPER_OUTLINE_MATCH_MM = 1.0  # a rectangle this close to the paper edge is the paper outline
TEXT_TOLERANCE = 0.10
PDF_RASTER_DPI = 100
PDF_RASTER_MAX_PIXELS = 40_000_000
PDF_RASTER_MAX_PAGES = 20
PDF_INK_LEVEL = 250  # grey values below this are ink
PDF_EDGE_TOL_MM = 0.5
PDF_TRIM_COVERAGE = 0.90  # a line along this share of a page edge is the trim outline
PDF_TRIM_MAX_MM = 2.0  # how thick a trim outline is searched for
PDF_FRAME_LINE_COVERAGE = 0.5  # a frame side is a line over this share of the expected side
PDF_SHIFT_TOL_MM = 2.0
MAX_FRAME_LINES = 60  # longest horizontal and vertical lines considered when pairing lines


# --------------------------------------------------------------------------------------
# page setup
# --------------------------------------------------------------------------------------


def _handle_key(handle: str) -> int:
    try:
        return int(handle, 16)
    except ValueError:
        return 0


@dataclass(frozen=True)
class PaperSetup:
    """A layout's paper and printable area, in layout units, plus what decides their meaning."""

    paper_box: Box
    printable_box: Box
    unit_mm: float | None  # millimetres per layout unit; None when the layout is in pixels
    plot_type: int
    rotation: int
    scale: tuple[float, float]  # (numerator, denominator); (0, 0) when it is not a plain ratio
    centered: bool
    margins_mm: tuple[float, float, float, float]  # left, bottom, right, top
    offset_mm: tuple[float, float]  # plot origin offset (x, y)
    paper_mm: tuple[float, float]
    limits_missing: bool  # the paper limits were unusable and the paper size was used instead

    def outside_skip_reason(self) -> str | None:
        """Why the frame cannot be compared with the printable area, or None."""
        if self.unit_mm is None:
            return "the layout is measured in pixels"
        if self.limits_missing:
            return "the layout has no usable paper limits"
        if self.rotation:
            return "the page is rotated in the page setup"
        return None

    def position_skip_reason(self) -> str | None:
        """Why the frame's place on the plotted page cannot be predicted, or None."""
        reason = self.outside_skip_reason()
        if reason:
            return reason
        if self.plot_type != 5:
            return f"the plot area is not the layout (plot type {self.plot_type})"
        num, den = self.scale
        if num <= 0 or den <= 0 or not math.isclose(num / den, 1.0, rel_tol=1e-6):
            return "the plot scale is not 1:1"
        if self.centered:
            return "the plot is centered on the page"
        return None

    def page_box(self, box: Box) -> Box:
        """A layout-unit box as millimetres from the lower-left corner of the page (y up)."""
        unit = self.unit_mm or 1.0
        ox = self.margins_mm[0] + self.offset_mm[0]
        oy = self.margins_mm[1] + self.offset_mm[1]
        return (box[0] * unit + ox, box[1] * unit + oy, box[2] * unit + ox, box[3] * unit + oy)


def paper_setup(layout: Any) -> PaperSetup | None:
    """Page setup of a paper layout; None when it has no paper size at all."""
    d = layout.dxf
    unit_mm = {0: 25.4, 1: 1.0}.get(int(d.get("plot_paper_units", 1)))
    paper_w = float(d.get("paper_width", 0.0))
    paper_h = float(d.get("paper_height", 0.0))
    lo, hi = layout.get_paper_limits()
    missing = hi.x - lo.x <= 1e-9 or hi.y - lo.y <= 1e-9
    if missing:
        if paper_w <= 1e-9 or paper_h <= 1e-9:
            return None
        unit = unit_mm or 1.0
        lo, hi = Vec2(0, 0), Vec2(paper_w / unit, paper_h / unit)
    left = float(d.get("left_margin", 0.0))
    bottom = float(d.get("bottom_margin", 0.0))
    right = float(d.get("right_margin", 0.0))
    top = float(d.get("top_margin", 0.0))
    unit = unit_mm or 1.0
    printable = (lo.x + left / unit, lo.y + bottom / unit, hi.x - right / unit, hi.y - top / unit)
    flags = int(d.get("plot_layout_flags", 0))
    if flags & 16:  # a standard scale is in use
        std = const.STD_SCALES.get(int(d.get("standard_scale_type", 16)))
        scale = (float(std[0]), float(std[1])) if std else (0.0, 0.0)
    else:
        scale = (float(d.get("scale_numerator", 1.0)), float(d.get("scale_denominator", 1.0)))
    return PaperSetup(
        paper_box=(lo.x, lo.y, hi.x, hi.y),
        printable_box=printable,
        unit_mm=unit_mm,
        plot_type=int(d.get("plot_type", 5)),
        rotation=int(d.get("plot_rotation", 0)),
        scale=scale,
        centered=bool(flags & 4),
        margins_mm=(left, bottom, right, top),
        offset_mm=(
            float(d.get("plot_origin_x_offset", 0.0)),
            float(d.get("plot_origin_y_offset", 0.0)),
        ),
        paper_mm=(paper_w, paper_h),
        limits_missing=missing,
    )


def unprintable_layers(layers: Any) -> frozenset[str]:
    """Lower-case names of layers that are off, frozen or not plotted."""
    return frozenset(
        lay.dxf.name.lower()
        for lay in layers
        if lay.is_off() or lay.is_frozen() or not lay.dxf.get("plot", 1)
    )


# --------------------------------------------------------------------------------------
# frame detection
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    box: Box
    handle: str  # the polyline, the lowest of the four lines, or the INSERT that holds it
    layer: str
    kind: str  # "polyline" | "lines"


@dataclass(frozen=True)
class _Line:
    coord: float  # y of a horizontal line, x of a vertical one
    lo: float
    hi: float
    handle: str
    layer: str


def _walk(
    layout: Any, unprintable: frozenset[str], deadline: Deadline | None
) -> Iterator[tuple[Any, str, str]]:
    """Entities of a layout and of the blocks inserted in it: (entity, layer, handle).

    A virtual entity on layer "0" takes the layer of the INSERT that holds it, and the handle
    reported is that of the outermost INSERT. MINSERT arrays are not expanded.
    """

    def inside(insert: Any, layer: str, handle: str, depth: int) -> Iterator[tuple[Any, str, str]]:
        if depth > MAX_NESTING or insert.mcount > 1:
            return
        try:
            content = list(insert.virtual_entities())
        except (ValueError, ZeroDivisionError, AttributeError, const.DXFError):
            return
        for entity in content:
            inner = entity.dxf.get("layer", "0")
            inner = layer if inner == "0" else inner
            if inner.lower() in unprintable:
                continue
            if entity.dxftype() == "INSERT":
                yield from inside(entity, inner, handle, depth + 1)
            else:
                yield entity, inner, handle

    for i, entity in enumerate(layout):
        if deadline is not None and i % 500 == 0:
            deadline.check()
        layer = str(entity.dxf.get("layer", "0"))
        if layer.lower() in unprintable:
            continue
        handle = str(entity.dxf.get("handle", ""))
        if entity.dxftype() == "INSERT":
            yield from inside(entity, layer, handle, 1)
        else:
            yield entity, layer, handle


def _corner_box(points: list[Vec3], tol: float) -> Box | None:
    """The box of four points that are the corners of an axis-aligned rectangle."""
    if len(points) == 5 and points[0].distance(points[4]) <= tol:
        points = points[:4]
    if len(points) != 4:
        return None
    x1, x2 = min(p.x for p in points), max(p.x for p in points)
    y1, y2 = min(p.y for p in points), max(p.y for p in points)
    if x2 - x1 <= tol or y2 - y1 <= tol:
        return None
    corners = {(x, y) for x in (x1, x2) for y in (y1, y2)}
    for point in points:
        hit = next(
            (c for c in corners if abs(point.x - c[0]) <= tol and abs(point.y - c[1]) <= tol),
            None,
        )
        if hit is None:
            return None
        corners.discard(hit)
    return (x1, y1, x2, y2)


def _polyline_box(entity: Any, tol: float) -> Box | None:
    kind = entity.dxftype()
    try:
        if kind == "LWPOLYLINE":
            if entity.has_arc:
                return None
            points = list(entity.vertices_in_wcs())
            closed = bool(entity.closed)
        elif kind == "POLYLINE":
            if not entity.is_2d_polyline or any(v.dxf.get("bulge", 0.0) for v in entity.vertices):
                return None
            points = list(entity.points_in_wcs())
            closed = bool(entity.is_closed)
        else:
            return None
    except (ValueError, AttributeError, const.DXFError):
        return None
    if not closed and not (len(points) == 5 and points[0].distance(points[4]) <= tol):
        return None
    return _corner_box(points, tol)


def _pair_lines(
    horizontals: list[_Line], verticals: list[_Line], tol: float
) -> Iterator[tuple[Box, list[_Line]]]:
    """Rectangles made of two horizontal and two vertical lines that meet at the corners."""
    hs = sorted(horizontals, key=lambda ln: ln.lo - ln.hi)[:MAX_FRAME_LINES]
    vs = sorted(verticals, key=lambda ln: ln.lo - ln.hi)[:MAX_FRAME_LINES]
    for i, a in enumerate(hs):
        for b in hs[i + 1 :]:
            if abs(a.lo - b.lo) > tol or abs(a.hi - b.hi) > tol or abs(a.coord - b.coord) <= tol:
                continue
            x1, x2 = min(a.lo, b.lo), max(a.hi, b.hi)
            y1, y2 = sorted((a.coord, b.coord))
            left = next(
                (
                    v
                    for v in vs
                    if abs(v.coord - x1) <= tol and abs(v.lo - y1) <= tol and abs(v.hi - y2) <= tol
                ),
                None,
            )
            right = next(
                (
                    v
                    for v in vs
                    if abs(v.coord - x2) <= tol and abs(v.lo - y1) <= tol and abs(v.hi - y2) <= tol
                ),
                None,
            )
            if left is not None and right is not None:
                yield (x1, y1, x2, y2), [a, b, left, right]


def find_frame(
    layout: Any,
    setup: PaperSetup,
    *,
    frame_layer: str | None = None,
    unprintable: frozenset[str] = frozenset(),
    deadline: Deadline | None = None,
) -> Frame | None:
    """The sheet frame of a paper layout, or None.

    Without ``frame_layer``: the largest candidate that covers at least ``FRAME_MIN_COVERAGE`` of
    the paper, but not the paper outline itself when another candidate exists. With it: the
    largest candidate on that layer, whatever its size.
    """
    unit = setup.unit_mm or 1.0
    tol = RECT_TOL_MM / unit
    wanted = frame_layer.lower() if frame_layer else None
    px1, py1, px2, py2 = setup.paper_box
    paper_w, paper_h = px2 - px1, py2 - py1
    candidates: list[Frame] = []
    horizontals: list[_Line] = []
    verticals: list[_Line] = []
    for entity, layer, handle in _walk(layout, unprintable, deadline):
        if wanted is not None and layer.lower() != wanted:
            continue
        kind = entity.dxftype()
        if kind in ("LWPOLYLINE", "POLYLINE"):
            box = _polyline_box(entity, tol)
            if box is not None:
                candidates.append(Frame(box, handle, layer, "polyline"))
        elif kind == "LINE":
            a, b = entity.dxf.start, entity.dxf.end
            dx, dy = abs(b.x - a.x), abs(b.y - a.y)
            long_enough = FRAME_MIN_COVERAGE if wanted is None else 0.0
            if dy <= tol and dx > tol and dx >= long_enough * paper_w:
                horizontals.append(_Line(a.y, min(a.x, b.x), max(a.x, b.x), handle, layer))
            elif dx <= tol and dy > tol and dy >= long_enough * paper_h:
                verticals.append(_Line(a.x, min(a.y, b.y), max(a.y, b.y), handle, layer))
    for box, parts in _pair_lines(horizontals, verticals, tol):
        first = min(parts, key=lambda ln: _handle_key(ln.handle))
        candidates.append(Frame(box, first.handle, first.layer, "lines"))
    if wanted is None:
        paper_area = paper_w * paper_h
        candidates = [c for c in candidates if _area(c.box) >= FRAME_MIN_COVERAGE * paper_area]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-_area(c.box), c.kind != "polyline", _handle_key(c.handle)))
    outline_tol = PAPER_OUTLINE_MATCH_MM / unit
    for candidate in candidates:
        if any(
            abs(a - b) > outline_tol for a, b in zip(candidate.box, setup.paper_box, strict=True)
        ):
            return candidate
    return candidates[0]


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def overshoot_mm(box: Box, limit: Box, unit_mm: float) -> dict[str, float]:
    """How far ``box`` leaves ``limit``, per side, in millimetres (sides within the limit omitted)."""
    out = {
        "left": (limit[0] - box[0]) * unit_mm,
        "bottom": (limit[1] - box[1]) * unit_mm,
        "right": (box[2] - limit[2]) * unit_mm,
        "top": (box[3] - limit[3]) * unit_mm,
    }
    return {side: value for side, value in out.items() if value > FRAME_EDGE_TOL_MM}


# --------------------------------------------------------------------------------------
# text boxes (estimates)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextItem:
    space: str  # layout name, "Model" for model space
    handle: str
    kind: str  # TEXT | MTEXT | ATTRIB
    box: Box
    lines: int | None  # number of lines of an MTEXT (None: not an MTEXT or has columns)


@functools.lru_cache(maxsize=256)
def _font(name: str, cap_height: float) -> Any:
    return ezfonts.make_font(name, cap_height, 1.0)


def _mtext_lines(entity: Any) -> tuple[list[str], float, Any] | None:
    """Wrapped lines, character height and font of an MTEXT; None for columns or no text."""
    if entity.has_columns:
        return None
    height = float(entity.dxf.get("char_height", 0.0))
    if height <= 0:
        height = 2.5
    text = entity.plain_text(split=False)
    if not text or text.isspace():
        return None
    font = _font(get_font_name(entity), height)
    reference = float(entity.dxf.get("width", 0.0))
    lines = text_wrap(text, reference if reference > 0 else None, font.text_width)
    return (lines, height, font) if lines else None


_ESTIMATE_ERRORS = (ValueError, ZeroDivisionError, AttributeError, TypeError, const.DXFError)


def mtext_lines(entity: Any) -> int | None:
    """Number of lines an MTEXT wraps to with its reference width; None when it cannot be told."""
    try:
        measured = _mtext_lines(entity)
    except _ESTIMATE_ERRORS:
        return None
    return None if measured is None else len(measured[0])


def _mtext_box(entity: Any) -> Box | None:
    measured = _mtext_lines(entity)
    if measured is None:
        return None
    lines, height, font = measured
    width = max(font.text_width(line) for line in lines)
    pitch = height * (5.0 / 3.0) * float(entity.dxf.get("line_spacing_factor", 1.0) or 1.0)
    total = height + (len(lines) - 1) * pitch
    point = int(entity.dxf.get("attachment_point", 1))
    column = (point - 1) % 3  # 0 left, 1 centre, 2 right
    row = (point - 1) // 3  # 0 top, 1 middle, 2 bottom
    reference = float(entity.dxf.get("width", 0.0))
    box_width = reference if reference > 0 else width  # the text is left-aligned in this box
    x1 = -box_width * column / 2
    y2 = (0.0, total / 2, total)[row]
    corners = [(x1, y2 - total), (x1 + width, y2 - total), (x1 + width, y2), (x1, y2)]
    angle = math.radians(float(entity.get_rotation()))
    cos, sin = math.cos(angle), math.sin(angle)
    ins = entity.dxf.insert
    xs = [ins.x + x * cos - y * sin for x, y in corners]
    ys = [ins.y + x * sin + y * cos for x, y in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def text_box(entity: Any) -> Box | None:
    """Estimated box of a TEXT, MTEXT or ATTRIB in its own space; None when it cannot be told."""
    try:
        if entity.dxftype() == "MTEXT":
            return _mtext_box(entity)
        box = ezdxf.bbox.extents([entity], fast=True)
    except _ESTIMATE_ERRORS:
        return None
    if not box.has_data:
        return None
    return (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)


def _text_value(entity: Any) -> str:
    if entity.dxftype() == "MTEXT":
        return str(entity.text or "")
    return str(entity.dxf.get("text", ""))


def collect_texts(
    doc: Any,
    unprintable: frozenset[str],
    deadline: Deadline,
    *,
    spaces: set[str] | None = None,
) -> list[TextItem]:
    """Visible, printable, non-empty texts of model space and every layout (not of block
    definitions, except through the attributes of INSERTs)."""
    items: list[TextItem] = []
    count = 0
    for name in doc.layouts.names_in_taborder():
        space = "Model" if name.lower() == "model" else name
        if spaces is not None and space not in spaces:
            continue
        for entity in doc.layouts.get(name):
            count += 1
            if count % 200 == 0:
                deadline.check()
            kind = entity.dxftype()
            layer = str(entity.dxf.get("layer", "0")).lower()
            if layer in unprintable:
                continue
            if kind in ("TEXT", "MTEXT"):
                candidates = [entity]
            elif kind == "INSERT":
                candidates = [
                    a
                    for a in entity.attribs
                    if not a.is_invisible
                    and str(a.dxf.get("layer", "0")).lower() not in unprintable
                ]
            else:
                continue
            for text in candidates:
                if not _text_value(text).strip():
                    continue
                box = text_box(text)
                if box is None:
                    continue
                lines = mtext_lines(text) if text.dxftype() == "MTEXT" else None
                items.append(TextItem(space, str(text.dxf.handle), text.dxftype(), box, lines))
    return items


def outside_frame(frame: Box, box: Box) -> bool:
    """True when ``box`` leaves ``frame`` by more than ``TEXT_TOLERANCE`` of its own size."""
    tol_x = max(TEXT_TOLERANCE * (box[2] - box[0]), 1e-9)
    tol_y = max(TEXT_TOLERANCE * (box[3] - box[1]), 1e-9)
    return (
        frame[0] - box[0] > tol_x
        or box[2] - frame[2] > tol_x
        or frame[1] - box[1] > tol_y
        or box[3] - frame[3] > tol_y
    )


def grew(old: Box, new: Box) -> bool:
    """True when the width or the height grew by more than ``TEXT_TOLERANCE``."""
    old_w, old_h = old[2] - old[0], old[3] - old[1]
    new_w, new_h = new[2] - new[0], new[3] - new[1]
    return (old_w > 0 and new_w > old_w * (1 + TEXT_TOLERANCE)) or (
        old_h > 0 and new_h > old_h * (1 + TEXT_TOLERANCE)
    )


# --------------------------------------------------------------------------------------
# plotted PDF: ink at the page edges, and where the frame is
# --------------------------------------------------------------------------------------


@dataclass
class PageInk:
    """The inked area of one rasterised PDF page (millimetres, y up from the page bottom)."""

    page: int  # 1-based
    size_mm: tuple[float, float]
    px_mm: tuple[float, float]  # size of one pixel
    mask: Image.Image  # mode "L": 255 where there is ink, trim outline removed
    trimmed: list[str] = field(default_factory=list)  # sides where a trim outline was removed
    bbox_mm: Box | None = None
    gaps_mm: dict[str, float] = field(default_factory=dict)  # ink to page edge, per side


def _box_filter() -> Any:
    from PIL import Image

    resampling = getattr(Image, "Resampling", Image)
    return resampling.BOX


def _profiles(mask: Image.Image) -> tuple[bytes, bytes]:
    """Share of inked pixels (0..255) in every column and in every row."""
    width, height = mask.size
    box = _box_filter()
    return mask.resize((width, 1), box).tobytes(), mask.resize((1, height), box).tobytes()


def _edge_run(profile: bytes, limit: int, from_end: bool) -> int:
    """How many pixels from a page edge belong to a line along at least PDF_TRIM_COVERAGE of it."""
    threshold = PDF_TRIM_COVERAGE * 255
    count = 0
    while count < min(limit, len(profile)):
        value = profile[-1 - count] if from_end else profile[count]
        if value < threshold:
            break
        count += 1
    return count


def pdf_pages_ink(
    path: Path, *, max_pages: int = PDF_RASTER_MAX_PAGES, deadline: Deadline | None = None
) -> tuple[list[PageInk], int]:
    """Rasterise up to ``max_pages`` pages and measure their ink; also returns the page count.

    Raises ImportError when Pillow or pypdfium2 is missing.
    """
    import pypdfium2 as pdfium
    from PIL import Image  # noqa: F401  (the missing-Pillow error must surface here)

    document = pdfium.PdfDocument(str(path))
    pages: list[PageInk] = []
    try:
        total = len(document)
        for index in range(min(total, max_pages)):
            if deadline is not None:
                deadline.check()
            page = document[index]
            width_pt, height_pt = page.get_size()
            dpi = float(PDF_RASTER_DPI)
            pixels = (width_pt / 72 * dpi) * (height_pt / 72 * dpi)
            if pixels > PDF_RASTER_MAX_PIXELS:
                dpi *= 0.999 * math.sqrt(
                    PDF_RASTER_MAX_PIXELS / pixels
                )  # rounding up must not exceed it
            if deadline is not None:
                deadline.check()
            grey = page.render(scale=dpi / 72.0, grayscale=True).to_pil().convert("L")
            mask = grey.point(lambda v: 255 if v < PDF_INK_LEVEL else 0)
            size_mm = (width_pt / 72 * 25.4, height_pt / 72 * 25.4)
            px = (size_mm[0] / mask.width, size_mm[1] / mask.height)
            ink = PageInk(index + 1, size_mm, px, mask)
            _measure_ink(ink)
            pages.append(ink)
    finally:
        document.close()
    return pages, total


def _measure_ink(ink: PageInk) -> None:
    """Remove a trim outline along the page edges, then find the bounding box of what is left."""
    mask = ink.mask
    width, height = mask.size
    cols, rows = _profiles(mask)
    limit_x = max(1, int(PDF_TRIM_MAX_MM / ink.px_mm[0]))
    limit_y = max(1, int(PDF_TRIM_MAX_MM / ink.px_mm[1]))
    runs = {
        "left": _edge_run(cols, limit_x, False),
        "right": _edge_run(cols, limit_x, True),
        "top": _edge_run(rows, limit_y, False),
        "bottom": _edge_run(rows, limit_y, True),
    }
    if runs["left"]:
        mask.paste(0, (0, 0, runs["left"], height))
    if runs["right"]:
        mask.paste(0, (width - runs["right"], 0, width, height))
    if runs["top"]:
        mask.paste(0, (0, 0, width, runs["top"]))
    if runs["bottom"]:
        mask.paste(0, (0, height - runs["bottom"], width, height))
    ink.trimmed = [side for side, n in runs.items() if n]
    bbox = mask.getbbox()
    if bbox is None:
        return
    px, py = ink.px_mm
    page_h = ink.size_mm[1]
    ink.bbox_mm = (bbox[0] * px, page_h - bbox[3] * py, bbox[2] * px, page_h - bbox[1] * py)
    ink.gaps_mm = {
        "left": bbox[0] * px,
        "right": (width - bbox[2]) * px,
        "top": bbox[1] * py,
        "bottom": (height - bbox[3]) * py,
    }


def _line_cluster(profile: bytes, threshold: float, last: bool) -> tuple[int, int] | None:
    """First (or last) run of consecutive indexes at or above ``threshold``: (start, end)."""
    hits = {i for i, v in enumerate(profile) if v >= threshold}
    if not hits:
        return None
    if last:
        end = max(hits)
        start = end
        while start - 1 in hits:
            start -= 1
        return start, end
    start = min(hits)
    end = start
    while end + 1 in hits:
        end += 1
    return start, end


def frame_on_page(ink: PageInk, expected: Box) -> Box | None:
    """Where the frame is on a rasterised page, as a page box in mm (y up); None if not seen.

    The frame sides are the outermost columns and rows that are inked along at least
    ``PDF_FRAME_LINE_COVERAGE`` of the length they should have. The position of a side is the
    centre of its line.
    """
    page_w, page_h = ink.size_mm
    expect_w, expect_h = expected[2] - expected[0], expected[3] - expected[1]
    if expect_w <= 0 or expect_h <= 0:
        return None
    cols, rows = _profiles(ink.mask)
    col_limit = 255 * PDF_FRAME_LINE_COVERAGE * expect_h / page_h
    row_limit = 255 * PDF_FRAME_LINE_COVERAGE * expect_w / page_w
    left = _line_cluster(cols, col_limit, last=False)
    right = _line_cluster(cols, col_limit, last=True)
    top = _line_cluster(rows, row_limit, last=False)
    bottom = _line_cluster(rows, row_limit, last=True)
    if left is None or right is None or top is None or bottom is None:
        return None
    px, py = ink.px_mm
    x1 = (left[0] + left[1] + 1) / 2 * px
    x2 = (right[0] + right[1] + 1) / 2 * px
    y_top = page_h - (top[0] + top[1] + 1) / 2 * py
    y_bottom = page_h - (bottom[0] + bottom[1] + 1) / 2 * py
    if x2 - x1 < 0.5 * expect_w or y_top - y_bottom < 0.5 * expect_h:
        return None
    # a printed frame is a closed rectangle: every side is inked along its whole length
    box = _box_filter()
    sides = (
        (left[0], top[0], left[1] + 1, bottom[1] + 1),
        (right[0], top[0], right[1] + 1, bottom[1] + 1),
        (left[0], top[0], right[1] + 1, top[1] + 1),
        (left[0], bottom[0], right[1] + 1, bottom[1] + 1),
    )
    for strip in sides:
        if strip[2] <= strip[0] or strip[3] <= strip[1]:
            return None
        share = ink.mask.crop(strip).resize((1, 1), box).tobytes()[0]
        if share < PDF_TRIM_COVERAGE * 255:
            return None
    return (x1, y_bottom, x2, y_top)
