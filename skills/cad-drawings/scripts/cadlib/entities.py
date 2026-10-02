"""Walking all spaces and block definitions, and describing entities as JSON."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import ezdxf
import ezdxf.bbox
import ezdxf.units
from ezdxf.math import Vec3
from ezdxf.tools import text as dxf_text

if TYPE_CHECKING:
    from ezdxf.document import Drawing

from .util import _LAYOUT_BLOCKS, _hash, _norm_block_name, _pt, r4
from .viewports import _viewport_info

_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION", "MULTILEADER", "MLEADER"})


_ATTRIB_TYPES = frozenset({"ATTRIB", "ATTDEF"})


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
        if anchor is None and cps:
            anchor = _pt(cps[0])
            a = anchor
        absolute["control_points"] = [_pt(p, 3) for p in cps]
        relative["control_points"] = [_rel(p, a) for p in cps]
        both("degree", int(entity.dxf.degree))
        fits = [Vec3(p) for p in entity.fit_points]
        absolute["fit_points"] = [_pt(p, 3) for p in fits]
        relative["fit_points"] = [_rel(p, a) for p in fits]
        both("knots", [r4(k) for k in entity.knots])
        both("weights", [r4(w) for w in entity.weights])
        bbox = _points_bbox(cps + fits)
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
        paths = _interior_paths(entity, kind)
        if bbox is not None:
            anchor = [bbox[0], bbox[1]]
            relative["size"] = [r4(bbox[2] - bbox[0]), r4(bbox[3] - bbox[1])]
            absolute["bbox_size"] = relative["size"]
        elif paths and paths[0]:
            anchor = [r4(paths[0][0][0]), r4(paths[0][0][1])]
        if paths and anchor is not None:
            # interior geometry: equal bounding boxes must not hide a changed boundary or leader
            absolute["interior"] = _hash(paths)
            relative["interior"] = _hash(
                [[[r4(x - anchor[0]), r4(y - anchor[1])] for x, y in path] for path in paths]
            )
    return anchor, absolute, relative, bbox


def _interior_paths(entity: Any, kind: str) -> list[list[tuple[float, float]]]:
    """Rounded vertex data of hatch/mpolygon boundaries and of leader lines (best effort)."""
    out: list[list[tuple[float, float]]] = []

    def rounded(points: Iterable[Any]) -> list[tuple[float, float]]:
        return [(r4(p[0]), r4(p[1])) for p in points]

    try:
        if kind in ("HATCH", "MPOLYGON"):
            for path in entity.paths:
                if hasattr(path, "vertices"):
                    out.append(rounded(path.vertices))
                    continue
                pts: list[tuple[float, float]] = []
                for edge in getattr(path, "edges", []):
                    for attr in (
                        "start",
                        "end",
                        "center",
                        "major_axis",
                        "control_points",
                        "fit_points",
                    ):
                        value = getattr(edge, attr, None)
                        if value is None:
                            continue
                        if attr in ("control_points", "fit_points"):
                            pts += rounded(value)
                        else:
                            pts += rounded([value])
                    for attr in ("radius", "ratio", "start_angle", "end_angle"):
                        value = getattr(edge, attr, None)
                        if value is not None:
                            pts.append((r4(value), 0.0))
                out.append(pts)
        elif kind == "LEADER":
            out.append(rounded(entity.vertices))
        elif kind in ("MULTILEADER", "MLEADER"):
            for leader in entity.context.leaders:
                for line in leader.lines:
                    out.append(rounded(line.vertices))
    except (AttributeError, TypeError, ValueError, IndexError):
        return out
    return out


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


def expect_for(loc: Loc) -> dict[str, Any] | None:
    """Precondition block shaped for ``assets/edit-spec.schema.json`` (``expect``).

    ``space`` is ``model``, the layout name, or ``block:<name>``; ``text`` is the raw string
    (``text_is_plain`` false); INSERT carries the current attribute values.
    """
    entity = loc.entity
    space = (
        "model"
        if loc.space == "model"
        else (loc.layout or "")
        if loc.space == "paper"
        else f"block:{loc.block}"
    )
    out: dict[str, Any] = {
        "type": entity.dxftype(),
        "layer": str(entity.dxf.layer),
        "space": space,
    }
    _plain, raw = entity_text(entity)
    if raw is not None:
        out["text"] = raw
        out["text_is_plain"] = False
    if entity.dxftype() == "INSERT":
        out["attrib"] = {str(a.dxf.tag): str(a.dxf.get("text", "")) for a in entity.attribs}
    if entity.dxf.hasattr("insert"):
        out["insert"] = _pt(entity.dxf.insert, 3)
    return out
