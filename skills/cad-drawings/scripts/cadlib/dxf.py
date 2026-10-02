"""Reading DXF (and, through conversion, DWG) with ezdxf: info, find, dump, fingerprint, diff.

Everything here works on a DXF document, never through COM. DWG input is converted first
(``convert.convert``) and the converted DXF is cached by the sha1 of the source.

Terminology used in the output:

* ``space``: ``model`` | ``paper`` | ``block`` (block definition) | ``table`` (layer/block names).
* ``visible_in_space``: the object is drawn in its own space (layer on and thawed, not flagged
  invisible, block definition actually instantiated). ``None`` when not applicable.
* ``prints_on``: layouts on which the object lands, derived from the geometry of the layout's
  viewports (never from a plot). ``None`` when the viewport data is insufficient.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ezdxf
import ezdxf.bbox
import ezdxf.units
from ezdxf import recover
from ezdxf.math import Matrix44, Vec3
from ezdxf.tools import text as dxf_text

from .command import Command
from .result import CadError, ExitCode, Result

if TYPE_CHECKING:
    from ezdxf.document import Drawing

    from .runs import RunContext

FINGERPRINT_SCHEMA = 1
DEFAULT_DUMP_LIMIT = 5000
DEFAULT_FIND_LIMIT = 20000
MAX_BLOCK_PLACEMENTS = 500
MAX_NESTING = 8
SUMMARY_CHANGES = 10
DIFF_JSON_CAP = 5000
_EPS = 1e-6
_ANON_NAME = re.compile(r"^\*[A-Za-z]\d*$")
_BOUND_XREF = re.compile(r"^(?P<xref>.+)\$\d+\$(?P<name>.+)$")
_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION", "MULTILEADER", "MLEADER"})
_ATTRIB_TYPES = frozenset({"ATTRIB", "ATTDEF"})
_LAYOUT_BLOCKS = ("*model_space", "*paper_space")


# --------------------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------------------


class Deadline:
    """Soft time limit checked from long loops; ``None`` disables it."""

    def __init__(self, seconds: float | None) -> None:
        self._end = None if not seconds else time.monotonic() + seconds

    def check(self) -> None:
        if self._end is not None and time.monotonic() > self._end:
            raise CadError(
                "TIMEOUT",
                "time limit reached while reading the drawing",
                exit_code=ExitCode.TIMEOUT,
                hint="raise --timeout or narrow the request",
            )


def r4(value: float) -> float:
    """Round for signatures and output; folds -0.0 into 0.0."""
    out = round(float(value), 4)
    return 0.0 if out == 0 else out


def _pt(point: Any, dims: int = 2) -> list[float]:
    v = Vec3(point)
    return [r4(v.x), r4(v.y)] if dims == 2 else [r4(v.x), r4(v.y), r4(v.z)]


def _hash(obj: Any, size: int = 16) -> str:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:size]


def _is_anonymous(name: str) -> bool:
    return bool(_ANON_NAME.match(name))


def _norm_block_name(name: str) -> str:
    """Anonymous block names (``*U7``, ``*D12``) are renumbered on every save."""
    return "*" + name[1].upper() if _is_anonymous(name) else name


def _brief(value: Any, limit: int = 120) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= limit else text[: limit - 3] + "..."


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, float):
        return r4(value)
    if isinstance(value, str | int | bool) or value is None:
        return value
    if hasattr(value, "x") and hasattr(value, "y"):
        return _pt(value, 3 if hasattr(value, "z") else 2)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    return str(value)


def parse_floats(text: str, count: int, what: str) -> list[float]:
    try:
        values = [float(p) for p in text.split(",")]
    except ValueError:
        values = []
    if len(values) != count:
        raise CadError(
            "BAD_ARGS",
            f"{what} must be {count} comma-separated numbers, got {text!r}",
            exit_code=ExitCode.BAD_ARGS,
        )
    return values


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path, default=None, help="base directory for run folders")
    parser.add_argument(
        "--timeout", type=float, default=240.0, help="soft time limit in seconds (default 240)"
    )


def _new_run(command: str, args: argparse.Namespace) -> RunContext:
    from .runs import RunContext

    return RunContext.create(command, getattr(args, "run_dir", None))


def _finish(result: Result, ctx: RunContext) -> Result:
    ctx.bind(result)
    ctx.finish()
    return result


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------


@dataclass
class Loaded:
    """An opened drawing together with how it was obtained."""

    doc: Drawing
    source: Path
    dxf_path: Path
    warnings: list[str] = field(default_factory=list)
    approximate: bool = False
    converter: str | None = None
    temp: Path | None = None

    def cleanup(self) -> None:
        """Delete the temporary converted DXF (one file; cached copies are never touched)."""
        if self.temp is not None:
            with contextlib.suppress(OSError):
                self.temp.unlink()
            self.temp = None


def _resolve_source(path: Path | str) -> Path:
    src = Path(path).expanduser()
    if not src.is_file():
        raise CadError(
            "FILE_NOT_FOUND",
            f"file not found: {src}",
            exit_code=ExitCode.PRECONDITION_FAILED,
            hint="check the path",
        )
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
            exit_code=ExitCode.ERROR,
            hint="the file is not a valid DXF; try converting from the DWG again",
        ) from exc
    if auditor.has_errors:
        warnings.append(f"{path.name}: audit found {len(auditor.errors)} structural error(s)")
    return doc


def open_drawing(path: Path | str, ctx: RunContext, *, use_cache: bool = True) -> Loaded:
    """Open a DXF or DWG. ``use_cache=False`` converts into a temporary file that
    :meth:`Loaded.cleanup` deletes (a cached conversion is still used when present)."""
    src = _resolve_source(path)
    suffix = src.suffix.lower()
    if suffix == ".dxf":
        warnings: list[str] = []
        doc = _read_dxf(src, warnings)
        return Loaded(doc, src, src, warnings)
    if suffix != ".dwg":
        raise CadError(
            "BAD_ARGS",
            f"unsupported file type {src.suffix!r} (expected .dxf or .dwg)",
            exit_code=ExitCode.BAD_ARGS,
        )
    from .runs import cache_get, cache_put, file_sha1

    sha = file_sha1(src)
    for kind, approximate in (("dxf", False), ("dxf-approx", True)):
        cached = cache_get(sha, kind)
        if cached is not None:
            ctx.log(f"using cached DXF for {src.name}")
            warnings = []
            if approximate:
                warnings.append(f"{src.name}: cached DXF came from an approximate converter")
            doc = _read_dxf(cached, warnings)
            return Loaded(doc, src, cached, warnings, approximate)
    from .convert import convert

    dst = ctx.path(f"converted-{sha[:10]}.dxf")
    converter, warnings = convert(src, dst, "dxf", ctx=ctx)
    warnings = list(warnings)
    approximate = bool(getattr(converter, "approximate", False))
    temp: Path | None = dst
    dxf_path = dst
    if use_cache:
        dxf_path = cache_put(sha, "dxf-approx" if approximate else "dxf", dst)
        if dxf_path != dst:
            with contextlib.suppress(OSError):
                dst.unlink()
            temp = None
    try:
        doc = _read_dxf(dxf_path, warnings)
    except CadError:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink()
        raise
    return Loaded(doc, src, dxf_path, warnings, approximate, getattr(converter, "name", None), temp)


def load_dxf(path: Path, ctx: RunContext) -> Drawing:
    """Open ``path`` (DXF, or DWG through ``convert`` with caching) and return the document."""
    return open_drawing(path, ctx).doc


# --------------------------------------------------------------------------------------
# viewports and visibility
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewportInfo:
    handle: str
    vp_id: int
    status: int
    center: tuple[float, float]
    size: tuple[float, float]
    view_center: tuple[float, float]  # as stored (display coordinate system)
    view_height: float
    twist: float
    frozen_layers: tuple[str, ...]
    top_view: bool
    clipped: bool  # non-rectangular clipping boundary (the rectangle is only an approximation)
    overall: bool = False
    target: tuple[float, float] = (0.0, 0.0)  # view target point (origin for plan views)

    @property
    def scale(self) -> float:
        """View height per paper height: model units per paper unit (0.0 when undefined)."""
        return self.view_height / self.size[1] if self.size[1] else 0.0

    @property
    def status_reliable(self) -> bool:
        return self.status > 0

    @property
    def usable(self) -> bool:
        return self.top_view and self.size[0] > 0 and self.size[1] > 0 and self.view_height > 0

    @property
    def view_center_wcs(self) -> tuple[float, float]:
        """Model-space point at the middle of the window.

        The stored view centre is in the display coordinate system:
        ``DCS = R(+twist) * (WCS - target)`` (verified against CAD plots).
        """
        a = math.radians(self.twist)
        ca, sa = math.cos(a), math.sin(a)
        cx, cy = self.view_center
        return (self.target[0] + cx * ca + cy * sa, self.target[1] - cx * sa + cy * ca)

    def model_to_paper(self, x: float, y: float) -> tuple[float, float]:
        """Paper-space position of a model-space point seen through this viewport."""
        s = self.size[1] / self.view_height  # paper units per model unit
        a = math.radians(self.twist)
        ca, sa = math.cos(a), math.sin(a)
        px, py = x - self.target[0], y - self.target[1]
        dx = s * (px * ca - py * sa - self.view_center[0])
        dy = s * (px * sa + py * ca - self.view_center[1])
        return (self.center[0] + dx, self.center[1] + dy)

    def contains_model_point(self, x: float, y: float) -> bool:
        px, py = self.model_to_paper(x, y)
        tol = _EPS * max(1.0, self.size[0], self.size[1])
        return (
            abs(px - self.center[0]) <= self.size[0] / 2 + tol
            and abs(py - self.center[1]) <= self.size[1] / 2 + tol
        )

    def freezes(self, layer: str) -> bool:
        low = layer.lower()
        return any(name.lower() == low for name in self.frozen_layers)


def _viewport_info(vp: Any) -> ViewportInfo:
    d = vp.dxf
    return ViewportInfo(
        handle=str(d.handle),
        vp_id=int(d.get("id", 0)),
        status=int(d.get("status", 0)),
        center=(float(d.center.x), float(d.center.y)),
        size=(float(d.width), float(d.height)),
        view_center=(float(d.view_center_point.x), float(d.view_center_point.y)),
        view_height=float(d.view_height),
        twist=float(d.get("view_twist_angle", 0.0)),
        frozen_layers=tuple(vp.frozen_layers),
        top_view=bool(vp.is_top_view),
        clipped=bool(vp.has_extended_clipping_path),
        target=(float(d.view_target_point.x), float(d.view_target_point.y)),
    )


def layout_viewports(layout: Any) -> list[ViewportInfo]:
    """All viewports of a paper layout in file order, the overall one flagged ``overall``."""
    infos = [_viewport_info(vp) for vp in layout.query("VIEWPORT")]
    overall: int | None = None
    for i, vp in enumerate(infos):
        if vp.vp_id == 1:
            overall = i
            break
    if overall is None:
        for i, vp in enumerate(infos):
            same_center = (
                abs(vp.view_center[0] - vp.center[0]) < _EPS
                and abs(vp.view_center[1] - vp.center[1]) < _EPS
            )
            if same_center and abs(vp.view_height - vp.size[1]) < _EPS * max(1.0, vp.size[1]):
                overall = i
                break
    if overall is not None:
        infos[overall] = ViewportInfo(**{**infos[overall].__dict__, "overall": True})
    return infos


@dataclass
class Placement:
    layout: str  # layout name (``Model`` for model space)
    matrices: list[Matrix44]
    layers: list[str]  # layers of the INSERTs on the way


class DocModel:
    """Layer states, viewports and block usage of one document, computed lazily and cached."""

    def __init__(self, doc: Drawing) -> None:
        self.doc = doc
        self.hidden_layers: set[str] = set()
        for layer in doc.layers:
            if layer.is_off() or layer.is_frozen():
                self.hidden_layers.add(layer.dxf.name.lower())
        self.layout_names = list(doc.layouts.names_in_taborder())
        self.paper_names = [n for n in self.layout_names if n.lower() != "model"]
        self.viewports: dict[str, list[ViewportInfo]] = {
            name: layout_viewports(doc.layouts.get(name)) for name in self.paper_names
        }
        self.warnings: list[str] = []
        for name, vps in self.viewports.items():
            for vp in vps:
                if not vp.overall and not vp.status_reliable:
                    self.warnings.append(
                        f"layout {name!r}: viewport status {vp.status} is not reliable "
                        "(COM-exported DXF reports it wrongly); treated as on"
                    )
        self._users: dict[str, list[tuple[str, str, str | None, Any]]] | None = None
        self._placements: dict[str, list[Placement]] = {}

    def layer_hidden(self, name: str) -> bool:
        return name.lower() in self.hidden_layers

    # -- block usage -------------------------------------------------------------------

    def _block_users(self) -> dict[str, list[tuple[str, str, str | None, Any]]]:
        if self._users is None:
            users: dict[str, list[tuple[str, str, str | None, Any]]] = defaultdict(list)
            for loc in iter_locations(self.doc, attribs=False):
                if loc.entity.dxftype() == "INSERT":
                    users[loc.entity.dxf.name.lower()].append(
                        (loc.space, loc.layout or "", loc.block, loc.entity)
                    )
            self._users = users
        return self._users

    def placements(self, block_name: str, _seen: frozenset[str] = frozenset()) -> list[Placement]:
        """Where a block definition ends up (model/paper layout + transform chain)."""
        key = block_name.lower()
        if key in self._placements:
            return self._placements[key]
        out: list[Placement] = []
        if key in _seen or len(_seen) >= MAX_NESTING:
            return out
        for space, layout, container, insert in self._block_users().get(key, []):
            try:
                matrix = insert.matrix44()
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            if space in ("model", "paper"):
                out.append(Placement(layout, [matrix], [insert.dxf.layer]))
            elif container is not None:
                for sub in self.placements(container, _seen | {key}):
                    out.append(
                        Placement(
                            sub.layout, [matrix, *sub.matrices], [insert.dxf.layer, *sub.layers]
                        )
                    )
            if len(out) >= MAX_BLOCK_PLACEMENTS:
                break
        if not _seen:
            self._placements[key] = out
        return out

    # -- printing ----------------------------------------------------------------------

    def model_point_prints_on(
        self, x: float, y: float, layers: Iterable[str]
    ) -> tuple[list[str] | None, str | None]:
        """Layouts whose active viewport window contains the model-space point."""
        layers = list(layers)
        if any(self.layer_hidden(name) for name in layers):
            return [], None
        hits: list[str] = []
        notes: list[str] = []
        unknown = False
        for name, vps in self.viewports.items():
            for vp in vps:
                if vp.overall:
                    continue
                if not vp.usable:
                    unknown = True
                    notes.append(f"{name}: viewport has no usable top-view window")
                    continue
                if any(vp.freezes(layer) for layer in layers):
                    continue
                if vp.contains_model_point(x, y):
                    hits.append(name)
                    if vp.clipped:
                        notes.append(f"{name}: non-rectangular clip approximated by its rectangle")
                    if not vp.status_reliable:
                        notes.append(f"{name}: viewport status {vp.status} treated as on")
                    break
        if unknown and not hits:
            return None, "; ".join(dict.fromkeys(notes))
        return hits, "; ".join(dict.fromkeys(notes)) or None

    def locate(self, loc: Loc) -> dict[str, Any]:
        """``visible_in_space``, ``prints_on`` and ``prints_on_note`` for an entity location."""
        entity = loc.entity
        chain = [entity.dxf.layer]
        if loc.parent is not None:
            chain.append(loc.parent.dxf.layer)
        invisible = bool(entity.dxf.get("invisible", 0)) or (
            loc.parent is not None and bool(loc.parent.dxf.get("invisible", 0))
        )
        hidden = invisible or any(self.layer_hidden(name) for name in chain)
        point = anchor_of(entity)
        if loc.space == "paper":
            return _loc_result(not hidden, [] if hidden else [loc.layout or ""], None)
        if loc.space == "model":
            if hidden:
                return _loc_result(False, [], None)
            if point is None:
                return _loc_result(True, None, "no insertion point to test against viewports")
            prints, note = self.model_point_prints_on(point[0], point[1], chain)
            return _loc_result(True, prints, note)
        # block definition: only the instantiated copies are drawn
        placements = self.placements(loc.block or "")
        if not placements:
            return _loc_result(False, [], None)
        if hidden:
            return _loc_result(False, [], None)
        layouts: list[str] = []
        notes: list[str] = []
        unknown = False
        visible = False
        for placement in placements:
            if any(self.layer_hidden(name) for name in placement.layers):
                continue
            visible = True
            if placement.layout.lower() != "model":
                if placement.layout not in layouts:
                    layouts.append(placement.layout)
                continue
            if point is None:
                unknown = True
                continue
            world = Vec3(point[0], point[1], 0.0)
            for matrix in placement.matrices:
                world = matrix.transform(world)
            prints, note = self.model_point_prints_on(world.x, world.y, [*chain, *placement.layers])
            if prints is None:
                unknown = True
            else:
                layouts.extend(n for n in prints if n not in layouts)
            if note:
                notes.append(note)
        notes.append("block content positioned through INSERT transforms (base point ignored)")
        if unknown and not layouts:
            return _loc_result(visible, None, "; ".join(dict.fromkeys(notes)))
        return _loc_result(visible, layouts, "; ".join(dict.fromkeys(notes)))


def _loc_result(
    visible: bool | None, prints_on: list[str] | None, note: str | None
) -> dict[str, Any]:
    out: dict[str, Any] = {"visible_in_space": visible, "prints_on": prints_on}
    if note:
        out["prints_on_note"] = note
    return out


# --------------------------------------------------------------------------------------
# entity traversal and description
# --------------------------------------------------------------------------------------


@dataclass
class Loc:
    space: str  # model | paper | block
    layout: str | None
    block: str | None
    entity: Any
    parent: Any | None = None


def iter_locations(doc: Drawing, *, attribs: bool = True) -> Iterator[Loc]:
    """Entities of model space, every paper layout and every block definition (plus ATTRIBs)."""

    def expand(entity: Any, space: str, layout: str | None, block: str | None) -> Iterator[Loc]:
        yield Loc(space, layout, block, entity)
        if attribs and entity.dxftype() == "INSERT":
            for attrib in entity.attribs:
                yield Loc(space, layout, block, attrib, entity)

    for entity in doc.modelspace():
        yield from expand(entity, "model", "Model", None)
    for name in doc.layouts.names_in_taborder():
        if name.lower() == "model":
            continue
        for entity in doc.layouts.get(name):
            yield from expand(entity, "paper", name, None)
    for blk in doc.blocks:
        if blk.name.lower().startswith(_LAYOUT_BLOCKS):
            continue
        for entity in blk:
            yield from expand(entity, "block", None, blk.name)


def anchor_of(entity: Any) -> list[float] | None:
    """A representative 2D point: insertion point, first vertex, centre or start."""
    d = entity.dxf
    for attr in ("insert", "center", "start", "location", "defpoint", "vtx0", "base_point"):
        if d.hasattr(attr):
            return _pt(d.get(attr))
    kind = entity.dxftype()
    with contextlib.suppress(Exception):
        if kind == "LWPOLYLINE":
            first = next(iter(entity.get_points("xy")), None)
            return [r4(first[0]), r4(first[1])] if first else None
        if kind == "POLYLINE":
            return _pt(entity.vertices[0].dxf.location)
    return None


_UNICODE_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def _plain_mtext(raw: str) -> str:
    """MTEXT without formatting codes; unicode escapes (backslash U plus four hex) are decoded."""
    decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), raw)
    return str(dxf_text.plain_mtext(decoded))


def entity_text(entity: Any) -> tuple[str | None, str | None]:
    """``(plain, raw)`` text of a text-bearing entity, ``(None, None)`` for the others."""
    kind = entity.dxftype()
    try:
        if kind in ("TEXT", "ATTRIB", "ATTDEF"):
            raw = str(entity.dxf.get("text", ""))
            return entity.plain_text(), raw
        if kind == "MTEXT":
            raw = str(entity.text)
            return _plain_mtext(raw), raw
        if kind == "DIMENSION":
            raw = str(entity.dxf.get("text", ""))
            return (raw, raw) if raw not in ("", "<>", " ") else (None, None)
        if kind in ("MULTILEADER", "MLEADER") and entity.has_mtext_content:
            raw = str(entity.get_mtext_content())
            return _plain_mtext(raw), raw
    except (AttributeError, ValueError, TypeError):
        return None, None
    return None, None


def _style(entity: Any) -> dict[str, Any]:
    d = entity.dxf
    return {
        "color": int(d.get("color", 256)),
        "linetype": str(d.get("linetype", "BYLAYER")),
        "lineweight": int(d.get("lineweight", -1)),
    }


def _rel(point: Any, anchor: list[float]) -> list[float]:
    p = _pt(point, 3)
    return [r4(p[0] - anchor[0]), r4(p[1] - anchor[1]), p[2]]


def _fallback_bbox(entity: Any) -> list[float] | None:
    with contextlib.suppress(Exception):
        box = ezdxf.bbox.extents([entity], fast=True)
        if box.has_data:
            return [r4(box.extmin.x), r4(box.extmin.y), r4(box.extmax.x), r4(box.extmax.y)]
    return None


def _points_bbox(points: Iterable[Iterable[float]]) -> list[float] | None:
    pts = [(float(p[0]), float(p[1])) for p in points]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [r4(min(xs)), r4(min(ys)), r4(max(xs)), r4(max(ys))]


def geometry(
    entity: Any,
) -> tuple[list[float] | None, dict[str, Any], dict[str, Any], list[float] | None]:
    """``(anchor, absolute props, anchor-relative props, bbox)`` of an entity.

    The relative props identify the *shape* independent of position, so a pure translation
    leaves them unchanged; absolute props are for display.
    """
    kind = entity.dxftype()
    d = entity.dxf
    anchor = anchor_of(entity)
    style = _style(entity)
    absolute: dict[str, Any] = dict(style)
    relative: dict[str, Any] = dict(style)
    bbox: list[float] | None = None
    a = anchor or [0.0, 0.0]

    def both(key: str, abs_value: Any, rel_value: Any = None) -> None:
        absolute[key] = abs_value
        relative[key] = abs_value if rel_value is None else rel_value

    if kind == "LINE":
        both("end", _pt(d.end, 3), _rel(d.end, a))
        bbox = _points_bbox([d.start, d.end])
    elif kind == "CIRCLE":
        both("radius", r4(d.radius))
        bbox = [r4(d.center.x - d.radius), r4(d.center.y - d.radius)]
        bbox += [r4(d.center.x + d.radius), r4(d.center.y + d.radius)]
    elif kind == "ARC":
        both("radius", r4(d.radius))
        both("angles", [r4(d.start_angle), r4(d.end_angle)])
        bbox = _fallback_bbox(entity)
    elif kind == "ELLIPSE":
        both("major_axis", _pt(d.major_axis, 3))
        both("ratio", r4(d.ratio))
        both("params", [r4(d.start_param), r4(d.end_param)])
        bbox = _fallback_bbox(entity)
    elif kind == "LWPOLYLINE":
        pts = [(float(x), float(y), float(b)) for x, y, b in entity.get_points("xyb")]
        absolute["points"] = [[r4(x), r4(y), r4(b)] for x, y, b in pts]
        relative["points"] = [[r4(x - a[0]), r4(y - a[1]), r4(b)] for x, y, b in pts]
        both("closed", bool(entity.closed))
        both("const_width", r4(d.get("const_width", 0.0)))
        bbox = _points_bbox(pts)
    elif kind == "POLYLINE":
        vts = [Vec3(v.dxf.location) for v in entity.vertices]
        absolute["points"] = [_pt(v, 3) for v in vts]
        relative["points"] = [_rel(v, a) for v in vts]
        both("closed", bool(entity.is_closed))
        bbox = _points_bbox(vts)
    elif kind in ("TEXT", "ATTRIB", "ATTDEF"):
        both("text", str(d.get("text", "")))
        both("height", r4(d.get("height", 0.0)))
        both("rotation", r4(d.get("rotation", 0.0)))
        both("style", str(d.get("style", "")))
        if kind != "TEXT":
            both("tag", str(d.get("tag", "")))
        bbox = _fallback_bbox(entity)
    elif kind == "MTEXT":
        both("text", str(entity.text))
        both("height", r4(d.get("char_height", 0.0)))
        both("rotation", r4(entity.get_rotation()))
        both("width", r4(d.get("width", 0.0)))
        both("style", str(d.get("style", "")))
        bbox = _fallback_bbox(entity)
    elif kind == "INSERT":
        both("block", _norm_block_name(str(d.name)))
        both("scale", [r4(d.get("xscale", 1)), r4(d.get("yscale", 1)), r4(d.get("zscale", 1))])
        both("rotation", r4(d.get("rotation", 0.0)))
        both("array", [int(d.get("column_count", 1)), int(d.get("row_count", 1))])
        attribs = {str(at.dxf.tag): str(at.dxf.get("text", "")) for at in entity.attribs}
        both("attribs", dict(sorted(attribs.items())))
        bbox = _fallback_bbox(entity)
    elif kind == "POINT":
        bbox = [r4(d.location.x), r4(d.location.y), r4(d.location.x), r4(d.location.y)]
    elif kind == "VIEWPORT":
        info = _viewport_info(entity)
        both("size", [r4(info.size[0]), r4(info.size[1])])
        both("view_center", [r4(info.view_center[0]), r4(info.view_center[1])])
        both("target", _pt(info.target))
        both("view_height", r4(info.view_height))
        both("twist", r4(info.twist))
        both("frozen_layers", sorted(info.frozen_layers, key=str.lower))
        bbox = [
            r4(info.center[0] - info.size[0] / 2),
            r4(info.center[1] - info.size[1] / 2),
            r4(info.center[0] + info.size[0] / 2),
            r4(info.center[1] + info.size[1] / 2),
        ]
    elif kind == "SPLINE":
        cps = [Vec3(p) for p in entity.control_points]
        absolute["control_points"] = [_pt(p, 3) for p in cps]
        relative["control_points"] = [_rel(p, a) for p in cps]
        both("degree", int(entity.dxf.degree))
        bbox = _points_bbox(cps)
    elif kind in ("SOLID", "TRACE", "3DFACE"):
        vts = [Vec3(d.get(f"vtx{i}", (0, 0, 0))) for i in range(4)]
        absolute["vertices"] = [_pt(v, 3) for v in vts]
        relative["vertices"] = [_rel(v, a) for v in vts]
        bbox = _points_bbox(vts)
    else:
        bbox = _fallback_bbox(entity)
        if kind == "HATCH":
            both("pattern", str(d.get("pattern_name", "")))
            both("solid", bool(d.get("solid_fill", 0)))
            both("associative", bool(d.get("associative", 0)))
        if kind == "DIMENSION":
            both("text", str(d.get("text", "")))
            both("dimtype", int(d.get("dimtype", 0)))
            both("block", _norm_block_name(str(d.get("geometry", ""))))
        if bbox is not None:
            anchor = [bbox[0], bbox[1]]
            relative["size"] = [r4(bbox[2] - bbox[0]), r4(bbox[3] - bbox[1])]
            absolute["bbox_size"] = relative["size"]
    return anchor, absolute, relative, bbox


def describe(loc: Loc, *, with_bbox: bool = True) -> dict[str, Any]:
    """JSON-ready record of an entity location (used by dump and fingerprint)."""
    entity = loc.entity
    anchor, absolute, _rel_props, bbox = geometry(entity)
    rec: dict[str, Any] = {
        "handle": str(entity.dxf.handle),
        "type": entity.dxftype(),
        "space": loc.space,
        "layout": loc.layout,
        "block": loc.block,
        "layer": entity.dxf.layer,
        "anchor": anchor,
        "props": absolute,
    }
    if loc.parent is not None:
        rec["parent_handle"] = str(loc.parent.dxf.handle)
    plain, raw = entity_text(entity)
    if plain is not None:
        rec["text"], rec["raw"] = plain, raw
    if with_bbox:
        rec["bbox"] = bbox
    return rec


# --------------------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------------------


def _header_units(doc: Drawing) -> dict[str, Any]:
    insunits = int(doc.header.get("$INSUNITS", 0))
    measurement = int(doc.header.get("$MEASUREMENT", 0))
    return {
        "insunits": insunits,
        "insunits_name": ezdxf.units.decode(insunits) or "unitless",
        "measurement": measurement,
        "measurement_name": "metric" if measurement == 1 else "imperial",
    }


def _extents(doc: Drawing) -> dict[str, Any]:
    lo, hi = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
    if lo is not None and hi is not None and abs(lo[0]) < 1e19 and abs(hi[0]) < 1e19:
        return {"min": _pt(lo), "max": _pt(hi), "source": "header"}
    # header values are missing or unset: a cheap sweep over vertices and anchors (text and block
    # extents are not measured; ezdxf.bbox.extents costs ~0.15 ms per entity)
    xs: list[float] = []
    ys: list[float] = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        points: list[Any] = []
        if kind == "LINE":
            points = [entity.dxf.start, entity.dxf.end]
        elif kind == "CIRCLE":
            c, r = entity.dxf.center, entity.dxf.radius
            points = [(c.x - r, c.y - r), (c.x + r, c.y + r)]
        elif kind == "LWPOLYLINE":
            points = [(x, y) for x, y, *_ in entity.get_points("xy")]
        else:
            anchor = anchor_of(entity)
            points = [anchor] if anchor else []
        for p in points:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return {"min": None, "max": None, "source": "empty"}
    return {
        "min": [r4(min(xs)), r4(min(ys))],
        "max": [r4(max(xs)), r4(max(ys))],
        "source": "computed from vertices and anchors (text and block extents not measured)",
    }


def _dget(namespace: Any, attr: str, default: Any) -> Any:
    """``dxf.get`` that tolerates attributes the entity type does not define."""
    try:
        return namespace.get(attr, default)
    except AttributeError:
        return default


def _layout_info(doc: Drawing, model: DocModel, name: str) -> dict[str, Any]:
    layout = doc.layouts.get(name)
    d = layout.dxf
    info: dict[str, Any] = {"name": name, "block": layout.block_record_name}
    if name.lower() == "model":
        return info
    info["page_setup"] = {
        "paper_size_mm": [r4(d.get("paper_width", 0.0)), r4(d.get("paper_height", 0.0))],
        "paper_units": int(d.get("plot_paper_units", 1)),
        "rotation": int(d.get("plot_rotation", 0)),
        "plot_style": layout.get_plot_style_filename() or None,
        "device": str(_dget(d, "plot_configuration_file", "")) or None,
        "page_setup_name": str(_dget(d, "page_setup_name", "")) or None,
    }
    with contextlib.suppress(Exception):
        lo, hi = layout.get_paper_limits()
        info["page_setup"]["limits"] = [r4(lo.x), r4(lo.y), r4(hi.x), r4(hi.y)]
    info["viewports"] = [
        {
            "handle": vp.handle,
            "id": vp.vp_id,
            "overall": vp.overall,
            "status": vp.status,
            "center": [r4(vp.center[0]), r4(vp.center[1])],
            "size": [r4(vp.size[0]), r4(vp.size[1])],
            "view_center": [r4(vp.view_center[0]), r4(vp.view_center[1])],
            "view_center_wcs": _pt(vp.view_center_wcs),
            "target": _pt(vp.target),
            "view_height": r4(vp.view_height),
            "scale": r4(vp.scale),
            "twist": r4(vp.twist),
            "frozen_layers": list(vp.frozen_layers),
        }
        for vp in model.viewports.get(name, [])
    ]
    return info


def _block_stats(
    doc: Drawing, source_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    instances: Counter[str] = Counter()
    for loc in iter_locations(doc, attribs=False):
        if loc.entity.dxftype() == "INSERT":
            instances[loc.entity.dxf.name.lower()] += 1
    blocks: list[dict[str, Any]] = []
    xrefs: list[dict[str, Any]] = []
    for blk in doc.blocks:
        if blk.name.lower().startswith(_LAYOUT_BLOCKS):
            continue
        flags = int(blk.block.dxf.get("flags", 0))
        is_xref = bool(flags & 4)
        entry = {
            "name": blk.name,
            "anonymous": _is_anonymous(blk.name),
            "entities": len(blk),
            "instances": instances.get(blk.name.lower(), 0),
            "attdefs": sum(1 for e in blk if e.dxftype() == "ATTDEF"),
            "xref": is_xref,
        }
        blocks.append(entry)
        if is_xref:
            raw = str(blk.block.dxf.get("xref_path", "")).replace("\\", "/")
            candidate = Path(raw) if raw else None
            resolved = False
            if candidate is not None:
                try:
                    resolved = candidate.is_file() or (source_dir / candidate).is_file()
                except OSError:
                    resolved = False
            xrefs.append(
                {
                    "block": blk.name,
                    "path": str(blk.block.dxf.get("xref_path", "")),
                    "overlay": bool(flags & 8),
                    "resolved_on_disk": resolved,
                    "instances": instances.get(blk.name.lower(), 0),
                }
            )
    return blocks, xrefs


def _bound_xrefs(doc: Drawing) -> list[str]:
    found: set[str] = set()
    for layer in doc.layers:
        m = _BOUND_XREF.match(layer.dxf.name)
        if m:
            found.add(m.group("xref"))
    for blk in doc.blocks:
        m = _BOUND_XREF.match(blk.name)
        if m:
            found.add(m.group("xref"))
    return sorted(found)


def _lock_files(source: Path) -> list[str]:
    stem = source.stem.lower()
    try:
        return sorted(
            p.name
            for p in source.parent.iterdir()
            if p.is_file() and p.suffix.lower() in (".dwl", ".dwl2") and p.stem.lower() == stem
        )
    except OSError:
        return []


def _conventions(doc: Drawing, model: DocModel) -> dict[str, Any]:
    styles = [
        {
            "name": s.dxf.name,
            "font": str(s.dxf.get("font", "")),
            "height": r4(s.dxf.get("height", 0)),
        }
        for s in doc.styles
    ]
    heights: Counter[float] = Counter()
    per_layout_text: Counter[str] = Counter()
    paper_inserts: Counter[str] = Counter()
    for loc in iter_locations(doc, attribs=False):
        kind = loc.entity.dxftype()
        if kind in ("TEXT", "MTEXT"):
            h = loc.entity.dxf.get("height" if kind == "TEXT" else "char_height", 0.0)
            heights[r4(h)] += 1
            if loc.space == "paper" and loc.layout:
                per_layout_text[loc.layout] += 1
        elif kind == "INSERT" and loc.space == "paper":
            paper_inserts[loc.entity.dxf.name] += 1

    def prefixes(names: Iterable[str]) -> list[dict[str, Any]]:
        counts = Counter(re.split(r"[-_$ ]", n, maxsplit=1)[0] for n in names if n)
        return [{"prefix": k, "count": v} for k, v in counts.most_common(10)]

    layer_names = [layer.dxf.name for layer in doc.layers]
    block_names = [b.name for b in doc.blocks if not b.name.startswith("*")]
    title_candidates = [
        {"block": name, "paper_space_inserts": n}
        for name, n in paper_inserts.most_common(5)
        if n >= max(1, len(model.paper_names) // 2)
    ]
    return {
        "text_styles": styles,
        "text_heights": [{"height": h, "count": n} for h, n in heights.most_common(10)],
        "title_block_guess": {
            "repeated_paper_space_blocks": title_candidates,
            "layout_with_most_text": per_layout_text.most_common(1)[0][0]
            if per_layout_text
            else None,
        },
        "layer_prefixes": prefixes(layer_names),
        "block_prefixes": prefixes(block_names),
        "layout_names": model.paper_names,
    }


def _font_report(fonts: list[str], source_dir: Path) -> dict[str, Any]:
    """SHX fonts used by text styles; ``found`` is true when the file is beside the drawing or in
    ezdxf's support folders, otherwise null (the CAD installation's folders are not searched)."""
    folders = [source_dir, *(Path(d) for d in ezdxf.options.support_dirs)]
    shx = []
    for name in (f for f in fonts if f.lower().endswith(".shx")):
        found = any((folder / name).is_file() for folder in folders)
        shx.append({"name": name, "found": True if found else None})
    return {
        "shx": shx,
        "other": [f for f in fonts if not f.lower().endswith(".shx")],
        "note": "found=null means not beside the drawing or in support folders; "
        "the CAD installation is not searched",
    }


def build_info(loaded: Loaded, conventions: bool) -> dict[str, Any]:
    doc = loaded.doc
    model = DocModel(doc)
    blocks, xrefs = _block_stats(doc, loaded.source.parent)
    layers = [
        {
            "name": lay.dxf.name,
            "off": lay.is_off(),
            "frozen": lay.is_frozen(),
            "locked": lay.is_locked(),
            "color": int(lay.dxf.get("color", 7)),
            "plot": bool(lay.dxf.get("plot", 1)),
        }
        for lay in doc.layers
    ]
    fonts = sorted({str(s.dxf.get("font", "")) for s in doc.styles if s.dxf.get("font", "")})
    info: dict[str, Any] = {
        "file": str(loaded.source),
        "dxf_version": doc.dxfversion,
        "units": _header_units(doc),
        "extents": _extents(doc),
        "layouts": [_layout_info(doc, model, n) for n in model.layout_names],
        "layers": layers,
        "blocks": blocks,
        "xrefs": xrefs,
        "bound_xref_prefixes": _bound_xrefs(doc),
        "fonts": _font_report(fonts, loaded.source.parent),
        "lock_files": _lock_files(loaded.source),
        "warnings": loaded.warnings + model.warnings,
    }
    if conventions:
        info["conventions"] = _conventions(doc, model)
    return info


def _run_info(args: argparse.Namespace) -> Result:
    ctx = _new_run("info", args)
    result = Result("info", backend="ezdxf")
    loaded = open_drawing(args.file, ctx)
    try:
        info = build_info(loaded, args.conventions)
    finally:
        loaded.cleanup()
    result.approximate = True if loaded.approximate else None
    path = ctx.path("info.json")
    _write_json(path, info)
    ctx.add_output(result, "info", path, source=loaded.source)
    vps = [vp for lay in info["layouts"] for vp in lay.get("viewports", []) if not vp["overall"]]
    result.summary = {
        "file": loaded.source.name,
        "dxf_version": info["dxf_version"],
        "insunits": info["units"]["insunits_name"],
        "measurement": info["units"]["measurement_name"],
        "layouts": [lay["name"] for lay in info["layouts"] if lay["name"].lower() != "model"],
        "viewports": len(vps),
        "viewports_status_unreliable": sum(1 for vp in vps if vp["status"] <= 0),
        "layers": {
            "count": len(info["layers"]),
            "off": sum(1 for x in info["layers"] if x["off"]),
            "frozen": sum(1 for x in info["layers"] if x["frozen"]),
        },
        "blocks": {
            "count": len(info["blocks"]),
            "anonymous": sum(1 for b in info["blocks"] if b["anonymous"]),
        },
        "xrefs": {
            "count": len(info["xrefs"]),
            "unresolved": sum(1 for x in info["xrefs"] if not x["resolved_on_disk"]),
            "bound": len(info["bound_xref_prefixes"]),
        },
        "lock_files": info["lock_files"],
    }
    for warning in info["warnings"]:
        result.warn(warning)
    for x in info["xrefs"]:
        if not x["resolved_on_disk"]:
            result.warn(f"xref {x['block']!r} not found on disk: {x['path']}")
    if loaded.approximate:
        result.warn("DWG was converted with an approximate converter; some objects may be missing")
    if info["lock_files"]:
        result.warn("lock files beside the source: the drawing may be open in a CAD application")
    return _finish(result, ctx)


def _add_info_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path, help="DXF or DWG file")
    p.add_argument("--conventions", action="store_true", help="add text styles, title block guess")
    _add_common(p)


# --------------------------------------------------------------------------------------
# find
# --------------------------------------------------------------------------------------

_WHERE = ("text", "attrib", "layer", "block")


def _matches(rx: re.Pattern[str], plain: str | None, raw: str | None) -> str | None:
    in_plain = plain is not None and rx.search(plain) is not None
    in_raw = raw is not None and rx.search(raw) is not None
    if in_plain and in_raw:
        return "both"
    return "plain" if in_plain else "raw" if in_raw else None


def find_hits(
    loaded: Loaded,
    rx: re.Pattern[str],
    where: set[str],
    hidden: str,
    deadline: Deadline,
) -> Iterator[dict[str, Any]]:
    """Hits of ``rx`` in one document; see the module docstring for the field meanings."""
    doc = loaded.doc
    model = DocModel(doc)
    name = str(loaded.source)
    for i, loc in enumerate(iter_locations(doc)):
        if i % 2000 == 0:
            deadline.check()
        kind = loc.entity.dxftype()
        if kind in _ATTRIB_TYPES:
            if "attrib" not in where:
                continue
        elif kind in _TEXT_TYPES:
            if "text" not in where:
                continue
        else:
            continue
        plain, raw = entity_text(loc.entity)
        matched = _matches(rx, plain, raw)
        if matched is None:
            continue
        place = model.locate(loc)
        is_hidden = place["visible_in_space"] is False
        if (hidden == "exclude" and is_hidden) or (hidden == "only" and not is_hidden):
            continue
        hit = {
            "file": name,
            "handle": str(loc.entity.dxf.handle),
            "type": kind,
            "space": loc.space,
            "layout": loc.layout,
            "block": loc.block,
            "layer": loc.entity.dxf.layer,
            "text": plain,
            "raw": raw,
            "matched_in": matched,
            "insert": anchor_of(loc.entity),
            **place,
        }
        if loc.parent is not None:
            hit["parent_handle"] = str(loc.parent.dxf.handle)
        yield hit
    table_ok = hidden != "only"
    if "layer" in where and table_ok:
        for layer in doc.layers:
            if rx.search(layer.dxf.name):
                yield _table_hit(name, "LAYER", layer.dxf.handle, layer.dxf.name, layer.dxf.name)
    if "block" in where and table_ok:
        for blk in doc.blocks:
            if blk.name.lower().startswith(_LAYOUT_BLOCKS):
                continue
            if rx.search(blk.name):
                handle = blk.block_record.dxf.handle if blk.block_record is not None else None
                yield _table_hit(name, "BLOCK", handle, blk.name, None)


def _table_hit(file: str, kind: str, handle: Any, text: str, layer: str | None) -> dict[str, Any]:
    return {
        "file": file,
        "handle": str(handle) if handle is not None else None,
        "type": kind,
        "space": "table",
        "layout": None,
        "block": text if kind == "BLOCK" else None,
        "layer": layer,
        "text": text,
        "raw": text,
        "matched_in": kind.lower(),
        "insert": None,
        "visible_in_space": None,
        "prints_on": None,
    }


def _run_find(args: argparse.Namespace) -> Result:
    where = {w.strip() for w in args.where.split(",") if w.strip()}
    if not where or not where <= set(_WHERE):
        raise CadError(
            "BAD_ARGS",
            f"--where takes a subset of {','.join(_WHERE)}",
            exit_code=ExitCode.BAD_ARGS,
        )
    try:
        rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
    except re.error as exc:
        raise CadError(
            "BAD_ARGS", f"invalid regular expression: {exc}", exit_code=ExitCode.BAD_ARGS
        ) from exc
    ctx = _new_run("find", args)
    result = Result("find", backend="ezdxf")
    deadline = Deadline(args.timeout)
    path = ctx.path("find.jsonl")
    by_type: Counter[str] = Counter()
    by_space: Counter[str] = Counter()
    by_file: Counter[str] = Counter()
    total = hidden_n = 0
    truncated = False
    sources: list[Path] = []
    with path.open("w", encoding="utf-8") as out:
        for file in args.files:
            loaded = open_drawing(file, ctx)
            sources.append(loaded.source)
            try:
                for w in loaded.warnings:
                    result.warn(w)
                if loaded.approximate:
                    result.approximate = True
                    result.warn(
                        f"{loaded.source.name}: approximate DWG conversion, hits may be missing"
                    )
                model_warnings = DocModel(loaded.doc).warnings
                for w in model_warnings:
                    result.warn(f"{loaded.source.name}: {w}")
                for hit in find_hits(loaded, rx, where, args.hidden, deadline):
                    if total >= args.limit:
                        truncated = True
                        break
                    total += 1
                    by_type[hit["type"]] += 1
                    by_space[hit["space"]] += 1
                    by_file[Path(hit["file"]).name] += 1
                    hidden_n += hit["visible_in_space"] is False
                    out.write(json.dumps(_jsonable(hit), ensure_ascii=False) + "\n")
            finally:
                loaded.cleanup()
            if truncated:
                break
    if truncated:
        result.warn(f"stopped at --limit {args.limit}; narrow the pattern or raise the limit")
    result.summary = {
        "hits": total,
        "hidden": hidden_n,
        "by_type": dict(by_type),
        "by_space": dict(by_space),
        "by_file": dict(by_file),
        "truncated": truncated,
        "prints_on_basis": "viewport geometry, not a plot",
    }
    ctx.add_output(result, "hits", path)
    if not total:
        result.next.append("no hits: try -i, a looser pattern or --where text,attrib,layer,block")
    return _finish(result, ctx)


def _add_find_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("files", nargs="+", type=Path, help="DXF or DWG files")
    p.add_argument("--pattern", required=True, help="regular expression (Python syntax)")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("--where", default=",".join(_WHERE), help="subset of text,attrib,layer,block")
    p.add_argument("--hidden", choices=("include", "only", "exclude"), default="include")
    p.add_argument("--limit", type=int, default=DEFAULT_FIND_LIMIT, help="maximum hits written")
    _add_common(p)


# --------------------------------------------------------------------------------------
# dump
# --------------------------------------------------------------------------------------


def _overlaps(bbox: list[float] | None, anchor: list[float] | None, win: list[float]) -> bool:
    x1, y1, x2, y2 = (
        min(win[0], win[2]),
        min(win[1], win[3]),
        max(win[0], win[2]),
        max(win[1], win[3]),
    )
    if bbox is not None:
        return not (bbox[2] < x1 or bbox[0] > x2 or bbox[3] < y1 or bbox[1] > y2)
    return anchor is not None and x1 <= anchor[0] <= x2 and y1 <= anchor[1] <= y2


def _space_matches(loc: Loc, space: str) -> bool:
    if space == "all":
        return True
    if space == "model":
        return loc.space == "model"
    if space == "paper":
        return loc.space == "paper"
    return (loc.layout or "").lower() == space.lower() or (loc.block or "").lower() == space.lower()


def _run_dump(args: argparse.Namespace) -> Result:
    ctx = _new_run("dump", args)
    result = Result("dump", backend="ezdxf")
    deadline = Deadline(args.timeout)
    types = {t.upper() for t in args.type or []}
    layers = {x.lower() for x in args.layer or []}
    handles = {h.upper() for h in args.handle or []}
    window = parse_floats(args.window, 4, "--window") if args.window else None
    loaded = open_drawing(args.file, ctx)
    path = ctx.path("dump.jsonl")
    written = 0
    truncated = False
    by_type: Counter[str] = Counter()
    try:
        for w in loaded.warnings:
            result.warn(w)
        result.approximate = True if loaded.approximate else None
        model = DocModel(loaded.doc)
        with path.open("w", encoding="utf-8") as out:
            for i, loc in enumerate(iter_locations(loaded.doc)):
                if i % 2000 == 0:
                    deadline.check()
                e = loc.entity
                if not _space_matches(loc, args.space):
                    continue
                if types and e.dxftype() not in types:
                    continue
                if layers and e.dxf.layer.lower() not in layers:
                    continue
                if handles and str(e.dxf.handle).upper() not in handles:
                    continue
                rec = describe(loc, with_bbox=True)
                if window is not None and not _overlaps(rec.get("bbox"), rec["anchor"], window):
                    continue
                if written >= args.limit:
                    truncated = True
                    break
                rec.update(model.locate(loc))
                out.write(json.dumps(_jsonable(rec), ensure_ascii=False) + "\n")
                written += 1
                by_type[rec["type"]] += 1
    finally:
        loaded.cleanup()
    if truncated:
        result.warn(f"output cut at --limit {args.limit}; add filters or raise the limit")
    result.summary = {"entities": written, "by_type": dict(by_type), "truncated": truncated}
    ctx.add_output(result, "entities", path, source=loaded.source)
    return _finish(result, ctx)


def _add_dump_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path)
    p.add_argument("--space", default="all", help="model | paper | all | layout or block name")
    p.add_argument("--type", action="append", help="entity type, repeatable (e.g. TEXT)")
    p.add_argument("--layer", action="append", help="layer name, repeatable")
    p.add_argument("--window", help="X1,Y1,X2,Y2 in drawing units (bbox overlap)")
    p.add_argument("--handle", action="append", help="entity handle, repeatable")
    p.add_argument("--limit", type=int, default=DEFAULT_DUMP_LIMIT)
    _add_common(p)


# --------------------------------------------------------------------------------------
# fingerprint
# --------------------------------------------------------------------------------------


def build_fingerprint(loaded: Loaded, ctx: RunContext, deadline: Deadline) -> dict[str, Any]:
    from .runs import file_sha1

    doc = loaded.doc
    model = DocModel(doc)
    entities: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for i, loc in enumerate(iter_locations(doc, attribs=False)):
        if i % 2000 == 0:
            deadline.check()
        e = loc.entity
        kind = e.dxftype()
        if kind == "VIEWPORT" and loc.space == "paper":
            vp_info = _viewport_info(e)
            if vp_info.overall or vp_info.vp_id == 1:
                continue
        anchor, _abs, relative, bbox = geometry(e)
        plain, _raw = entity_text(e)
        layer = e.dxf.layer
        shape = _hash([kind, layer, relative])
        rec: dict[str, Any] = {
            "handle": str(e.dxf.handle),
            "type": kind,
            "space": loc.space,
            "scope": loc.layout if loc.space != "block" else loc.block,
            "layer": layer,
            "bbox": bbox,
            "anchor": anchor,
            "text_sha1": hashlib.sha1(plain.encode("utf-8")).hexdigest() if plain else None,
            "props": _abs,
            "shape": shape,
            "sig": _hash([shape, anchor]),
        }
        if kind == "VIEWPORT":
            rec["vp_id"] = _viewport_info(e).vp_id
        entities.append(rec)
        counts[kind] += 1
    layers = {
        lay.dxf.name: {
            "off": lay.is_off(),
            "frozen": lay.is_frozen(),
            "locked": lay.is_locked(),
            "color": int(lay.dxf.get("color", 7)),
            "linetype": str(lay.dxf.get("linetype", "")),
            "plot": bool(lay.dxf.get("plot", 1)),
        }
        for lay in doc.layers
    }
    viewports = [
        {
            "layout": name,
            "id": vp.vp_id,
            "status": vp.status,
            "overall": vp.overall,
            "center": [r4(vp.center[0]), r4(vp.center[1])],
            "size": [r4(vp.size[0]), r4(vp.size[1])],
            "view_center": [r4(vp.view_center[0]), r4(vp.view_center[1])],
            "view_center_wcs": _pt(vp.view_center_wcs),
            "target": _pt(vp.target),
            "view_height": r4(vp.view_height),
            "scale": r4(vp.scale),
            "twist": r4(vp.twist),
            "frozen_layers": sorted(vp.frozen_layers, key=str.lower),
        }
        for name, vps in model.viewports.items()
        for vp in vps
    ]
    return {
        "schema": FINGERPRINT_SCHEMA,
        "source": {
            "name": loaded.source.name,
            "bytes": loaded.source.stat().st_size,
            "sha1": file_sha1(loaded.source),
        },
        "header": {
            **_header_units(doc),
            "handseed": str(doc.header.get("$HANDSEED", "")),
            "dxf_version": doc.dxfversion,
        },
        "layouts": [
            {"name": n, "block": doc.layouts.get(n).block_record_name} for n in model.paper_names
        ],
        "blocks": [b.name for b in doc.blocks if not b.name.lower().startswith(_LAYOUT_BLOCKS)],
        "layers": layers,
        "viewports": viewports,
        "counts": dict(counts),
        "total": len(entities),
        "warnings": loaded.warnings + model.warnings,
        "entities": entities,
    }


def _fingerprint_of(path: Path, ctx: RunContext, deadline: Deadline) -> tuple[dict[str, Any], bool]:
    """Fingerprint from a ``.json`` fingerprint file or from a drawing; second value: approximate."""
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(_resolve_source(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CadError(
                "BAD_FINGERPRINT",
                f"cannot read fingerprint {path.name}: {exc}",
                hint="regenerate it",
            ) from exc
        if not isinstance(data, dict) or data.get("schema") != FINGERPRINT_SCHEMA:
            raise CadError(
                "BAD_FINGERPRINT",
                f"{path.name} is not a fingerprint (schema {FINGERPRINT_SCHEMA})",
                hint="create it with the fingerprint command",
            )
        return data, False
    loaded = open_drawing(path, ctx, use_cache=False)
    try:
        return build_fingerprint(loaded, ctx, deadline), loaded.approximate
    finally:
        loaded.cleanup()


def _run_fingerprint(args: argparse.Namespace) -> Result:
    ctx = _new_run("fingerprint", args)
    result = Result("fingerprint", backend="ezdxf")
    deadline = Deadline(args.timeout)
    src = _resolve_source(args.file)
    loaded = open_drawing(src, ctx, use_cache=False)
    try:
        fp = build_fingerprint(loaded, ctx, deadline)
    finally:
        loaded.cleanup()
    out = ctx.path("fingerprint.json")
    _write_json(out, fp)
    if args.out is not None:
        dest = args.out.expanduser()
        if dest.exists() and not args.overwrite:
            raise CadError(
                "EXISTS",
                f"{dest} exists",
                exit_code=ExitCode.PRECONDITION_FAILED,
                hint="pass --overwrite or choose another --out",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(out.read_bytes())
        out = dest
    result.approximate = True if loaded.approximate else None
    for w in fp["warnings"]:
        result.warn(w)
    if loaded.approximate:
        result.warn("approximate DWG conversion: the fingerprint may lack some objects")
    result.summary = {
        "entities": fp["total"],
        "by_type": fp["counts"],
        "layouts": [x["name"] for x in fp["layouts"]],
        "viewports": sum(1 for v in fp["viewports"] if not v["overall"]),
    }
    ctx.add_output(result, "fingerprint", out, source=loaded.source)
    return _finish(result, ctx)


def _add_fingerprint_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path)
    p.add_argument("--out", type=Path, default=None, help="also copy the fingerprint here")
    p.add_argument("--overwrite", action="store_true")
    _add_common(p)


# --------------------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------------------


def _scope_names(fp: dict[str, Any]) -> dict[str, str]:
    """Block name -> comparison name; anonymous blocks are identified by their content."""
    members: dict[str, list[str]] = defaultdict(list)
    for e in fp["entities"]:
        if e["space"] == "block":
            members[e["scope"]].append(e["sig"])
    out: dict[str, str] = {}
    for name in fp.get("blocks", []):
        out[name] = (
            f"*anon:{_hash(sorted(members.get(name, [])), 10)}" if _is_anonymous(name) else name
        )
    return out


def _prop_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            out.append({"field": key, "old": _brief(old.get(key)), "new": _brief(new.get(key))})
    return out


def _pair_nearest(
    a_items: list[dict[str, Any]], b_items: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair two equally-shaped groups by smallest anchor distance (stable, greedy)."""
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if len(a_items) * len(b_items) > 40000:
        key = lambda e: tuple(e["anchor"] or (0.0, 0.0))
        return list(zip(sorted(a_items, key=key), sorted(b_items, key=key), strict=False))
    cand = []
    for i, a in enumerate(a_items):
        ax, ay = a["anchor"] or (0.0, 0.0)
        for j, b in enumerate(b_items):
            bx, by = b["anchor"] or (0.0, 0.0)
            cand.append(((ax - bx) ** 2 + (ay - by) ** 2, i, j))
    cand.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    for _dist, i, j in cand:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((a_items[i], b_items[j]))
    return pairs


