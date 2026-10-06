"""qa: findings with a severity on a drawing and on a plotted PDF.

Mechanical checks only (what a script can decide). Whether a layout looks right stays with the
agent (see references/visual-qa.md). Errors end the run with exit 7; warnings and info do not.
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import sheet_frame
from .command import Command
from .drawing_info import build_info
from .dxf import _open
from .result import CadError, ExitCode, Result
from .util import Deadline, _add_common, _finish, _new_run, _write_json

SEVERITIES = ("error", "warning", "info")
SUMMARY_FINDINGS = 5
MAX_FINDINGS_PER_ID = 50  # more findings of one id are counted, not listed
BASELINE_MIN_MATCH = 0.5  # share of texts that must exist in the baseline for a comparison


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    where: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# drawing checks
# --------------------------------------------------------------------------------------


def check_drawing(doc: Any, info: dict[str, Any]) -> list[Finding]:
    """Checks that need only the opened drawing and its ``info``."""
    found: list[Finding] = []
    if int(info["units"].get("insunits", 0)) == 0:
        found.append(
            Finding(
                "UNITS_UNSET",
                "warning",
                "header",
                "$INSUNITS is 0 (unitless): lengths and areas cannot be converted safely",
            )
        )
    for layout in info["layouts"]:
        name = layout["name"]
        if name.lower() == "model":
            continue
        content = sum(1 for e in doc.layouts.get(name) if e.dxftype() != "VIEWPORT")
        if content == 0:
            found.append(
                Finding(
                    "LAYOUT_EMPTY",
                    "warning",
                    name,
                    "the layout holds nothing but viewports (no frame, text or linework)",
                )
            )
        for vp in layout.get("viewports", []):
            if vp["overall"]:
                continue
            scale = vp.get("scale")
            if scale is None or not math.isfinite(scale) or scale <= 0:
                found.append(
                    Finding(
                        "VIEWPORT_SCALE",
                        "warning",
                        f"{name}:{vp['handle']}",
                        f"viewport scale is not a positive number ({scale})",
                    )
                )
            if vp["status"] <= 0:
                found.append(
                    Finding(
                        "VIEWPORT_STATUS_UNKNOWN",
                        "info",
                        f"{name}:{vp['handle']}",
                        "the file does not say whether this viewport prints "
                        "(prints_on is geometry-based)",
                    )
                )
    for xref in info["xrefs"]:
        if xref["checked"] and not xref["resolved_on_disk"]:
            found.append(
                Finding(
                    "XREF_MISSING",
                    "warning",
                    f"xref {xref['block']}",
                    f"external reference not found on disk: {xref['path']}",
                )
            )
    return found


@dataclass
class FrameReport:
    findings: list[Finding] = field(default_factory=list)
    frames: dict[str, list[float] | None] = field(default_factory=dict)  # layout -> frame box
    found: dict[str, tuple[sheet_frame.PaperSetup, sheet_frame.Frame]] = field(default_factory=dict)


def _sides(over: dict[str, float]) -> str:
    return ", ".join(f"{side} {value:.1f} mm" for side, value in over.items())


def check_frames(
    doc: Any, info: dict[str, Any], *, frame_layer: str | None, deadline: Deadline
) -> FrameReport:
    """Frame, printable area and text against the frame, for every layout that holds content."""
    report = FrameReport()
    unprintable = sheet_frame.unprintable_layers(doc.layers)
    for entry in info["layouts"]:
        name = entry["name"]
        if name.lower() == "model":
            continue
        layout = doc.layouts.get(name)
        if not any(e.dxftype() != "VIEWPORT" for e in layout):
            continue
        deadline.check()
        setup = sheet_frame.paper_setup(layout)
        if setup is None:
            report.frames[name] = None
            report.findings.append(
                Finding("FRAME_CHECK_SKIPPED", "info", name, "the layout has no paper size")
            )
            continue
        budget = sheet_frame.ExpansionBudget()
        frame = sheet_frame.find_frame(
            layout,
            setup,
            frame_layer=frame_layer,
            unprintable=unprintable,
            deadline=deadline,
            budget=budget,
        )
        if budget.exhausted:
            report.findings.append(
                Finding(
                    "FRAME_CHECK_SKIPPED",
                    "info",
                    name,
                    f"block expansion limit reached ({sheet_frame.MAX_EXPANDED_ENTITIES} "
                    "entities): the frame search is incomplete (a block that nests itself "
                    "many times?)",
                )
            )
        if frame is None and budget.exhausted:
            report.frames[name] = None
            continue
        if frame is None:
            report.frames[name] = None
            where_layer = f" on layer {frame_layer!r}" if frame_layer else ""
            coverage = (
                ""
                if frame_layer
                else (f" covering {sheet_frame.FRAME_MIN_COVERAGE:.0%} of the paper")
            )
            report.findings.append(
                Finding(
                    "FRAME_NOT_FOUND",
                    "info",
                    name,
                    f"no closed rectangle{coverage}{where_layer} (polyline or four lines): "
                    "frame checks skipped; --frame-layer names the layer of the frame",
                )
            )
            continue
        report.frames[name] = [round(v, 4) for v in frame.box]
        report.found[name] = (setup, frame)
        where = f"{name}:{frame.handle}"
        reason = setup.outside_skip_reason()
        if reason:
            report.findings.append(
                Finding(
                    "FRAME_CHECK_SKIPPED",
                    "info",
                    name,
                    f"frame not compared with the printable area: {reason}",
                )
            )
        elif setup.unit_mm:
            over = sheet_frame.overshoot_mm(frame.box, setup.printable_box, setup.unit_mm)
            if over:
                beyond = sheet_frame.overshoot_mm(frame.box, setup.paper_box, setup.unit_mm)
                tail = (
                    f"it also extends beyond the paper ({_sides(beyond)})"
                    if beyond
                    else "it stays within the paper"
                )
                report.findings.append(
                    Finding(
                        "FRAME_OUTSIDE_PAPER",
                        "warning",
                        where,
                        f"the frame leaves the printable area: {_sides(over)}; {tail}. "
                        "A plot clips what is outside the printable area: check the page "
                        "setup margins",
                    )
                )
        for item in sheet_frame.collect_texts(doc, unprintable, deadline, spaces={name}):
            if sheet_frame.outside_frame(frame.box, item.box):
                report.findings.append(
                    Finding(
                        "TEXT_OUTSIDE_FRAME",
                        "warning",
                        f"{name}:{item.handle}",
                        f"{item.kind} crosses the sheet frame and sticks out by more than "
                        f"{sheet_frame.TEXT_TOLERANCE:.0%} of its size (estimate from font "
                        "metrics: check it on the render)",
                    )
                )
    return report


def check_baseline(doc: Any, baseline: Any, *, deadline: Deadline) -> list[Finding]:
    """Texts (same handle) that grew or wrap to more lines than in the earlier drawing."""
    current = sheet_frame.collect_texts(doc, sheet_frame.unprintable_layers(doc.layers), deadline)
    earlier = sheet_frame.collect_texts(
        baseline, sheet_frame.unprintable_layers(baseline.layers), deadline
    )
    before = {(t.handle, t.kind): t for t in earlier}
    pairs = [(t, before[(t.handle, t.kind)]) for t in current if (t.handle, t.kind) in before]
    if current and len(pairs) / len(current) < BASELINE_MIN_MATCH:
        return [
            Finding(
                "BASELINE_MISMATCH",
                "info",
                "baseline",
                f"only {len(pairs)} of {len(current)} texts share a handle with the baseline: it "
                "is not an earlier version of this drawing (or it was re-saved with new handles); "
                "texts were not compared",
            )
        ]
    found: list[Finding] = []
    for new, old in pairs:
        where = f"{new.space}:{new.handle}"
        if new.lines is not None and old.lines is not None and new.lines > old.lines:
            found.append(
                Finding(
                    "TEXT_WRAPPED",
                    "warning",
                    where,
                    f"{new.kind} wraps to {new.lines} lines, it was {old.lines} (estimate from "
                    "font metrics): check that it still fits its place",
                )
            )
        elif sheet_frame.grew(old.box, new.box):
            found.append(
                Finding(
                    "TEXT_GREW",
                    "warning",
                    where,
                    f"{new.kind} box grew from {old.box[2] - old.box[0]:.1f} x "
                    f"{old.box[3] - old.box[1]:.1f} to {new.box[2] - new.box[0]:.1f} x "
                    f"{new.box[3] - new.box[1]:.1f} drawing units (estimate from font metrics): "
                    "check that it still fits its place",
                )
            )
    return found


def cap_findings(found: list[Finding]) -> tuple[list[Finding], dict[str, int]]:
    """At most ``MAX_FINDINGS_PER_ID`` per id; returns what was dropped, per id."""
    seen: Counter[str] = Counter()
    kept: list[Finding] = []
    dropped: Counter[str] = Counter()
    for f in found:
        seen[f.id] += 1
        if seen[f.id] <= MAX_FINDINGS_PER_ID:
            kept.append(f)
        else:
            dropped[f.id] += 1
    return kept, dict(dropped)


# --------------------------------------------------------------------------------------
# PDF checks
# --------------------------------------------------------------------------------------


def _pdf_text(path: Path) -> str | None:
    """Text layer of every page, lower-cased with collapsed whitespace; None if unavailable."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
    except Exception:  # noqa: BLE001
        return None
    try:
        parts = [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    except Exception:  # noqa: BLE001
        return None
    finally:
        pdf.close()
    return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()


def _close(a: float, b: float) -> bool:
    """Equal within 2 % + 1 mm (the tolerance of the page size check)."""
    return abs(a - b) <= 0.02 * max(a, b) + 1.0


def _r_half(value: float) -> float:
    """Round to half a millimetre: the raster cannot tell more, and it keeps messages stable."""
    return round(value * 2) / 2


def _edge_findings(
    path: Path,
    *,
    size: tuple[float, float],
    expected_mm: tuple[float, float] | None,
    expected_frame_mm: tuple[float, float, float, float] | None,
    frame_skipped: str | None,
    size_error: bool,
    deadline: Deadline | None = None,
) -> list[Finding]:
    """Content cut off at the page edges, and a frame that is not where the layout puts it."""
    name = path.name
    try:
        pages, total = sheet_frame.pdf_pages_ink(path, deadline=deadline)
    except ImportError:
        return [
            Finding(
                "PDF_UNCHECKED",
                "info",
                name,
                "Pillow is not installed: content at the page edges and the frame position "
                "were not checked",
            )
        ]
    except CadError:
        raise  # the time limit
    except Exception as exc:  # noqa: BLE001
        return [Finding("PDF_UNCHECKED", "info", name, f"could not rasterise the PDF: {exc}")]
    found: list[Finding] = []
    if total > len(pages):
        found.append(
            Finding(
                "PDF_UNCHECKED",
                "info",
                name,
                f"only the first {len(pages)} of {total} pages were checked at the edges",
            )
        )
    for ink in pages:
        where = name if total == 1 else f"{name} p.{ink.page}"
        if ink.trimmed:
            found.append(
                Finding(
                    "PDF_TRIM_OUTLINE",
                    "info",
                    where,
                    f"a line along the full {', '.join(ink.trimmed)} page edge is taken for the "
                    "outline of the sheet format and ignored",
                )
            )
        touching = {
            s: length
            for s, length in ink.edge_ink_mm.items()
            if ink.gaps_mm.get(s, 0.0) < sheet_frame.PDF_EDGE_TOL_MM
        }
        cut = {
            s: n
            for s, n in touching.items()
            if n >= sheet_frame.PDF_CLIPPED_MIN_MM
            or ink.edge_depth_mm.get(s, 0.0) >= sheet_frame.PDF_CLIPPED_STROKE_MM
        }
        marks = {s: n for s, n in touching.items() if s not in cut}
        if cut:
            sides = ", ".join(f"{s} {g:.2f} mm" for s, g in ink.gaps_mm.items() if s in cut)
            lengths = ", ".join(
                f"{n:.0f} mm along and {ink.edge_depth_mm.get(s, 0.0):.0f} mm deep at the {s} edge"
                for s, n in cut.items()
            )
            found.append(
                Finding(
                    "PDF_CLIPPED",
                    "error",
                    where,
                    f"content reaches the page edge ({sides}; {lengths}): the sheet is "
                    "probably cut off (a shifted plot or a wrong paper size)",
                )
            )
        if marks:
            lengths = ", ".join(f"{n:.1f} mm on the {s} edge" for s, n in marks.items())
            found.append(
                Finding(
                    "PDF_EDGE_MARKS",
                    "info",
                    where,
                    f"marks touch the page edge ({lengths}): the sheet itself is not cut "
                    f"(under {sheet_frame.PDF_CLIPPED_MIN_MM:g} mm of ink along an edge)",
                )
            )
    if total != 1 or size_error or not pages:
        return found
    where = name
    if expected_frame_mm is None:
        if frame_skipped:
            found.append(
                Finding(
                    "FRAME_CHECK_SKIPPED",
                    "info",
                    where,
                    f"frame position not compared with the layout: {frame_skipped}",
                )
            )
        return found
    if expected_mm is not None and not (
        _close(size[0], expected_mm[0]) and _close(size[1], expected_mm[1])
    ):
        found.append(
            Finding(
                "FRAME_CHECK_SKIPPED",
                "info",
                where,
                "frame position not compared with the layout: the page is turned against the "
                "paper of the layout",
            )
        )
        return found
    seen = sheet_frame.frame_on_page(pages[0], expected_frame_mm)
    if seen is None:
        found.append(
            Finding(
                "FRAME_CHECK_SKIPPED",
                "info",
                where,
                "frame position not compared with the layout: the frame is not visible on the "
                "page (not plotted, or hidden at the page edge)",
            )
        )
        return found
    deltas = [s - e for s, e in zip(seen, expected_frame_mm, strict=True)]
    if max(abs(d) for d in deltas) > sheet_frame.PDF_SHIFT_TOL_MM:
        dx = _r_half((deltas[0] + deltas[2]) / 2)
        dy = _r_half((deltas[1] + deltas[3]) / 2)
        found.append(
            Finding(
                "PDF_SHIFTED",
                "warning",
                where,
                f"the frame is off its place in the layout by dx {dx:+.1f} mm, dy {dy:+.1f} mm "
                f"(sides {', '.join(f'{_r_half(d):+.1f}' for d in deltas)} mm: left, bottom, "
                "right, top): the plot probably inherited another page setup or plot offset",
            )
        )
    return found


def check_pdf(
    path: Path,
    *,
    expected_mm: tuple[float, float] | None = None,
    expect_pages: int = 1,
    require: tuple[str, ...] = (),
    forbid: tuple[str, ...] = (),
    expected_frame_mm: tuple[float, float, float, float] | None = None,
    frame_skipped: str | None = None,
    deadline: Deadline | None = None,
) -> list[Finding]:
    """Checks on a plotted PDF: readable, page count and size, content, required words.

    Also content at the page edges. ``expected_frame_mm`` is where the layout puts its frame on
    the page (mm from the lower-left corner); ``frame_skipped`` says why that is not known.
    """
    from .acad import MIN_PDF_OBJECTS, _pdf_info, _pdf_object_count, sizes_match

    where = path.name
    try:
        if not path.is_file() or path.read_bytes()[:5] != b"%PDF-":
            raise ValueError("missing %PDF header")
        info = _pdf_info(path)
    except (OSError, RuntimeError, ValueError) as exc:
        return [Finding("PDF_UNREADABLE", "error", where, f"cannot be read as a PDF: {exc}")]
    if info is None:
        return [
            Finding("PDF_UNCHECKED", "info", where, "pypdfium2 is not installed: PDF not checked")
        ]
    pages, size = info
    found: list[Finding] = []
    if pages != expect_pages:
        found.append(
            Finding("PDF_PAGES", "error", where, f"{pages} page(s), expected {expect_pages}")
        )
    if pages == 0:
        return found
    objects = _pdf_object_count(path)
    if objects is not None and objects < MIN_PDF_OBJECTS:
        found.append(
            Finding(
                "PDF_EMPTY",
                "error",
                where,
                f"first page has {objects} drawing object(s): almost no content "
                "(an empty layout or a reset plot setup)",
            )
        )
    if expected_mm is not None and not sizes_match(size, expected_mm):
        found.append(
            Finding(
                "PDF_SIZE",
                "error",
                where,
                f"page is {size[0]:.0f} x {size[1]:.0f} mm, expected "
                f"{expected_mm[0]:.0f} x {expected_mm[1]:.0f} mm",
            )
        )
    found += _edge_findings(
        path,
        size=size,
        expected_mm=expected_mm,
        expected_frame_mm=expected_frame_mm,
        frame_skipped=frame_skipped,
        size_error=any(f.id == "PDF_SIZE" for f in found),
        deadline=deadline,
    )
    if require or forbid:
        text = _pdf_text(path)
        if not text:
            found.append(
                Finding(
                    "PDF_NO_TEXT",
                    "info",
                    where,
                    "no text layer to search (SHX and plotted-as-geometry text is not text); "
                    "required and forbidden words were not checked",
                )
            )
        else:
            for word in require:
                if re.sub(r"\s+", " ", word).strip().lower() not in text:
                    found.append(
                        Finding(
                            "PDF_WORD_MISSING", "error", where, f"required text not found: {word!r}"
                        )
                    )
            for word in forbid:
                if re.sub(r"\s+", " ", word).strip().lower() in text:
                    found.append(
                        Finding(
                            "PDF_WORD_FORBIDDEN", "error", where, f"forbidden text found: {word!r}"
                        )
                    )
    return found


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def _parse_size(text: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*", text)
    if not match:
        raise CadError("BAD_ARGS", f"--size must look like 420x297 (millimetres), got {text!r}")
    return float(match.group(1)), float(match.group(2))


def _layout_name(info: dict[str, Any], name: str) -> str | None:
    for layout in info["layouts"]:
        if layout["name"].lower() == name.lower() and layout["name"].lower() != "model":
            return str(layout["name"])
    return None


def _layout_size(info: dict[str, Any], name: str) -> tuple[float, float]:
    found = _layout_name(info, name)
    if found is not None:
        for layout in info["layouts"]:
            if layout["name"] == found:
                size = layout["page_setup"]["paper_size_mm"]
                if min(size) <= 0:
                    raise CadError("NO_PAPER_SIZE", f"layout {found!r} has no paper size")
                return float(size[0]), float(size[1])
    names = [x["name"] for x in info["layouts"] if x["name"].lower() != "model"]
    raise CadError(
        "LAYOUT_NOT_FOUND",
        f"no layout named {name!r}",
        hint="layouts: " + ", ".join(names[:12]),
    )


def _check_frame_layer(doc: Any, name: str) -> None:
    layers = sorted((str(x.dxf.name) for x in doc.layers), key=str.lower)
    if name.lower() not in {x.lower() for x in layers}:
        shown = layers[:30]
        more = f" (+{len(layers) - len(shown)} more)" if len(layers) > len(shown) else ""
        raise CadError(
            "BAD_ARGS",
            f"--frame-layer {name!r} is not a layer of the drawing",
            hint="layers: " + ", ".join(shown) + more,
        )


def _run_qa(args: argparse.Namespace) -> Result:
    if args.file is None and args.pdf is None:
        raise CadError("BAD_ARGS", "give a drawing, a --pdf, or both")
    if args.layout and args.file is None and args.size is None:
        raise CadError("BAD_ARGS", "--layout needs the drawing it belongs to (or use --size)")
    if args.file is None and args.frame_layer:
        raise CadError("BAD_ARGS", "--frame-layer needs the drawing it belongs to")
    if args.file is None and args.baseline:
        raise CadError("BAD_ARGS", "--baseline needs the drawing to compare with it")
    ctx = _new_run("qa", args)
    result = Result("qa", backend="ezdxf")
    deadline = Deadline(args.timeout)
    expected = _parse_size(args.size) if args.size else None
    found: list[Finding] = []
    frames: dict[str, list[float] | None] | None = None
    expected_frame: tuple[float, float, float, float] | None = None
    frame_skipped: str | None = None
    source = args.pdf or args.file
    if args.file is not None:
        loaded = _open(args.file, args, ctx, result)
        info = build_info(loaded, False)
        if args.layout and expected is None:
            expected = _layout_size(info, args.layout)
        if args.frame_layer:
            _check_frame_layer(loaded.doc, args.frame_layer)
        found += check_drawing(loaded.doc, info)
        report = check_frames(loaded.doc, info, frame_layer=args.frame_layer, deadline=deadline)
        found += report.findings
        frames = report.frames
        if args.baseline is not None:
            earlier = _open(args.baseline, args, ctx, result)
            found += check_baseline(loaded.doc, earlier.doc, deadline=deadline)
        layout_name = _layout_name(info, args.layout) if args.layout else None
        if layout_name in report.found:
            setup, frame = report.found[layout_name]
            frame_skipped = setup.position_skip_reason()
            if frame_skipped is None:
                expected_frame = setup.page_box(frame.box)
    if args.pdf is not None:
        found += check_pdf(
            args.pdf,
            expected_mm=expected,
            expect_pages=args.pages,
            require=tuple(args.require or ()),
            forbid=tuple(args.forbid or ()),
            expected_frame_mm=expected_frame,
            frame_skipped=frame_skipped,
            deadline=deadline,
        )
    found.sort(key=lambda f: SEVERITIES.index(f.severity))
    counts = {s: sum(1 for f in found if f.severity == s) for s in SEVERITIES}  # before the cap
    found, dropped = cap_findings(found)
    path = ctx.path("findings.json")
    payload: dict[str, Any] = {"findings": [f.as_dict() for f in found]}
    if frames is not None:
        payload["frames"] = frames
    if dropped:
        payload["truncated"] = dropped
    _write_json(path, payload)
    ctx.add_output(result, "findings", path, source=source)
    result.summary = {
        "errors": counts["error"],
        "warnings": counts["warning"],
        "info": counts["info"],
        "first": [
            {k: v for k, v in f.as_dict().items() if k != "message"}
            for f in found[:SUMMARY_FINDINGS]
        ],
    }
    if dropped:
        result.summary["not_listed"] = dropped
        for finding_id, n in dropped.items():
            result.warn(f"{finding_id}: {n} more not listed (at most {MAX_FINDINGS_PER_ID} each)")
    for f in found[:SUMMARY_FINDINGS]:
        if f.severity != "info":
            result.warn(f"{f.id} {f.where}: {f.message}")
    if counts["error"]:
        result.exit_code = ExitCode.PARTIAL
        result.add_error(
            CadError(
                "QA_FAILED",
                f"{counts['error']} error(s) and {counts['warning']} warning(s) found",
                hint="read findings.json; fix the cause and run qa again",
            )
        )
    return _finish(result, ctx)


def _add_qa_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path, nargs="?", help="DXF or DWG drawing (optional with --pdf)")
    p.add_argument("--pdf", type=Path, help="a plotted PDF to check")
    p.add_argument("--layout", help="layout the PDF was plotted from (gives the expected size)")
    p.add_argument("--size", help="expected page size in mm, e.g. 420x297 (either orientation)")
    p.add_argument("--pages", type=int, default=1, help="expected page count (default 1)")
    p.add_argument("--require", action="append", help="text that must be in the PDF (repeatable)")
    p.add_argument(
        "--forbid", action="append", help="text that must not be in the PDF (repeatable)"
    )
    p.add_argument(
        "--frame-layer",
        help="layer that holds the sheet frame (overrides the search for the largest closed "
        "rectangle)",
    )
    p.add_argument(
        "--baseline",
        type=Path,
        help="earlier version of the same drawing: report texts (same handle) whose estimated "
        "box grew or that wrap to more lines",
    )
    _add_common(p)


COMMANDS = {
    "qa": Command(
        help="findings with severity on a drawing and on a plotted PDF; exit 7 only for errors",
        add_arguments=_add_qa_args,
        run=_run_qa,
        epilog="example: cad.py qa plan.dxf --pdf sheet.pdf --layout Sheet-A --require 'Rev. C'; "
        "after editing texts: cad.py qa new.dxf --baseline old.dxf",
    ),
}
