"""Render layouts to PNG: a CAD plot (COM) when available and agreed, otherwise ezdxf.

The ezdxf path is an approximation (substitute fonts, no plot style unless given). It applies the
workarounds that make the output usable:

1. viewports are normalised in memory (ezdxf drops every viewport with status <= 0 and the first
   one with status 1, which in exported files is often the real plan);
2. a plot style table is applied only when ``--ctb`` names a file;
3. SHX/support directories and a line weight factor can be given;
4. 1:1 geometry: the drawing area is the layout's paper limits, mapped to the image without
   fitting (``fit_page=False`` semantics);
5. layers can be hidden everywhere, viewports included (``--hide-layer``);
6. nothing from the AGPL PyMuPDF stack is imported unless ``--raster pymupdf`` is requested and
   the package is installed, and library output on stdout is redirected to stderr;
7. external references are embedded from disk (``\\`` normalised to ``/``, nested ones in a loop).

Every run writes into a fresh run directory; no older artifact is ever reused.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ezdxf
from ezdxf import recover

from .command import Command
from .drawing import _resolve_source, open_drawing
from .result import CadError, Result
from .util import (
    Deadline,
    atomic_write,
    conversion_options,
    parse_floats,
    path_is_local,
    safe_is_file,
)
from .viewports import layout_viewports

if TYPE_CHECKING:
    from ezdxf.document import Drawing
    from PIL import Image

    from .runs import RunContext

DEFAULT_DPI = 150
DEFAULT_MAX_PX = 2000
DEFAULT_LINEWEIGHT_SCALING = 0.87
MAX_MASTER_PIXELS = 50_000_000
MAX_CROP_SIDE_PX = 10_000
MAX_TILES = 100
MAX_LISTED_OUTPUTS = 12
XREF_NESTING = 5
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_PAPER_UNIT_MM = {0: 25.4, 1: 1.0}  # plot_paper_units: inches, millimetres (2 = pixels: unknown)


# --------------------------------------------------------------------------------------
# helpers shared by both backends
# --------------------------------------------------------------------------------------


def _pil() -> Any:
    """Pillow's ``Image`` module, imported when first needed (clean error when missing)."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise CadError(
            "MISSING_DEPENDENCY",
            "rendering needs the Python package Pillow, which is not installed",
            hint="ask the user, then: pip install pillow",
        ) from exc
    return Image


def png_name(stem: str, layout: str, suffix: str = "") -> str:
    """``<stem>__<layout>[<suffix>].png`` with characters that are illegal in file names replaced."""
    return f"{stem}__{_UNSAFE_NAME.sub('_', layout).strip() or 'layout'}{suffix}.png"


def assign_slugs(layouts: list[str]) -> dict[str, str]:
    """File-name slug per layout; names that collapse to the same slug get ``-2``, ``-3``...

    ``A/B``, ``A:B`` and ``A_B`` all sanitise to ``A_B``: each still gets its own file.
    Comparison is case-insensitive because Windows and macOS file systems are.
    """
    taken: set[str] = set()
    out: dict[str, str] = {}
    for name in layouts:
        base = _UNSAFE_NAME.sub("_", name).strip() or "layout"
        slug, n = base, 1
        while slug.lower() in taken:
            n += 1
            slug = f"{base}-{n}"
        taken.add(slug.lower())
        out[name] = slug
    return out


def save_png(image: Image.Image, path: Path) -> None:
    """Write a PNG through a temporary sibling and ``os.replace`` (never a half-written file)."""
    atomic_write(path, lambda tmp: image.save(tmp, format="PNG"))


def parse_tiles(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)[xX](\d+)", text.strip())
    if not match:
        raise CadError(
            "BAD_ARGS",
            f"--tiles must look like 3x2 (columns x rows), got {text!r}",
        )
    cols, rows = int(match.group(1)), int(match.group(2))
    if not (1 <= cols and 1 <= rows and cols * rows <= MAX_TILES):
        raise CadError(
            "BAD_ARGS",
            f"--tiles must be between 1x1 and {MAX_TILES} tiles in total",
        )
    return cols, rows