def _group(items: Iterable[dict[str, Any]], key: Any) -> dict[Any, list[dict[str, Any]]]:
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return groups


def _ref(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "handle": e["handle"],
        "type": e["type"],
        "space": e["space"],
        "scope": e["scope"],
        "layer": e["layer"],
        "anchor": e["anchor"],
    }


def _describe_change(c: dict[str, Any]) -> str:
    where = c["scope"] if c["space"] != "model" else "model space"
    base = f"{c['type']} on layer {c['layer']} in {where}"
    kind = c["kind"]
    if kind == "moved":
        v = c["vector"]
        return f"{base} moved by ({v[0]:g}, {v[1]:g}), shape unchanged"
    if kind == "changed":
        parts = [f"{x['field']}: {x['old']!r} -> {x['new']!r}" for x in c["changes"][:3]]
        return f"{base} changed ({'; '.join(parts)})"
    if kind == "removed":
        return f"{base} at {c['anchor']} has no counterpart in B (cause not determined)"
    return f"{base} at {c['anchor']} has no counterpart in A (cause not determined)"


def diff_fingerprints(
    a: dict[str, Any], b: dict[str, Any], *, full: bool = False
) -> dict[str, Any]:
    """Semantic diff of two fingerprints; see the module docstring of the diff command."""
    names_a, names_b = _scope_names(a), _scope_names(b)

    def scoped(
        fp: dict[str, Any], names: dict[str, str]
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for e in fp["entities"]:
            scope = names.get(e["scope"], e["scope"]) if e["space"] == "block" else e["scope"]
            groups[(e["space"], scope)].append(e)
        return groups

    sa, sb = scoped(a, names_a), scoped(b, names_b)
    changes: list[dict[str, Any]] = []
    noise: Counter[str] = Counter()
    matched_pairs = 0

    def note_pair(ea: dict[str, Any], eb: dict[str, Any]) -> None:
        nonlocal matched_pairs
        matched_pairs += 1
        if ea["handle"] != eb["handle"]:
            noise["handles_renumbered"] += 1
        if ea["type"] == "VIEWPORT" and ea.get("vp_id") != eb.get("vp_id"):
            noise["viewport_ids_changed"] += 1

    for scope in sorted(set(sa) | set(sb)):
        left, right = list(sa.get(scope, [])), list(sb.get(scope, []))
        # 1. identical content
        by_sig = _group(right, lambda e: e["sig"])
        rest_a: list[dict[str, Any]] = []
        for ea in left:
            bucket = by_sig.get(ea["sig"])
            if bucket:
                note_pair(ea, bucket.pop(0))
            else:
                rest_a.append(ea)
        rest_b = [e for bucket in by_sig.values() for e in bucket]
        # 2. same shape somewhere else: moved
        groups_b = _group(rest_b, lambda e: (e["type"], e["layer"], e["shape"]))
        still_a: list[dict[str, Any]] = []
        by_shape_a = _group(rest_a, lambda e: (e["type"], e["layer"], e["shape"]))
        paired_b: set[int] = set()
        for key, items_a in by_shape_a.items():
            items_b = groups_b.get(key, [])
            if not items_b or any(e["anchor"] is None for e in items_a + items_b):
                still_a.extend(items_a)
                continue
            pairs = _pair_nearest(items_a, items_b)
            done_a = {id(p[0]) for p in pairs}
            for ea, eb in pairs:
                note_pair(ea, eb)
                vec = [r4(eb["anchor"][0] - ea["anchor"][0]), r4(eb["anchor"][1] - ea["anchor"][1])]
                changes.append(
                    {
                        "kind": "moved",
                        **_ref(ea),
                        "handle_b": eb["handle"],
                        "anchor_b": eb["anchor"],
                        "vector": vec,
                    }
                )
                paired_b.add(id(eb))
            still_a.extend(e for e in items_a if id(e) not in done_a)
        rest_b = [e for e in rest_b if id(e) not in paired_b]
        # 3. same place, different content: changed (first with the same layer, then any)
        for key_fn in (
            lambda e: (e["type"], e["layer"], tuple(e["anchor"] or ())),
            lambda e: (e["type"], tuple(e["anchor"] or ())),
        ):
            groups_b = _group(rest_b, key_fn)
            next_a: list[dict[str, Any]] = []
            used_b: set[int] = set()
            for ea in still_a:
                bucket = groups_b.get(key_fn(ea))
                bucket = [x for x in bucket if id(x) not in used_b] if bucket else []
                if bucket and ea["anchor"] is not None:
                    eb = bucket[0]
                    used_b.add(id(eb))
                    note_pair(ea, eb)
                    diffs = _prop_changes(ea["props"], eb["props"])
                    if ea["layer"] != eb["layer"]:
                        diffs.insert(0, {"field": "layer", "old": ea["layer"], "new": eb["layer"]})
                    changes.append(
                        {"kind": "changed", **_ref(ea), "handle_b": eb["handle"], "changes": diffs}
                    )
                else:
                    next_a.append(ea)
            still_a = next_a
            rest_b = [e for e in rest_b if id(e) not in used_b]
        for ea in still_a:
            changes.append({"kind": "removed", **_ref(ea), "to_confirm": True})
        for eb in rest_b:
            changes.append({"kind": "added", **_ref(eb), "to_confirm": True})

    # anonymous / layout block names that differ although the content matched
    anon_a = {names_a[n]: n for n in a.get("blocks", []) if _is_anonymous(n)}
    anon_b = {names_b[n]: n for n in b.get("blocks", []) if _is_anonymous(n)}
    renamed = sum(1 for key, name in anon_a.items() if key in anon_b and name != anon_b[key])
    if renamed:
        noise["anonymous_blocks_renamed"] = renamed
    layouts_a = {x["name"]: x["block"] for x in a.get("layouts", [])}
    layouts_b = {x["name"]: x["block"] for x in b.get("layouts", [])}
    block_renamed = sum(1 for n, blk in layouts_a.items() if n in layouts_b and layouts_b[n] != blk)
    if block_renamed:
        noise["paper_space_block_names_changed"] = block_renamed
    if a["header"].get("handseed") != b["header"].get("handseed"):
        noise["handseed_changed"] = 1

    structural = _structural_changes(a, b, layouts_a, layouts_b)
    order = {"changed": 0, "moved": 1, "removed": 2, "added": 3}
    changes.sort(key=lambda c: (order[c["kind"]], c["scope"] or "", c["type"], c["handle"]))
    for c in changes:
        c["description"] = _describe_change(c)
    counts = Counter(c["kind"] for c in changes)
    cap = None if full else DIFF_JSON_CAP
    return {
        "counts": {k: counts.get(k, 0) for k in order},
        "structural": structural,
        "noise": dict(noise),
        "matched_entities": matched_pairs,
        "entities_a": a["total"],
        "entities_b": b["total"],
        "changes": changes if cap is None else changes[:cap],
        "truncated": cap is not None and len(changes) > cap,
        "total_changes": len(changes) + len(structural),
    }


def _structural_changes(
    a: dict[str, Any], b: dict[str, Any], layouts_a: dict[str, str], layouts_b: dict[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(kind: str, what: str, name: str, **extra: Any) -> None:
        out.append(
            {
                "kind": kind,
                "what": what,
                "name": name,
                **extra,
                "description": f"{what} {name!r} {kind}",
            }
        )

    for name in sorted(set(layouts_a) - set(layouts_b)):
        add("removed", "layout", name, to_confirm=True)
    for name in sorted(set(layouts_b) - set(layouts_a)):
        add("added", "layout", name, to_confirm=True)
    for name in sorted(set(a["layers"]) - set(b["layers"])):
        add("removed", "layer", name, to_confirm=True)
    for name in sorted(set(b["layers"]) - set(a["layers"])):
        add("added", "layer", name, to_confirm=True)
    for name in sorted(set(a["layers"]) & set(b["layers"])):
        if a["layers"][name] != b["layers"][name]:
            diffs = _prop_changes(a["layers"][name], b["layers"][name])
            add("changed", "layer", name, changes=diffs)
    plain_a = {n for n in a.get("blocks", []) if not n.startswith("*")}
    plain_b = {n for n in b.get("blocks", []) if not n.startswith("*")}
    for name in sorted(plain_a - plain_b):
        add("removed", "block definition", name, to_confirm=True)
    for name in sorted(plain_b - plain_a):
        add("added", "block definition", name, to_confirm=True)
    for key in ("insunits", "measurement"):
        if a["header"].get(key) != b["header"].get(key):
            add(
                "changed",
                "setting",
                key,
                changes=[{"field": key, "old": a["header"].get(key), "new": b["header"].get(key)}],
            )
    return out


def _run_diff(args: argparse.Namespace) -> Result:
    ctx = _new_run("diff", args)
    result = Result("diff", backend="ezdxf")
    deadline = Deadline(args.timeout)
    fp_a, approx_a = _fingerprint_of(args.a, ctx, deadline)
    fp_b, approx_b = _fingerprint_of(args.b, ctx, deadline)
    if approx_a or approx_b:
        result.approximate = True
        result.warn(
            "an input came from an approximate DWG conversion; differences may be artefacts"
        )
    for fp, label in ((fp_a, "A"), (fp_b, "B")):
        for w in fp.get("warnings", []):
            result.warn(f"{label}: {w}")
    report = diff_fingerprints(fp_a, fp_b, full=args.full)
    report["a"], report["b"] = fp_a["source"], fp_b["source"]
    path = ctx.path("diff.json")
    _write_json(path, report)
    ctx.add_output(result, "diff", path)
    all_changes = report["structural"] + report["changes"]
    first = [c["description"] for c in all_changes[:SUMMARY_CHANGES]]
    if report["truncated"]:
        result.warn(f"diff.json lists the first {DIFF_JSON_CAP} changes; use --full for all")
    result.summary = {
        **report["counts"],
        "structural": len(report["structural"]),
        "identical": report["total_changes"] == 0,
        "noise": report["noise"],
        "first_changes": first,
    }
    if report["counts"]["removed"] or report["counts"]["added"]:
        result.next.append(
            "added/removed items are unmatched, not explained: confirm with the author"
        )
    return _finish(result, ctx)


def _add_diff_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("a", type=Path, help="base: drawing or fingerprint .json")
    p.add_argument("b", type=Path, help="changed: drawing or fingerprint .json")
    p.add_argument("--full", action="store_true", help="no cap on the changes listed in diff.json")
    _add_common(p)


COMMANDS = {
    "info": Command(
        help="units, layouts, viewports, layers, blocks, xrefs and lock files of a drawing",
        add_arguments=_add_info_args,
        run=_run_info,
        epilog="example: cad.py info plan.dwg --conventions",
    ),
    "find": Command(
        help="regex search in text, attributes, layer and block names, all spaces",
        add_arguments=_add_find_args,
        run=_run_find,
        epilog="example: cad.py find a.dxf b.dxf --pattern 'fire|exit' -i --hidden include",
    ),
    "dump": Command(
        help="entities as JSONL with filters (space, type, layer, window, handle)",
        add_arguments=_add_dump_args,
        run=_run_dump,
        epilog="example: cad.py dump plan.dxf --space Sheet-A --type TEXT --type MTEXT",
    ),
    "fingerprint": Command(
        help="compact JSON fingerprint of the graphic entities (for diff)",
        add_arguments=_add_fingerprint_args,
        run=_run_fingerprint,
        epilog="example: cad.py fingerprint plan.dwg --out plan.fp.json",
    ),
    "diff": Command(
        help="semantic diff of two drawings or fingerprints, ignoring re-save noise",
        add_arguments=_add_diff_args,
        run=_run_diff,
        epilog="example: cad.py diff v1.fp.json v2.dwg",
    ),
}
