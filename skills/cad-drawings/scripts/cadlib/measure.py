"""measure: lengths and areas of entities, in the unit asked for, with units from $INSUNITS.

Geometry comes from ezdxf paths flattened to a tolerance that scales with the entity, so arcs,
bulges, splines and ellipses are measured, not approximated by their control points. A HATCH is
its outline minus its islands. Totals are given only when a single entity type matched: a hatch
and the outline it fills would otherwise be counted twice.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from ezdxf import path as ezpath
from ezdxf import units as ezunits
from ezdxf.math import Vec2, is_point_in_polygon_2d
from ezdxf.math import area as polygon_area

from .command import Command
from .dxf import _open, _overlaps, _space_matches
from .entities import describe, iter_locations
from .result import CadError, Result
from .util import Deadline, _add_common, _finish, _new_run, _write_json, parse_floats

UNIT_CODES = {"in": 1, "ft": 2, "mm": 4, "cm": 5, "m": 6, "km": 7}
PAPER_UNITS = {0: 1, 1: 4}  # DXF plot_paper_units: 0 = inches, 1 = millimetres
RELATIVE_TOLERANCE = 1e-6  # flattening distance as a fraction of the entity's size
BEZIER_SEGMENTS = 16  # ezdxf's default approximates a quarter circle by one curve: 0.03 % off
BY_LAYER_LIMIT = 10
MEASURABLE = {
    "LINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "SPLINE",
    "LWPOLYLINE",
    "POLYLINE",
    "HATCH",
}


@dataclass(frozen=True)
class Measure:
    length: float | None
    area: float | None
    closed: bool
    note: str = ""


def unit_factor(insunits: int, name: str) -> float:
    """Multiplier from drawing units (an INSUNITS code) to the named unit."""
    code = UNIT_CODES.get(name.lower())
    if code is None:
        raise CadError(
            "BAD_ARGS", f"unknown unit {name!r}", hint="use one of: " + ", ".join(UNIT_CODES)
        )
    return float(ezunits.conversion_factor(insunits, code))


def _area(vertices: list[Any]) -> float:
    """Polygon area, computed relative to the first vertex.

    Survey drawings sit at coordinates around 1e9; the shoelace sum on those absolute values
    cancels away every digit of a small area, so the polygon is moved to the origin first.
    """
    origin = vertices[0]
    return abs(polygon_area([v - origin for v in vertices]))


def _polyline_length(vertices: list[Any], closed: bool) -> float:
    if closed and len(vertices) > 1 and (vertices[0] - vertices[-1]).magnitude > 1e-12:
        vertices = [*vertices, vertices[0]]
    return sum((b - a).magnitude for a, b in pairwise(vertices))


def _flatten(p: Any) -> list[Any]:
    control = [v for v in p.control_vertices()] or [p.start]
    xs = [v.x for v in control]
    ys = [v.y for v in control]
    size = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    return list(p.flattening(size * RELATIVE_TOLERANCE))


def _hatch(entity: Any) -> Measure | None:
    polygons = [_flatten(p) for p in ezpath.from_hatch(entity)]
    polygons = [pg for pg in polygons if len(pg) >= 3]
    if not polygons:
        return None
    area = 0.0
    islands = 0
    for i, poly in enumerate(polygons):
        # even-odd nesting: a boundary inside an odd number of others is a hole
        depth = sum(
            1
            for j, other in enumerate(polygons)
            if j != i and is_point_in_polygon_2d(Vec2(poly[0]), [Vec2(v) for v in other]) >= 0
        )
        size = _area(poly)
        if depth % 2:
            area -= size
            islands += 1
        else:
            area += size
    length = sum(_polyline_length(pg, True) for pg in polygons)
    note = (
        f"{islands} island(s) subtracted; length is the boundary including islands"
        if islands
        else ""
    )
    return Measure(length=length, area=abs(area), closed=True, note=note)


def measure_entity(entity: Any) -> Measure | None:
    """Length and (for closed shapes) area in drawing units; None for unsupported types."""
    kind = entity.dxftype()
    if kind not in MEASURABLE:
        return None
    if kind == "HATCH":
        return _hatch(entity)
    p = ezpath.make_path(entity, segments=BEZIER_SEGMENTS)
    parts = list(p.sub_paths()) if p.has_sub_paths else [p]
    length = 0.0
    area = 0.0
    closed = True
    for part in parts:
        vertices = _flatten(part)
        if len(vertices) < 2:
            continue
        is_closed = bool(part.is_closed)
        length += _polyline_length(vertices, is_closed)
        if is_closed and len(vertices) >= 3:
            area += _area(vertices)
        else:
            closed = False
    note = ""
    extrusion = getattr(entity.dxf, "extrusion", None)
    if extrusion is not None and abs(float(extrusion[2])) < 0.999:
        note = "not parallel to the XY plane: the area is its projection onto XY"
    return Measure(length=length, area=area if closed else None, closed=closed, note=note)


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def _sig(value: float) -> float:
    """Nine significant digits: a fixed number of decimals ruins small values in a big unit."""
    return float(f"{value:.9g}")


def _rounded(record: dict[str, Any]) -> dict[str, Any]:
    return {k: (_sig(v) if isinstance(v, float) else v) for k, v in record.items()}


def _drawing_unit_name(insunits: int) -> str:
    try:
        return str(ezunits.decode(insunits)) if insunits else "unitless"
    except (KeyError, ValueError):
        return "unknown"


def _run_measure(args: argparse.Namespace) -> Result:
    types = {t.upper() for t in args.type or []}
    layers = {x.lower() for x in args.layer or []}
    handles = {h.upper() for h in args.handle or []}
    if not (types or layers or handles):
        raise CadError(
            "BAD_ARGS",
            "give at least one of --layer, --type, --handle",
            hint="measuring everything adds up unrelated shapes; narrow it down first (see dump)",
        )
    window = parse_floats(args.window, 4, "--window") if args.window else None
    ctx = _new_run("measure", args)
    result = Result("measure", backend="ezdxf")
    deadline = Deadline(args.timeout)
    loaded = _open(args.file, args, ctx, result)
    doc = loaded.doc
    insunits = int(doc.header.get("$INSUNITS", 0))
    model_factor: float | None = None
    if insunits == 0 and args.assume_unit:
        model_factor = unit_factor(UNIT_CODES[args.assume_unit.lower()], args.unit)
        result.warn(
            f"$INSUNITS is 0: assuming {args.assume_unit} as stated by --assume-unit; "
            "check this against the drawing before relying on the numbers"
        )
    elif insunits:
        model_factor = unit_factor(insunits, args.unit)
    paper_factors: dict[str, float | None] = {}

    def factor_for(loc: Any) -> float:
        if loc.space == "paper":
            key = loc.layout or ""
            if key not in paper_factors:
                code = PAPER_UNITS.get(int(doc.layouts.get(key).dxf.get("plot_paper_units", 1)))
                paper_factors[key] = None if code is None else unit_factor(code, args.unit)
            value = paper_factors[key]
            if value is None:
                raise CadError("UNSUPPORTED", f"layout {key!r} is measured in pixels")
            return value
        if model_factor is None:
            raise CadError(
                "NO_UNITS",
                "the drawing has no length unit ($INSUNITS is 0)",
                hint="ask the user for the unit, then pass --assume-unit mm|cm|m|in|ft",
            )
        return model_factor

    records: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for i, loc in enumerate(iter_locations(doc, attribs=False)):
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
        if window is not None:
            rec = describe(loc, with_bbox=True)
            if not _overlaps(rec.get("bbox"), rec["anchor"], window):
                continue
        m = measure_entity(e)
        if m is None:
            skipped[e.dxftype()] += 1
            continue
        k = factor_for(loc)
        records.append(
            {
                "handle": str(e.dxf.handle),
                "type": e.dxftype(),
                "layer": e.dxf.layer,
                "space": loc.layout or loc.block or "model",
                "closed": m.closed,
                "length": None if m.length is None else m.length * k,
                "area": None if m.area is None else m.area * k * k,
                "note": m.note,
            }
        )
    path = ctx.path("measurements.json")
    # plain JSON types, no _jsonable: it rounds floats to 4 decimals (made for coordinates)
    _write_json(path, {"unit": args.unit, "entities": [_rounded(r) for r in records]})
    ctx.add_output(result, "measurements", path, source=loaded.source)

    by_type: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "length": 0.0, "area": 0.0, "_area_n": 0}
    )
    by_layer: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "length": 0.0, "area": 0.0}
    )
    for r in records:
        t = by_type[r["type"]]
        t["count"] += 1
        t["length"] += r["length"] or 0.0
        if r["area"] is not None:
            t["area"] += r["area"]
            t["_area_n"] += 1
        lay = by_layer[r["layer"]]
        lay["count"] += 1
        lay["length"] += r["length"] or 0.0
        lay["area"] += r["area"] or 0.0
    type_summary = {}
    for name, t in by_type.items():
        entry: dict[str, Any] = {"count": t["count"], "length": _sig(t["length"])}
        if t["_area_n"]:
            entry["area"] = _sig(t["area"])
        type_summary[name] = entry
    layer_rank = sorted(by_layer.items(), key=lambda kv: -kv[1]["count"])
    result.summary = {
        "unit": args.unit,
        "drawing_units": _drawing_unit_name(insunits)
        if not args.assume_unit or insunits
        else args.assume_unit,
        "count": len(records),
        "skipped": dict(skipped),
        "by_type": type_summary,
        "by_layer": {
            name: {
                "count": v["count"],
                "length": _sig(v["length"]),
                "area": _sig(v["area"]),
            }
            for name, v in layer_rank[:BY_LAYER_LIMIT]
        },
        "layers_not_shown": max(0, len(layer_rank) - BY_LAYER_LIMIT),
    }
    if len(type_summary) == 1:
        only = next(iter(type_summary.values()))
        result.summary["length"] = only["length"]
        if "area" in only:
            result.summary["area"] = only["area"]
    elif len(type_summary) > 1:
        result.warn(
            "several entity types matched, so there is no single total: a hatch and the outline it "
            "fills are the same area counted twice; choose one type with --type"
        )
    if skipped:
        result.warn(
            "not measurable and left out: " + ", ".join(f"{n} x {t}" for t, n in skipped.items())
        )
    if not records:
        result.warn("no measurable entities matched the filters")
    return _finish(result, ctx)


def _add_measure_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path, help="DXF or DWG file")
    p.add_argument("--layer", action="append", help="layer name, repeatable")
    p.add_argument(
        "--type", action="append", help="entity type, repeatable (e.g. HATCH, LWPOLYLINE)"
    )
    p.add_argument("--handle", action="append", help="entity handle, repeatable")
    p.add_argument(
        "--space", default="model", help="model (default) | paper | all | layout or block name"
    )
    p.add_argument("--window", help="X1,Y1,X2,Y2 in drawing units (bbox overlap)")
    p.add_argument(
        "--unit",
        default="m",
        help="result unit: mm, cm, m, km, in, ft (default m; areas are unit squared)",
    )
    p.add_argument("--assume-unit", help="unit to assume when $INSUNITS is 0 (ask the user first)")
    _add_common(p)


COMMANDS = {
    "measure": Command(
        help="lengths and areas of selected entities in a chosen unit (hatch minus islands)",
        add_arguments=_add_measure_args,
        run=_run_measure,
        epilog="example: cad.py measure plan.dxf --layer ROOM --type HATCH --unit m",
    ),
}