def cap_longest_side(image: Image.Image, max_px: int) -> Image.Image:
    """Downscale so that the longest side is at most ``max_px`` (never upscales)."""
    longest = max(image.size)
    if longest <= max_px:
        return image
    factor = max_px / longest
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    return image.resize(size, _pil().Resampling.LANCZOS)


def crop_to_pixels(
    box: tuple[float, float, float, float], size: tuple[int, int], crop: list[float]
) -> tuple[int, int, int, int]:
    """Map a crop given in layout coordinates (y up) to a pixel box (y down) of the master image."""
    x1, y1, x2, y2 = box
    cx1, cx2 = sorted((crop[0], crop[2]))
    cy1, cy2 = sorted((crop[1], crop[3]))
    width, height = x2 - x1, y2 - y1
    left = round((max(cx1, x1) - x1) / width * size[0])
    right = round((min(cx2, x2) - x1) / width * size[0])
    top = round((y2 - min(cy2, y2)) / height * size[1])
    bottom = round((y2 - max(cy1, y1)) / height * size[1])
    if right - left < 1 or bottom - top < 1:
        raise CadError(
            "BAD_ARGS",
            "--crop does not overlap the rendered area",
            hint=f"the layout covers x {x1:g}..{x2:g}, y {y1:g}..{y2:g}",
        )
    return left, top, right, bottom


def tile_boxes(
    region: tuple[int, int, int, int], cols: int, rows: int
) -> Iterator[tuple[int, int, tuple[int, int, int, int]]]:
    """Pixel boxes of a ``cols`` x ``rows`` grid over ``region``, row 1 at the top."""
    left, top, right, bottom = region
    for row in range(rows):
        for col in range(cols):
            yield (
                col + 1,
                row + 1,
                (
                    left + round((right - left) * col / cols),
                    top + round((bottom - top) * row / rows),
                    left + round((right - left) * (col + 1) / cols),
                    top + round((bottom - top) * (row + 1) / rows),
                ),
            )


def pymupdf_available() -> bool:
    """True when PyMuPDF is installed (checked without importing it: the import is AGPL-noisy)."""
    return importlib.util.find_spec("pymupdf") is not None


def pdf_to_image(pdf: Path, dpi: int, raster: str = "pdfium") -> Image.Image:
    """First page of ``pdf`` as an RGB image. pypdfium2 by default (permissive licence)."""
    if raster == "pymupdf":
        if not pymupdf_available():
            raise CadError(
                "MISSING_DEPENDENCY",
                "--raster pymupdf requested but PyMuPDF is not installed (AGPL, optional)",
                hint="use the default pypdfium2 raster or install PyMuPDF yourself",
            )
        import pymupdf  # type: ignore[import-not-found,unused-ignore]

        with contextlib.closing(pymupdf.open(str(pdf))) as doc:
            pix = doc[0].get_pixmap(dpi=dpi)
            return _pil().frombytes("RGB", (pix.width, pix.height), pix.samples)
    try:
        import pypdfium2 as pdfium
    except ImportError:
        try:
            from pdf2image import convert_from_path  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:
            raise CadError(
                "MISSING_DEPENDENCY",
                "no PDF rasteriser available (pypdfium2 is missing)",
                hint="pip install pypdfium2",
            ) from exc
        return convert_from_path(str(pdf), dpi=dpi, first_page=1, last_page=1)[0].convert("RGB")
    document = pdfium.PdfDocument(str(pdf))
    try:
        page = document[0]
        return page.render(scale=dpi / 72.0).to_pil().convert("RGB")  # type: ignore[no-any-return]
    finally:
        document.close()


# --------------------------------------------------------------------------------------
# ezdxf backend
# --------------------------------------------------------------------------------------


