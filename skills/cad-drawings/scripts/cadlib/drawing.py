"""Opening a DXF or DWG as an ezdxf document."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import ezdxf
import ezdxf.bbox
import ezdxf.units
from ezdxf import recover

from .result import CadError

if TYPE_CHECKING:
    from ezdxf.document import Drawing

    from .runs import RunContext


@dataclass
class Loaded:
    """An opened drawing together with how it was obtained."""

    doc: Drawing
    source: Path
    dxf_path: Path
    warnings: list[str] = field(default_factory=list)
    approximate: bool = False
    converter: str | None = None


def _resolve_source(path: Path | str) -> Path:
    src = Path(path).expanduser()
    if not src.is_file():
        raise CadError("FILE_NOT_FOUND", f"file not found: {src}", hint="check the path")
    return src.resolve()


def _read_dxf(path: Path, warnings: list[str]) -> Drawing:
    try:
        return ezdxf.readfile(path)
    except (ezdxf.DXFError, UnicodeDecodeError, ValueError, IndexError, KeyError) as exc:
        warnings.append(
            f"{path.name}: strict read failed ({type(exc).__name__}: {exc}); "
            "recovered with ezdxf.recover, some content may be missing"
        )
    try:
        doc, auditor = recover.readfile(path)
    except (OSError, ezdxf.DXFError) as exc:
        raise CadError(
            "UNREADABLE",
            f"cannot read {path.name}: {exc}",
            hint="the file is not a valid DXF; try converting from the DWG again",
        ) from exc
    if auditor.has_errors:
        warnings.append(f"{path.name}: audit found {len(auditor.errors)} structural error(s)")
    return doc


def open_drawing(
    path: Path | str,
    ctx: RunContext,
    *,
    allow_com: bool = False,
    prefer: str | None = None,
) -> Loaded:
    """Open a DXF or DWG. A DWG goes through ``convert.ensure_dxf`` (the one conversion and
    cache pipeline); a CAD application is used only with ``allow_com`` or ``prefer="com"``.
    Every warning of the conversion is kept in ``Loaded.warnings``."""
    src = _resolve_source(path)
    suffix = src.suffix.lower()
    warnings: list[str] = []
    if suffix == ".dxf":
        return Loaded(_read_dxf(src, warnings), src, src, warnings)
    if suffix != ".dwg":
        raise CadError(
            "UNSUPPORTED", f"unsupported file type {src.suffix!r} (expected .dxf or .dwg)"
        )
    from . import convert

    made = convert.ensure_dxf(src, ctx, allow_com=allow_com, prefer=prefer)
    warnings.extend(made.warnings)
    if made.approximate and not any("approximate" in w.lower() for w in warnings):
        warnings.append(
            f"{src.name}: converted with an approximate converter; objects may be missing"
        )
    doc = _read_dxf(made.path, warnings)
    return Loaded(doc, src, made.path, warnings, bool(made.approximate), made.backend)


def load_dxf(path: Path, ctx: RunContext) -> Drawing:
    """Open ``path`` (DXF, or DWG through ``convert.ensure_dxf``) and return the document."""
    return open_drawing(path, ctx).doc
