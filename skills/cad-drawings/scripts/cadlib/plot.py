"""Deliverable PDFs: one single-page PDF per layout, always plotted by the CAD application.

An ezdxf drawing is never a deliverable, so there is no fallback: without ``--allow-com`` the
command refuses. The CAD application works on a staged copy in a fresh run directory, with a
fresh document per layout, and the instance it started is quit at the end. Flags map onto the
``page_setup`` keys of ``acad.AcadSession.plot_layout_pdf``; without flags each layout keeps its
own page setup. A layout that fails does not hide the ones that worked: the result is partial
(exit 7) and lists both.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .command import Command
from .drawing import _resolve_source, open_drawing
from .result import CadError, ExitCode, Result
from .util import atomic_write, parse_floats

if TYPE_CHECKING:
    from .runs import RunContext

DEFAULT_TIMEOUT_S = 100.0
AREAS = ("layout", "extents", "display", "window")
ROTATIONS = (0, 90, 180, 270)
MAX_LISTED_OUTPUTS = 12
_NUMBER = r"(\d+(?:\.\d+)?|\.\d+)"
_SCALE = re.compile(rf"^{_NUMBER}:{_NUMBER}$")
# A failure with one of these codes would hit every following layout too: stop plotting.
_ABORT_CODES = frozenset({"TIMEOUT", "LOCKED", "NO_BACKEND", "NO_INSTANCE", "PID_UNAVAILABLE"})

_clock: Callable[[], float] = time.monotonic  # replaced in unit tests


# --------------------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------------------


def parse_scale(text: str) -> str:
    """``fit`` or ``paper:drawing`` (``1:50``, ``2:1``) with both numbers positive."""
    cleaned = text.strip().lower()
    if cleaned == "fit":
        return "fit"
    match = _SCALE.match(cleaned)
    if not match or float(match.group(1)) <= 0 or float(match.group(2)) <= 0:
        raise CadError(
            "BAD_ARGS",
            f"--scale must be fit, 1:N or N:1 with positive numbers, got {text!r}",
            hint="examples: fit, 1:50, 2:1",
        )
    return f"{match.group(1)}:{match.group(2)}"


def parse_window(text: str) -> tuple[float, float, float, float]:
    """``X1,Y1,X2,Y2`` with the corners put in order (min first)."""
    x1, y1, x2, y2 = parse_floats(text, 4, "--window")
    if x1 == x2 or y1 == y2:
        raise CadError("BAD_ARGS", f"--window must enclose an area, got {text!r}")
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def build_page_setup(args: argparse.Namespace) -> dict[str, Any]:
    """The ``page_setup`` dict for ``plot_layout_pdf``; empty = each layout's own setup."""
    setup: dict[str, Any] = {}
    if args.device:
        setup["device"] = args.device
    if args.media:
        setup["media"] = args.media
    area = args.area.strip().lower() if args.area else None
    if area is not None and area not in AREAS:
        raise CadError("BAD_ARGS", f"--area must be one of {', '.join(AREAS)}, got {args.area!r}")
    if args.window:
        if area not in (None, "window"):
            raise CadError("BAD_ARGS", f"--window only works with --area window, not {area}")
        area = "window"
        setup["window"] = parse_window(args.window)
    elif area == "window":
        raise CadError("BAD_ARGS", "--area window needs --window X1,Y1,X2,Y2")
    if area:
        setup["plot_area"] = area
    if args.scale:
        setup["scale"] = parse_scale(args.scale)
    if args.rotate is not None:
        if args.rotate not in ROTATIONS:
            raise CadError(
                "BAD_ARGS", f"--rotate must be one of 0, 90, 180, 270, got {args.rotate}"
            )
        setup["rotation"] = args.rotate
    if args.style_sheet:
        setup["style_sheet"] = args.style_sheet
    return setup


# --------------------------------------------------------------------------------------
# CAD session and verification
# --------------------------------------------------------------------------------------


def _start_session(ctx: RunContext) -> Any:
    """Start an own CAD instance (context manager)."""
    try:
        from .acad import AcadSession
    except ImportError as exc:
        raise CadError(
            "NO_BACKEND",
            "COM automation is not available on this system",
            hint="plotting a deliverable needs Windows and a COM-capable CAD application",
        ) from exc
    return AcadSession.start(log=ctx.log)


_session_factory: Callable[[RunContext], Any] = _start_session  # replaced in unit tests