def normalise_viewports(layout: Any) -> list[str]:
    """Make ezdxf draw every real viewport of ``layout`` (in memory); returns warnings.

    ezdxf treats the lowest positive ``status`` as the layout's own "overall" viewport and
    drops it, and drops all viewports with status <= 0. Exported files do not follow that
    convention, so: the overall viewport (id 1, or view centre/height equal to its own) gets
    status 1, every other one status 2, 3, ... in file order; a placeholder overall viewport is
    added when there is none.
    """
    warnings: list[str] = []
    entities = list(layout.query("VIEWPORT"))
    if not entities:
        return warnings
    infos = layout_viewports(layout)
    name = layout.name
    if not any(info.overall for info in infos):
        placeholder = layout.add_viewport(
            center=(0, 0), size=(1, 1), view_center_point=(0, 0), view_height=1, status=1
        )
        placeholder.dxf.id = 1
        warnings.append(f"layout {name!r}: no overall viewport, added a placeholder in memory")
    order = 2
    for entity, info in zip(entities, infos, strict=True):
        if info.overall:
            entity.dxf.status = 1
            continue
        if info.status <= 0:
            warnings.append(
                f"layout {name!r}: viewport status {info.status} in the file is not reliable "
                "(COM-exported DXF reports it wrongly); drawn as on"
            )
        if not info.top_view:
            warnings.append(f"layout {name!r}: a non-top-view viewport cannot be rendered by ezdxf")
        entity.dxf.status = order
        order += 1
    return warnings


def embed_xrefs(doc: Drawing, source_dir: Path) -> list[str]:
    """Embed external references found on disk into the document (in memory); returns warnings.

    Backslashes in stored paths are normalised, relative paths are tried against the host's
    folder and then by file name alone (also in the folders of references found earlier, so a
    nested reference may sit beside its parent); nested references are resolved in a loop.
    """
    from ezdxf import xref

    warnings: list[str] = []
    done: set[str] = set()

    search_dirs = [source_dir]  # grows with the folders of references found on the way

    def locate(name: str) -> Path | None:
        raw = Path(name.replace("\\", "/"))
        candidates = [raw] if path_is_local(raw) else []
        for folder in search_dirs:
            candidates += [folder / raw.name]
            if path_is_local(raw):
                candidates.append(folder / raw)
        for candidate in candidates:
            if safe_is_file(candidate):
                parent = candidate.resolve().parent
                if parent not in search_dirs:
                    search_dirs.append(parent)
                return candidate
        return None

    def load(name: str) -> Drawing:
        found = locate(name)
        if found is None:
            raise FileNotFoundError(name)
        return recover.readfile(found)[0]

    for _ in range(XREF_NESTING):
        pending = [
            blk
            for blk in doc.blocks
            if int(blk.block.dxf.get("flags", 0)) & 4 and blk.name.lower() not in done
        ]
        if not pending:
            break
        for blk in pending:
            done.add(blk.name.lower())
            stored = str(blk.block.dxf.get("xref_path", ""))
            blk.block.dxf.xref_path = stored.replace("\\", "/")
            if not path_is_local(stored):
                warnings.append(
                    f"xref {blk.name!r} not checked: network or non-local path {stored}"
                )
                continue
            if stored.lower().endswith(".dwg"):
                warnings.append(
                    f"xref {blk.name!r} is a DWG ({stored}); convert it to DXF to embed"
                )
                continue
            try:
                xref.embed(blk, load_fn=load, search_paths=list(search_dirs))
            except FileNotFoundError:
                warnings.append(f"xref {blk.name!r} not found on disk ({stored}); drawn without it")
            except (ezdxf.DXFError, ezdxf.DXFVersionError, OSError, ValueError) as exc:
                warnings.append(f"xref {blk.name!r} not embedded: {type(exc).__name__}: {exc}")
    return warnings


@dataclass
class EzdxfOptions:
    dpi: int = DEFAULT_DPI
    max_px: int = DEFAULT_MAX_PX
    ctb: Path | None = None
    support_dirs: list[str] = field(default_factory=list)
    lineweight_scaling: float = DEFAULT_LINEWEIGHT_SCALING
    hide_layers: set[str] = field(default_factory=set)
    want_hires: bool = False  # crop/tiles requested: keep the master at full resolution


@dataclass
class Rendered:
    image: Image.Image  # master raster
    box: tuple[float, float, float, float]  # layout coordinates covered by the image
    dpi: float
    warnings: list[str] = field(default_factory=list)


