"""qa: findings with a severity on a drawing and on a plotted PDF.

Mechanical checks only (what a script can decide). Whether a layout looks right stays with the
agent (see references/visual-qa.md). Errors end the run with exit 7; warnings and info do not.
"""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .command import Command
from .drawing_info import build_info
from .dxf import _open
from .result import CadError, ExitCode, Result
from .util import _add_common, _finish, _new_run, _write_json

SEVERITIES = ("error", "warning", "info")
SUMMARY_FINDINGS = 5


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


def check_pdf(
    path: Path,
    *,
    expected_mm: tuple[float, float] | None = None,
    expect_pages: int = 1,
    require: tuple[str, ...] = (),
    forbid: tuple[str, ...] = (),
) -> list[Finding]:
    """Checks on a plotted PDF: readable, page count and size, content, required words."""
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


def _layout_size(info: dict[str, Any], name: str) -> tuple[float, float]:
    for layout in info["layouts"]:
        if layout["name"].lower() == name.lower() and layout["name"].lower() != "model":
            size = layout["page_setup"]["paper_size_mm"]
            if min(size) <= 0:
                raise CadError("NO_PAPER_SIZE", f"layout {layout['name']!r} has no paper size")
            return float(size[0]), float(size[1])
    names = [x["name"] for x in info["layouts"] if x["name"].lower() != "model"]
    raise CadError(
        "LAYOUT_NOT_FOUND",
        f"no layout named {name!r}",
        hint="layouts: " + ", ".join(names[:12]),
    )


def _run_qa(args: argparse.Namespace) -> Result:
    if args.file is None and args.pdf is None:
        raise CadError("BAD_ARGS", "give a drawing, a --pdf, or both")
    if args.layout and args.file is None and args.size is None:
        raise CadError("BAD_ARGS", "--layout needs the drawing it belongs to (or use --size)")
    ctx = _new_run("qa", args)
    result = Result("qa", backend="ezdxf")
    expected = _parse_size(args.size) if args.size else None
    found: list[Finding] = []
    source = args.pdf or args.file
    if args.file is not None:
        loaded = _open(args.file, args, ctx, result)
        info = build_info(loaded, False)
        if args.layout and expected is None:
            expected = _layout_size(info, args.layout)
        found += check_drawing(loaded.doc, info)
    if args.pdf is not None:
        found += check_pdf(
            args.pdf,
            expected_mm=expected,
            expect_pages=args.pages,
            require=tuple(args.require or ()),
            forbid=tuple(args.forbid or ()),
        )
    found.sort(key=lambda f: SEVERITIES.index(f.severity))
    path = ctx.path("findings.json")
    _write_json(path, {"findings": [f.as_dict() for f in found]})
    ctx.add_output(result, "findings", path, source=source)
    counts = {s: sum(1 for f in found if f.severity == s) for s in SEVERITIES}
    result.summary = {
        "errors": counts["error"],
        "warnings": counts["warning"],
        "info": counts["info"],
        "first": [
            {k: v for k, v in f.as_dict().items() if k != "message"}
            for f in found[:SUMMARY_FINDINGS]
        ],
    }
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
    _add_common(p)


COMMANDS = {
    "qa": Command(
        help="findings with severity on a drawing and on a plotted PDF; exit 7 only for errors",
        add_arguments=_add_qa_args,
        run=_run_qa,
        epilog="example: cad.py qa plan.dxf --pdf sheet.pdf --layout Sheet-A --require 'Rev. C'",
    ),
}