def verify_pdf(path: Path) -> tuple[float, float] | None:
    """Raise ``PLOT_BAD_OUTPUT`` unless ``path`` is a non-empty single-page PDF.

    Returns the first page size in mm, or None when it cannot be measured (pypdfium2 missing).
    The comparison with the media size is done by the CAD layer, which knows the media.
    """
    if not path.is_file() or path.stat().st_size == 0:
        raise CadError("PLOT_BAD_OUTPUT", f"{path.name} was not written or is empty")
    with path.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise CadError("PLOT_BAD_OUTPUT", f"{path.name} is not a PDF (missing header)")
    from .acad import _pdf_info

    try:
        info = _pdf_info(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CadError("PLOT_BAD_OUTPUT", f"{path.name} cannot be read as a PDF: {exc}") from exc
    if info is None:
        return None
    pages, size = info
    if pages != 1:
        raise CadError("PLOT_BAD_OUTPUT", f"{path.name} has {pages} pages, expected 1")
    if min(size) <= 0:
        raise CadError("PLOT_BAD_OUTPUT", f"{path.name} has an empty page")
    return (round(size[0], 1), round(size[1], 1))


# --------------------------------------------------------------------------------------
# layouts and destination
# --------------------------------------------------------------------------------------


def _choose_layouts(
    args: argparse.Namespace, source: Path, ctx: RunContext, result: Result
) -> list[str]:
    """Layouts to plot: those named, else every paper layout with content."""
    from .render import _resolve_layouts

    if source.suffix.lower() == ".dwg" and args.layout:
        # names are checked by the CAD application: no conversion just to read them
        seen: dict[str, str] = {}
        for name in args.layout:
            seen.setdefault(name.lower(), name)
        return list(seen.values())
    loaded = open_drawing(source, ctx, allow_com=True)
    for warning in loaded.warnings:
        result.warn(warning)
    if loaded.approximate:
        result.warn("approximate DWG conversion: the layout list may be incomplete")
    names = _resolve_layouts(loaded.doc, args.layout, result)
    if not args.layout and names == ["Model"]:
        raise CadError(
            "EMPTY_LAYOUT",
            "no paper layout with content to plot",
            hint="name a layout with --layout, or create a layout with a viewport first",
        )
    return names


def _check_dest(dest: Path, names: list[str], overwrite: bool) -> None:
    """Refuse a destination that is a file or, without ``--overwrite``, holds a target."""
    if dest.exists() and not dest.is_dir():
        raise CadError("BAD_ARGS", f"--dest is not a directory: {dest}")
    if overwrite:
        return
    taken = [name for name in names if (dest / name).exists()]
    if taken:
        raise CadError(
            "EXISTS",
            f"{len(taken)} target(s) already exist in {dest}: {', '.join(taken[:5])}",
            hint="nothing was changed; use --overwrite or another --dest",
        )


def _copier(src: Path) -> Callable[[Path], None]:
    def copy(tmp: Path) -> None:
        shutil.copyfile(src, tmp)

    return copy


def _copy_to_dest(dest: Path, files: list[Path], overwrite: bool) -> list[str]:
    """Copy PDFs into ``dest`` (temp file + replace); returns the written names."""
    names = [f.name for f in files]
    _check_dest(dest, names, overwrite)  # again: the folder may have changed while CAD ran
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for src in files:
        try:
            atomic_write(dest / src.name, _copier(src))
        except OSError as exc:
            raise CadError(
                "UNEXPECTED",
                f"copy to {dest} failed after {len(written)} file(s): {exc}",
                hint="the finished PDFs are still in the run directory",
            ) from exc
        written.append(src.name)
    return written


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def _run_plot(args: argparse.Namespace) -> Result:
    from .acad import VIEWER_WARNING
    from .render import assign_slugs
    from .runs import RunContext, stage_copy_report

    ctx = RunContext.create("plot", args.run_dir)  # first: errors below keep a run directory
    setup = build_page_setup(args)
    if not args.allow_com:
        raise CadError(
            "NO_BACKEND",
            "a deliverable PDF is plotted by the CAD application, which needs the user's consent",
            hint="ask the user, then rerun with --allow-com",
        )
    source = _resolve_source(args.file)
    result = Result("plot", backend="com")
    names = _choose_layouts(args, source, ctx, result)
    slugs = assign_slugs(names)
    pdf_names = {name: f"{source.stem}__{slugs[name]}.pdf" for name in names}
    dest = args.dest.expanduser() if args.dest else None
    if dest is not None:
        _check_dest(dest, list(pdf_names.values()), args.overwrite)  # before CAD is started
    staged = stage_copy_report(source, ctx)
    for skipped in staged.skipped:
        result.warn(skipped)
    end = _clock() + args.timeout if args.timeout else None

    plots: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    first_error: CadError | None = None
    stopped: CadError | None = None
    not_plotted: list[str] = []
    with _session_factory(ctx) as session:
        for i, name in enumerate(names):
            if end is not None and _clock() > end:
                stopped = CadError(
                    "TIMEOUT",
                    f"time limit of {args.timeout:g} s reached before plotting {name!r}",
                    hint="raise --timeout; the PDFs finished so far are kept",
                )
                not_plotted = names[i:]
                break
            ctx.progress(i, len(names), f"plotting {name}")
            ctx.log(f"plot {name} (CAD)")
            pdf = ctx.path(pdf_names[name])
            try:
                warnings = session.plot_layout_pdf(staged.path, name, pdf, page_setup=setup or None)
                page_mm = verify_pdf(pdf)
                ctx.add_output(result, name, pdf, source)
            except CadError as err:
                result.outputs.pop(name, None)
                with contextlib.suppress(OSError):
                    pdf.unlink()  # a rejected or half-written PDF is not left as an artifact
                ctx.log(f"plot {name} failed: {err.code}: {err.message}")
                failed.append({"layout": name, "code": err.code, "message": err.message})
                result.add_error(err)
                first_error = first_error or err
                if err.code in _ABORT_CODES:
                    stopped = err
                    not_plotted = names[i + 1 :]
                    break
                continue
            for warning in warnings or []:
                result.warn(warning)
            plots.append(
                {
                    "layout": name,
                    "pdf": str(pdf),
                    "device": setup.get("device"),
                    "media": setup.get("media"),
                    "area": setup.get("plot_area"),
                    "window": setup.get("window"),
                    "scale": setup.get("scale"),
                    "rotation": setup.get("rotation"),
                    "page_mm": list(page_mm) if page_mm else None,
                    "warnings": list(warnings or []),
                }
            )
        for warning in getattr(session, "warnings", []) or []:
            result.warn(warning)
    result.warn(VIEWER_WARNING)  # also when nothing was plotted: CAD may have opened a viewer
    ctx.progress(len(plots), len(names), "done")

    if not plots:
        raise first_error or stopped or CadError("PLOT_FAILED", "nothing was plotted")
    copied: list[str] = []
    if dest is not None:
        copied = _copy_to_dest(dest, [Path(p["pdf"]) for p in plots], args.overwrite)
    return _finish(result, ctx, source, plots, failed, not_plotted, stopped, dest, copied)


def _finish(
    result: Result,
    ctx: RunContext,
    source: Path,
    plots: list[dict[str, Any]],
    failed: list[dict[str, str]],
    not_plotted: list[str],
    stopped: CadError | None,
    dest: Path | None,
    copied: list[str],
) -> Result:
    report = {
        "source": str(source),
        "plots": plots,
        "failed": failed,
        "not_plotted": not_plotted,
        "dest": {"dir": str(dest), "files": copied} if dest is not None else None,
    }
    path = ctx.path("plot.json")
    text = json.dumps(report, ensure_ascii=False, indent=1)

    def write(tmp: Path) -> None:
        tmp.write_text(text, encoding="utf-8")

    atomic_write(path, write)
    ctx.add_output(result, "plot.json", path)
    if len(result.outputs) > MAX_LISTED_OUTPUTS:
        result.warn(f"only {MAX_LISTED_OUTPUTS} outputs are listed; see plot.json")
    result.summary = {
        "layouts": [
            {"name": p["layout"], "page_mm": p["page_mm"]} for p in plots[:MAX_LISTED_OUTPUTS]
        ],
        "count": len(plots),
        "failed": failed[:MAX_LISTED_OUTPUTS],
        "not_plotted": not_plotted[:MAX_LISTED_OUTPUTS],
    }
    if dest is not None:
        result.summary["dest"] = {"dir": str(dest), "files": copied[:MAX_LISTED_OUTPUTS]}
    if stopped is not None and not any(e["code"] == stopped.code for e in result.errors):
        result.add_error(stopped)
    if failed or not_plotted or stopped is not None:
        result.exit_code = ExitCode.PARTIAL
        result.next.append("the PDFs that succeeded are listed in outputs; see plot.json")
    ctx.bind(result)
    ctx.finish()
    return result


def _add_plot_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path, help="DXF or DWG file")
    p.add_argument(
        "--layout",
        action="append",
        help="layout name, repeatable (default: paper layouts with content)",
    )
    p.add_argument(
        "--allow-com",
        action="store_true",
        help="the user agreed to a CAD application: required, a deliverable PDF comes from CAD",
    )
    p.add_argument(
        "--device", help="plot device, e.g. the built-in PDF plotter (default: layout's)"
    )
    p.add_argument("--media", help="canonical media name offered by the device")
    p.add_argument("--area", help="plot area: layout, extents, display or window")
    p.add_argument("--window", help="X1,Y1,X2,Y2 of the plot window (implies --area window)")
    p.add_argument("--scale", help="fit, 1:N or N:1 (default: the layout's own)")
    p.add_argument("--rotate", type=int, help="page rotation: 0, 90, 180 or 270")
    p.add_argument("--style-sheet", help="plot style table name (.ctb or .stb known to the CAD)")
    p.add_argument("--dest", type=Path, help="also copy the finished PDFs into this folder")
    p.add_argument(
        "--overwrite", action="store_true", help="replace files that already exist in --dest"
    )
    p.add_argument("--run-dir", type=Path, default=None, help="base directory for run folders")
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"soft time limit in seconds, checked between layouts (default {DEFAULT_TIMEOUT_S:g})",
    )


COMMANDS = {
    "plot": Command(
        help="deliverable PDF per layout, plotted by the CAD application (needs --allow-com)",
        add_arguments=_add_plot_args,
        run=_run_plot,
        epilog=(
            "examples:\n"
            "  cad.py plot plan.dwg --allow-com --layout Sheet-A --scale fit --dest out\n"
            "  cad.py plot plan.dwg --allow-com --media 'ISO_A3_(420.00_x_297.00_MM)'\n"
            "exit codes: 0 ok, 2 bad arguments, 3 no consent or CAD, 6 target exists or "
            "nothing to plot, 7 some layouts failed"
        ),
    ),
}