def _render_box(
    layout: Any, doc: Drawing
) -> tuple[tuple[float, float, float, float], float | None, list[str]]:
    """``(box, mm per layout unit, warnings)``; the mm factor is None for model space."""
    import ezdxf.bbox

    warnings: list[str] = []
    if layout.name.lower() == "model":
        box = ezdxf.bbox.extents(layout, fast=True)
        if not box.has_data:
            raise CadError(
                "EMPTY_LAYOUT",
                "model space is empty, nothing to render",
            )
        return (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y), None, warnings
    units = int(layout.dxf.get("plot_paper_units", 1))
    factor = _PAPER_UNIT_MM.get(units)
    if factor is None:
        factor = 1.0
        warnings.append(f"layout {layout.name!r}: paper units are pixels, assuming millimetres")
    lo, hi = layout.get_paper_limits()
    width, height = hi.x - lo.x, hi.y - lo.y
    if width <= 1e-9 or height <= 1e-9:
        width, height = float(layout.dxf.paper_width), float(layout.dxf.paper_height)
        lo, hi = ezdxf.math.Vec2(0, 0), ezdxf.math.Vec2(width, height)
        factor = 1.0
        warnings.append(f"layout {layout.name!r}: no usable paper limits, used the paper size")
    if width <= 1e-9 or height <= 1e-9:
        raise CadError(
            "NO_PAPER_SIZE",
            f"layout {layout.name!r} has no paper size",
        )
    return (lo.x, lo.y, hi.x, hi.y), factor, warnings


@contextlib.contextmanager
def _ezdxf_options(support_dirs: list[str]) -> Iterator[None]:
    """Apply SHX search directories for the duration of a render, then restore."""
    if not support_dirs:
        yield
        return
    options = ezdxf.options
    saved_dirs = list(options.support_dirs)
    saved_order = options.get("drawing-addon", "shx_resolve_order", "")
    options.support_dirs = [*support_dirs, *saved_dirs]
    options.set("drawing-addon", "shx_resolve_order", "sl")
    try:
        yield
    finally:
        options.support_dirs = saved_dirs
        options.set("drawing-addon", "shx_resolve_order", saved_order or "tsl")


def render_layout_ezdxf(doc: Drawing, layout_name: str, opts: EzdxfOptions) -> Rendered:
    """Draw one layout of ``doc`` (already prepared: xrefs embedded) into a PIL image."""
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing import config as dconfig
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise CadError(
            "MISSING_DEPENDENCY",
            f"the ezdxf render needs the Python package matplotlib ({exc})",
            hint="ask the user, then: pip install matplotlib",
        ) from exc
    image_module = _pil()

    layout = doc.layouts.get(layout_name)
    warnings = [] if layout.name.lower() == "model" else normalise_viewports(layout)
    box, mm_per_unit, box_warnings = _render_box(layout, doc)
    warnings += box_warnings
    width, height = box[2] - box[0], box[3] - box[1]
    if mm_per_unit is None:  # model space has no paper size: pick the pixel size directly
        long_px = max(opts.max_px, 4000) if opts.want_hires else opts.max_px
        dpi = 100.0
        inches_long = long_px / dpi
        scale_in = inches_long / max(width, height)
        size_in = (width * scale_in, height * scale_in)
    else:
        dpi = float(opts.dpi)
        size_in = (width * mm_per_unit / 25.4, height * mm_per_unit / 25.4)
        pixels = size_in[0] * dpi * size_in[1] * dpi
        if pixels > MAX_MASTER_PIXELS:
            dpi = dpi * (MAX_MASTER_PIXELS / pixels) ** 0.5
            warnings.append(
                f"layout {layout.name!r}: resolution lowered to {dpi:.0f} dpi (size cap)"
            )
        if opts.ctb is None and layout.get_plot_style_filename():
            warnings.append(
                f"layout {layout.name!r} names the plot style {layout.get_plot_style_filename()!r}, "
                "which is not applied (pass --ctb FILE); colours and line weights follow the layers"
            )
    figure = Figure(figsize=size_in, dpi=dpi)
    canvas = FigureCanvasAgg(figure)
    axes = figure.add_axes((0, 0, 1, 1))
    backend = MatplotlibBackend(axes, adjust_figure=False)
    ctx = RenderContext(doc, ctb=str(opts.ctb) if opts.ctb is not None else "")
    if opts.hide_layers:
        hidden = {name.lower() for name in opts.hide_layers}

        def hide(layers: Any) -> None:
            for props in layers:
                if str(props.layer).lower() in hidden:
                    props.is_visible = False

        ctx.set_layer_properties_override(hide)
    cfg = dconfig.Configuration(
        lineweight_scaling=opts.lineweight_scaling,
        background_policy=dconfig.BackgroundPolicy.WHITE,
    )
    with _ezdxf_options(opts.support_dirs), contextlib.redirect_stdout(sys.stderr):
        Frontend(ctx, backend, config=cfg).draw_layout(layout, finalize=True)
    axes.set_xlim(box[0], box[2])
    axes.set_ylim(box[1], box[3])
    axes.set_aspect("auto")
    canvas.draw()
    # frombuffer shares the canvas memory; convert() makes the only copy, then the figure goes
    shared = image_module.frombuffer(
        "RGBA", canvas.get_width_height(), canvas.buffer_rgba(), "raw", "RGBA", 0, 1
    )
    master = shared.convert("RGB")
    shared.close()
    figure.clear()
    return Rendered(master, box, dpi, warnings)


# --------------------------------------------------------------------------------------
# COM backend
# --------------------------------------------------------------------------------------


def _com_candidate() -> bool:
    """Cheap check that a COM-capable CAD might be present (no CAD is started)."""
    if sys.platform != "win32":
        return False
    try:
        from . import acad
    except ImportError:
        return False
    return bool(getattr(acad, "PROGIDS", None))


def _com_session(ctx: RunContext | None = None) -> Any:
    """Start an own CAD instance (context manager). Isolated so tests can replace it."""
    try:
        from .acad import AcadSession
    except ImportError as exc:
        raise CadError(
            "NO_BACKEND",
            "COM automation is not available on this system",
            hint="use --backend ezdxf (approximate) or run on Windows with a COM-capable CAD",
        ) from exc
    return AcadSession.start(log=ctx.log if ctx is not None else None)


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def _has_content(layout: Any) -> bool:
    """True when a paper layout holds anything besides its own overall viewport."""
    overall = {vp.handle for vp in layout_viewports(layout) if vp.overall}
    return any(e.dxftype() != "VIEWPORT" or e.dxf.handle not in overall for e in layout)


def _resolve_layouts(doc: Drawing, requested: list[str] | None, result: Result) -> list[str]:
    """Layouts to render: those asked for, else every paper layout that has content."""
    names = list(doc.layouts.names_in_taborder())
    if not requested:
        paper = [n for n in names if n.lower() != "model"]
        used = [n for n in paper if _has_content(doc.layouts.get(n))]
        for name in paper:
            if name not in used:
                result.warn(f"skipped empty layout {name!r} (name it with --layout to render it)")
        return used or ["Model"]
    by_lower = {n.lower(): n for n in names}
    out: list[str] = []
    for want in requested:
        real = by_lower.get(want.lower())
        if real is None:
            raise CadError(
                "LAYOUT_NOT_FOUND",
                f"layout {want!r} not found",
                hint="available: " + ", ".join(names),
            )
        if real not in out:
            out.append(real)
    return out


def _save_extras(
    image: Image.Image,
    box: tuple[float, float, float, float],
    stem: str,
    layout: str,
    slug: str,
    crop: list[float] | None,
    tiles: tuple[int, int] | None,
    ctx: RunContext,
    result: Result,
    source: Path,
) -> list[dict[str, Any]]:
    """Crop and tile PNGs at the master resolution; returns manifest entries."""
    entries: list[dict[str, Any]] = []
    region = (0, 0, image.width, image.height)
    if crop is not None:
        region = crop_to_pixels(box, image.size, crop)
        part = image.crop(region)
        if max(part.size) > MAX_CROP_SIDE_PX:
            part = cap_longest_side(part, MAX_CROP_SIDE_PX)
            result.warn(f"{layout}: crop is larger than {MAX_CROP_SIDE_PX}px, downscaled")
        path = ctx.path(png_name(stem, slug, "__crop"))
        save_png(part, path)
        entries.append(
            {
                "kind": "crop",
                "path": str(path),
                "size_px": list(part.size),
                "pixel_box": list(region),
            }
        )
    if tiles is not None:
        cols, rows = tiles
        for col, row, tile_box in tile_boxes(region, cols, rows):
            path = ctx.path(png_name(stem, slug, f"__tile-{col}-{row}"))
            tile = image.crop(tile_box)
            save_png(tile, path)
            entries.append(
                {
                    "kind": "tile",
                    "col": col,
                    "row": row,
                    "path": str(path),
                    "size_px": list(tile.size),
                    "pixel_box": list(tile_box),
                }
            )
    for entry in entries:
        ctx.add_output(Result("render"), Path(entry["path"]).name, Path(entry["path"]), source)
    return entries


def _run_render(args: argparse.Namespace) -> Result:
    from .runs import RunContext

    ctx = RunContext.create("render", args.run_dir)  # first: errors below keep a run directory
    crop = parse_floats(args.crop, 4, "--crop") if args.crop else None
    tiles = parse_tiles(args.tiles) if args.tiles else None
    if args.dpi < 20 or args.dpi > 1200 or args.max_px < 64:
        raise CadError("BAD_ARGS", "--dpi must be 20..1200 and --max-px at least 64")
    ctb = args.ctb.expanduser() if args.ctb else None
    if ctb is not None and not ctb.is_file():
        raise CadError(
            "BAD_ARGS", f"--ctb file not found: {ctb}", hint="give the full path of a .ctb file"
        )
    result = Result("render")
    deadline = Deadline(args.timeout)
    backend = args.backend
    consent = args.allow_com or backend == "com"
    if backend == "auto":
        backend = "com" if (args.allow_com and _com_candidate()) else "ezdxf"
        if backend == "ezdxf" and _com_candidate() and not args.allow_com:
            result.next.append(
                "a CAD plot is more faithful: ask the user, then run again with --backend com"
            )
    args.allow_com = consent  # one consent for the plot and for a DWG conversion
    if backend == "com":
        try:
            return _render_com(args, ctx, result, deadline, crop, tiles)
        except CadError as err:
            if args.backend == "com":
                raise
            result.warn(f"COM render unavailable ({err.message}); fell back to ezdxf")
    return _render_ezdxf(args, ctx, result, deadline, crop, tiles, ctb)


def _finalize_png(
    master: Image.Image,
    box: tuple[float, float, float, float],
    args: argparse.Namespace,
    layout: str,
    ctx: RunContext,
    result: Result,
    source: Path,
    manifest: list[dict[str, Any]],
    dpi: float,
    backend: str,
    crop: list[float] | None,
    tiles: tuple[int, int] | None,
    slug: str,
) -> None:
    stem = source.stem
    main = cap_longest_side(master, args.max_px)
    path = ctx.path(png_name(stem, slug))
    save_png(main, path)
    extras = _save_extras(
        master,
        box,
        stem,
        layout,
        slug,
        crop,
        tiles,
        ctx,
        result,
        source,
    )
    entry = {
        "layout": layout,
        "backend": backend,
        "path": str(path),
        "size_px": list(main.size),
        "master_px": list(master.size),
        "dpi": round(dpi, 1),
        "box": [round(v, 4) for v in box],
        "extras": extras,
    }
    manifest.append(entry)
    if len(manifest) <= MAX_LISTED_OUTPUTS:
        ctx.add_output(result, layout, path, source)
    else:
        ctx.add_output(Result("render"), layout, path, source)


def _finish(
    result: Result,
    ctx: RunContext,
    manifest: list[dict[str, Any]],
    backend: str,
    source: Path,
    args: argparse.Namespace,
) -> Result:
    manifest_path = ctx.path("render.json")
    text = json.dumps({"source": str(source), "renders": manifest}, ensure_ascii=False, indent=1)
    atomic_write(manifest_path, lambda tmp: tmp.write_text(text, encoding="utf-8"))
    ctx.add_output(result, "manifest", manifest_path)
    if len(manifest) > MAX_LISTED_OUTPUTS:
        result.warn(
            f"only the first {MAX_LISTED_OUTPUTS} PNGs are listed in outputs; see render.json"
        )
    result.summary = {
        "layouts": [
            {"name": m["layout"], "png_px": m["size_px"], "master_px": m["master_px"]}
            for m in manifest
        ][:MAX_LISTED_OUTPUTS],
        "count": len(manifest),
        "extra_images": sum(len(m["extras"]) for m in manifest),
        "dpi": args.dpi,
        "max_px": args.max_px,
    }
    result.backend = backend
    ctx.bind(result)
    ctx.finish()
    return result


def _render_ezdxf(
    args: argparse.Namespace,
    ctx: RunContext,
    result: Result,
    deadline: Deadline,
    crop: list[float] | None,
    tiles: tuple[int, int] | None,
    ctb: Path | None,
) -> Result:
    result.approximate = True
    result.warn(
        "approximate render (ezdxf): fonts are substitutes and plot styles are not applied unless "
        "--ctb is given; use a CAD plot for anything that must match"
    )
    opts = EzdxfOptions(
        dpi=args.dpi,
        max_px=args.max_px,
        ctb=ctb,
        support_dirs=[str(d) for d in args.support_dir or []],
        lineweight_scaling=args.lineweight_scaling,
        hide_layers=set(args.hide_layer or []),
        want_hires=crop is not None or tiles is not None,
    )
    manifest: list[dict[str, Any]] = []
    loaded = open_drawing(args.file, ctx, **_conversion(args))
    for warning in loaded.warnings:
        result.warn(warning)
    if loaded.approximate:
        result.warn("approximate DWG conversion: the render may lack some objects")
    source = loaded.source
    layouts = _resolve_layouts(loaded.doc, args.layout, result)
    slugs = assign_slugs(layouts)
    for warning in embed_xrefs(loaded.doc, source.parent):
        result.warn(warning)
    for i, name in enumerate(layouts):
        deadline.check()
        ctx.progress(i, len(layouts), f"rendering {name}")
        ctx.log(f"render {name} (ezdxf)")
        rendered = render_layout_ezdxf(loaded.doc, name, opts)
        for warning in rendered.warnings:
            result.warn(warning)
        _finalize_png(
            rendered.image, rendered.box, args, name, ctx, result, source, manifest,
            rendered.dpi, "ezdxf", crop, tiles, slugs[name],
        )  # fmt: skip
        rendered.image.close()
    ctx.progress(len(layouts), len(layouts), "done")
    return _finish(result, ctx, manifest, "ezdxf", source, args)


def _conversion(args: argparse.Namespace) -> dict[str, Any]:
    """DWG conversion options: ``--allow-com`` is the shared consent, ``--convert-with`` the backend."""
    options = conversion_options(args)
    chosen = getattr(args, "convert_with", "auto")
    options["prefer"] = None if chosen in (None, "auto") else chosen
    return options


def _render_com(
    args: argparse.Namespace,
    ctx: RunContext,
    result: Result,
    deadline: Deadline,
    crop: list[float] | None,
    tiles: tuple[int, int] | None,
) -> Result:
    from .runs import stage_copy

    source = _resolve_source(args.file)
    names: list[str] = args.layout or []
    layout_box: dict[str, tuple[float, float, float, float]] = {}
    if not names or crop is not None or tiles is not None:
        loaded = open_drawing(source, ctx, **_conversion(args))  # layout names and paper limits
        for warning in loaded.warnings:
            result.warn(warning)
        names = _resolve_layouts(loaded.doc, args.layout, result)
        for name in names:
            if name.lower() != "model":
                lo, hi = loaded.doc.layouts.get(name).get_paper_limits()
                layout_box[name] = (lo.x, lo.y, hi.x, hi.y)
    slugs = assign_slugs(names)
    manifest: list[dict[str, Any]] = []
    staged = stage_copy(source, ctx)  # CAD works on a copy: the original is never opened
    with _com_session(ctx) as session:
        for i, name in enumerate(names):
            deadline.check()
            ctx.progress(i, len(names), f"plotting {name}")
            pdf = ctx.path(f"{source.stem}__{slugs[name]}.pdf")
            for warning in session.plot_layout_pdf(staged, name, pdf) or []:
                result.warn(warning)  # includes the PDF-viewer warning
            ctx.add_output(Result("render"), pdf.name, pdf, source)
            image = pdf_to_image(pdf, args.dpi, args.raster)
            box = layout_box.get(name, (0.0, 0.0, float(image.width), float(image.height)))
            if (crop is not None or tiles is not None) and name not in layout_box:
                result.warn(f"{name}: crop/tiles need paper limits; mapped to image pixels instead")
            elif crop is not None:
                result.warn(f"{name}: crop mapped assuming the plot area equals the paper limits")
            _finalize_png(
                image, box, args, name, ctx, result, source, manifest,
                float(args.dpi), "com", crop, tiles, slugs[name],
            )  # fmt: skip
            image.close()
        for warning in getattr(session, "warnings", []) or []:
            result.warn(warning)
    return _finish(result, ctx, manifest, "com", source, args)


def _add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path, help="DXF or DWG file")
    p.add_argument(
        "--layout", action="append", help="layout name, repeatable (default: all paper layouts)"
    )
    p.add_argument("--backend", choices=("auto", "com", "ezdxf"), default="auto")
    p.add_argument(
        "--allow-com",
        action="store_true",
        help="the user agreed to a CAD application: lets auto use a CAD plot and a DWG be "
        "converted by it",
    )
    p.add_argument(
        "--convert-with",
        choices=("auto", "com", "oda", "libredwg"),
        default="auto",
        help="converter for a DWG input (auto = oda, libredwg; com needs consent)",
    )
    p.add_argument(
        "--dpi", type=int, default=DEFAULT_DPI, help=f"master resolution (default {DEFAULT_DPI})"
    )
    p.add_argument(
        "--max-px", type=int, default=DEFAULT_MAX_PX, help="cap on the longest side of the main PNG"
    )
    p.add_argument(
        "--crop", help="X1,Y1,X2,Y2 in layout coordinates (y up); extra PNG at master resolution"
    )
    p.add_argument("--tiles", help="COLSxROWS grid over the crop (or the whole layout); extra PNGs")
    p.add_argument(
        "--raster", choices=("pdfium", "pymupdf"), default="pdfium", help="PDF to PNG (COM path)"
    )
    p.add_argument(
        "--ctb", type=Path, default=None, help="plot style table (.ctb) to apply (ezdxf path)"
    )
    p.add_argument(
        "--support-dir", action="append", type=Path, help="folder with SHX fonts, repeatable"
    )
    p.add_argument("--lineweight-scaling", type=float, default=DEFAULT_LINEWEIGHT_SCALING)
    p.add_argument(
        "--hide-layer", action="append", help="layer to leave out everywhere, repeatable"
    )
    p.add_argument("--run-dir", type=Path, default=None, help="base directory for run folders")
    p.add_argument(
        "--timeout", type=float, default=100.0, help="soft time limit in seconds (default 100)"
    )


COMMANDS = {
    "render": Command(
        help="PNG per layout: a CAD plot when available and agreed, otherwise an ezdxf approximation",
        add_arguments=_add_render_args,
        run=_run_render,
        epilog=(
            "examples:\n"
            "  cad.py render plan.dxf --layout Sheet-A --max-px 1600\n"
            "  cad.py render plan.dwg --backend com --crop 0,0,200,150 --tiles 2x2\n"
            "exit codes: 0 ok, 2 bad arguments, 3 missing backend or package, 6 empty layout"
        ),
    ),
}
