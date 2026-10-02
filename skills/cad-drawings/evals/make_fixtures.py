#!/usr/bin/env python3
"""Generate synthetic CAD test drawings (DXF) plus machine-readable ground truth.

Everything produced here is invented for testing. The output is deterministic: the same
arguments always give byte-identical files (fixed seed, fixed metadata, sequential handles).

Usage:
    python make_fixtures.py --out <dir> [--large]

Outputs (in --out, default: ``fixtures/`` next to this script, which is git-ignored):
    plan_cm_v1.dxf, plan_mm_v1.dxf   same plan in centimetres / millimetres, shifted origin
    sheet_set.dxf                    two layouts, title blocks, viewports, hidden text
    mtext_cases.dxf                  MTEXT with inline codes and with two columns
    blocks_attribs.dxf               block with attributes, anonymous block
    hatch_assoc.dxf                  associative hatch on a closed polyline
    xref_missing.dxf                 external reference to a file that does not exist
    sheet_set_v1.dxf, plan_v2.dxf    base and changed drawing (3 real changes + re-save noise)
    plan_large.dxf                   about 30 000 entities (only with --large)
    truth.json                       ground truth for every file above
    changes.json                     ground truth of the v1 -> v2 comparison
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import units, xref
from ezdxf.document import Drawing
from ezdxf.entitydb import EntityDB
from ezdxf.lldxf import const

SCHEMA_VERSION = 1
SEED = 20260930
DXF_VERSION = "R2018"

UNITS_PER_METRE = {units.CM: 100.0, units.MM: 1000.0, units.M: 1.0}
UNIT_NAMES = {units.CM: "centimetre", units.MM: "millimetre", units.M: "metre"}

LAYER_COLORS = {
    "A-WALL": 7,
    "A-ROOM": 3,
    "A-TEXT": 2,
    "A-FURN": 5,
    "A-HATCH": 1,
    "A-HIDDEN": 8,
    "A-FIRE-ZONE": 1,
    "TB-TEXT": 7,
    "TB-FRAME": 7,
    "VP-FROZEN": 6,
}

# Room outlines in metres (closed polygons). Areas: 19.0, 12.0 and 6.0 square metres.
ROOMS_M: dict[str, list[tuple[float, float]]] = {
    "ROOM A": [(0, 0), (5, 0), (5, 3), (3, 4), (0, 4)],
    "ROOM B": [(5, 0), (8, 0), (8, 4), (5, 4)],
    "ROOM C": [(0, 4.5), (3, 4.5), (3, 6.5), (0, 6.5)],
}

# Origin shift of the millimetre plan relative to the centimetre plan, in metres.
MM_PLAN_OFFSET_M = (12.5, 7.25)

FIELD_ORDER = ("DRAWN", "DATE", "REV", "TITLE")
SHEETS: tuple[dict[str, Any], ...] = (
    {
        "name": "Sheet-A",
        "fields": {
            "DRAWN": "A. Drafter",
            "DATE": "2026-01-15",
            "REV": "A",
            "TITLE": "GROUND FLOOR PLAN",
        },
        "twist": 0.0,
        "freeze": [],
    },
    {
        "name": "Sheet-B",
        "fields": {
            "DRAWN": "B. Drafter",
            "DATE": "2026-01-16",
            "REV": "B",
            "TITLE": "UPPER FLOOR PLAN",
        },
        "twist": 90.0,
        "freeze": ["VP-FROZEN"],
    },
)
PAPER_SIZE = (420.0, 297.0)

MTEXT_FORMATTED_RAW = r"Line one\PTemp 21\U+00B0C {\fArial|b1;bold} then plain\Pend"
MTEXT_FORMATTED_PLAIN = "Line one\nTemp 21°C bold then plain\nend"
MTEXT_COLUMNS = ("first column text", "second column text")

LARGE_COUNTS = {"LINE": 12000, "LWPOLYLINE": 10000, "TEXT": 8000}
LARGE_NEEDLES = 3

DIFF_MOVE_VECTOR = (1500.0, -750.0)
DIFF_TEXT_BEFORE = "STORE"
DIFF_TEXT_AFTER = "ARCHIVE"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


@contextmanager
def _fixed_metadata() -> Iterator[None]:
    """Make ezdxf output reproducible (restores everything on exit).

    1. Constant timestamps and GUIDs via the documented ``write_fixed_meta_data_for_testing``.
    2. ezdxf 1.4.x writes the CLASSES section by iterating a ``set`` of DXF type names, so the
       order of CLASS records depends on PYTHONHASHSEED. Returning a sorted list removes that.
    """
    previous = ezdxf.options.write_fixed_meta_data_for_testing
    original_types_in_use = EntityDB.dxf_types_in_use
    ezdxf.options.write_fixed_meta_data_for_testing = True
    EntityDB.dxf_types_in_use = lambda self: sorted(original_types_in_use(self))  # type: ignore[method-assign,assignment,return-value]
    try:
        yield
    finally:
        EntityDB.dxf_types_in_use = original_types_in_use  # type: ignore[method-assign]
        ezdxf.options.write_fixed_meta_data_for_testing = previous


def _new_doc(unit_code: int) -> Drawing:
    doc = ezdxf.new(DXF_VERSION)
    doc.units = unit_code
    for name, color in LAYER_COLORS.items():
        doc.layers.add(name, color=color)
    return doc


def _save(doc: Drawing, path: Path) -> str:
    """Write the DXF file and return the $HANDSEED stored in it."""
    doc.saveas(path)
    return str(doc.header["$HANDSEED"])


def _pt(p: Any) -> list[float]:
    return [float(p[0]), float(p[1])]


def _shoelace(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


class _Registry:
    """Collects entities under stable logical keys; optionally burns handles (re-save noise)."""

    def __init__(self, doc: Drawing, rng: random.Random | None = None) -> None:
        self.doc = doc
        self.rng = rng
        self.items: dict[str, Any] = {}

    def burn(self, count: int) -> None:
        for _ in range(count):
            self.doc.entitydb.handles.next()

    def add(self, key: str, entity: Any) -> Any:
        if key in self.items:
            raise KeyError(f"duplicate registry key: {key}")
        self.items[key] = entity
        if self.rng is not None:
            self.burn(self.rng.randint(1, 3))
        return entity

    def truth(self) -> dict[str, dict[str, str]]:
        return {key: {"handle": e.dxf.handle, "type": e.dxftype()} for key, e in self.items.items()}


def _draw_rooms(
    msp: Any,
    reg: _Registry,
    unit_code: int,
    offset_units: tuple[float, float],
    with_room_polygons: bool,
) -> list[dict[str, Any]]:
    """Draw walls (wide polylines), room polygons and labels; return room ground truth."""
    k = UNITS_PER_METRE[unit_code]
    dx, dy = offset_units
    rooms: list[dict[str, Any]] = []
    for name, pts_m in ROOMS_M.items():
        tag = name.split()[-1]
        pts = [(x * k + dx, y * k + dy) for x, y in pts_m]
        wall = reg.add(
            f"wall.{tag}",
            msp.add_lwpolyline(
                pts, close=True, dxfattribs={"layer": "A-WALL", "const_width": 0.2 * k}
            ),
        )
        entry: dict[str, Any] = {"name": name, "wall_handle": wall.dxf.handle}
        if with_room_polygons:
            poly = reg.add(
                f"room.{tag}",
                msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "A-ROOM"}),
            )
            area = _shoelace(pts)
            entry.update(
                polygon_handle=poly.dxf.handle,
                vertices_units=[_pt(p) for p in pts],
                area_units2=area,
                area_m2=round(area / (k * k), 9),
            )
        label_at = (min(p[0] for p in pts) + 0.3 * k, min(p[1] for p in pts) + 0.3 * k)
        label = reg.add(
            f"label.{tag}",
            msp.add_text(name, height=0.3 * k, dxfattribs={"layer": "A-TEXT", "insert": label_at}),
        )
        entry["label_handle"] = label.dxf.handle
        rooms.append(entry)
    return rooms


def _make_layouts(doc: Drawing, names: tuple[str, ...], resave: bool) -> list[Any]:
    """Create the paper-space layouts.

    A plain first save uses the default layout block names (``*Paper_Space``,
    ``*Paper_Space0``); a re-save creates new layouts and drops ``Layout1``, which shifts them.
    """
    if resave:
        layouts = [doc.layouts.new(name) for name in names]
        doc.layouts.delete("Layout1")
    else:
        doc.layouts.rename("Layout1", names[0])
        layouts = [doc.layouts.get(names[0])] + [doc.layouts.new(n) for n in names[1:]]
    for layout in layouts:
        layout.page_setup(size=PAPER_SIZE, margins=(10, 10, 10, 10), units="mm", device="None")
    return layouts


def _add_sheet(layout: Any, sheet: dict[str, Any], vp_id: int, reg: _Registry) -> dict[str, Any]:
    """Border, title block (TEXT entities) and one viewport in a paper-space layout."""
    name = sheet["name"]
    reg.add(
        f"{name}.border",
        layout.add_lwpolyline(
            [(10, 10), (410, 10), (410, 287), (10, 287)],
            close=True,
            dxfattribs={"layer": "TB-FRAME"},
        ),
    )
    reg.add(
        f"{name}.titleblock.frame",
        layout.add_lwpolyline(
            [(285, 10), (410, 10), (410, 50), (285, 50)],
            close=True,
            dxfattribs={"layer": "TB-FRAME"},
        ),
    )
    fields: dict[str, dict[str, str]] = {}
    for i, field in enumerate(FIELD_ORDER):
        y = 42.0 - 8.0 * i
        label = reg.add(
            f"{name}.field.{field}.label",
            layout.add_text(
                f"{field}:", height=3.0, dxfattribs={"layer": "TB-TEXT", "insert": (288.0, y)}
            ),
        )
        value = reg.add(
            f"{name}.field.{field}.value",
            layout.add_text(
                sheet["fields"][field],
                height=3.5,
                dxfattribs={"layer": "TB-TEXT", "insert": (315.0, y)},
            ),
        )
        fields[field] = {
            "label_handle": label.dxf.handle,
            "label_text": f"{field}:",
            "value_handle": value.dxf.handle,
            "value": sheet["fields"][field],
        }
    # The file stores the view centre in display coordinates (DCS), which the twist rotates:
    # DCS = R(+twist) * (WCS - target), target = origin. Verified against a real CAD plot.
    wcs_center = (4000.0, 3250.0)
    theta = math.radians(float(sheet["twist"]))
    dcs_center = (
        wcs_center[0] * math.cos(theta) - wcs_center[1] * math.sin(theta),
        wcs_center[0] * math.sin(theta) + wcs_center[1] * math.cos(theta),
    )
    vp = reg.add(
        f"{name}.viewport",
        layout.add_viewport(
            center=(147.5, 148.5),
            size=(275.0, 267.0),
            view_center_point=dcs_center,
            view_height=6675.0,
        ),
    )
    vp.dxf.id = vp_id
    vp.dxf.view_twist_angle = sheet["twist"]
    if sheet["freeze"]:
        vp.frozen_layers = list(sheet["freeze"])
    return {
        "name": name,
        "block_name": layout.block_record_name,
        "title_block": fields,
        "viewports": [
            {
                "handle": vp.dxf.handle,
                "id": vp_id,
                "center": [147.5, 148.5],
                "size": [275.0, 267.0],
                "view_center": [round(dcs_center[0], 6), round(dcs_center[1], 6)],
                "view_center_is_dcs": True,
                "view_center_wcs": list(wcs_center),
                "view_height": 6675.0,
                "twist_deg": float(sheet["twist"]),
                "frozen_layers": list(sheet["freeze"]),
            }
        ],
    }


def _define_door_tag(doc: Drawing) -> Any:
    """Block with two attribute definitions (default text carries no searchable word)."""
    blk = doc.blocks.new("DOOR_TAG")
    blk.add_lwpolyline(
        [(0, 0), (600, 0), (600, 300), (0, 300)], close=True, dxfattribs={"layer": "A-TEXT"}
    )
    blk.add_attdef("MARK", (50, 50), text="-", dxfattribs={"height": 100, "layer": "A-TEXT"})
    blk.add_attdef("NOTE", (50, 180), text="-", dxfattribs={"height": 80, "layer": "A-TEXT"})
    return blk


def _add_tagged_insert(
    layout: Any,
    block: str,
    at: tuple[float, float],
    values: dict[str, str],
    layer: str,
    **attribs: Any,
) -> Any:
    """INSERT with ATTRIB children (``add_auto_blockref`` would wrap it in an anonymous block)."""
    ref = layout.add_blockref(block, at, dxfattribs={"layer": layer, **attribs})
    ref.add_auto_attribs(values)
    return ref


def _define_anonymous_block(doc: Drawing, name: str) -> Any:
    """Anonymous block the way CAD stores it: ``*U<n>`` name and the anonymous flag set."""
    blk = doc.blocks.new(name)
    blk.block.dxf.flags = const.BLK_ANONYMOUS
    blk.add_line((0, 0), (800, 0), dxfattribs={"layer": "A-FURN"})
    blk.add_text("ANON CONTENT", height=100, dxfattribs={"layer": "A-TEXT", "insert": (0, 150)})
    return blk


# --------------------------------------------------------------------------------------
# fixtures 1: plan in cm / mm
# --------------------------------------------------------------------------------------


def build_plan(unit_code: int, offset_m: tuple[float, float]) -> tuple[Drawing, dict[str, Any]]:
    k = UNITS_PER_METRE[unit_code]
    offset_units = (offset_m[0] * k, offset_m[1] * k)
    doc = _new_doc(unit_code)
    reg = _Registry(doc)
    rooms = _draw_rooms(doc.modelspace(), reg, unit_code, offset_units, with_room_polygons=True)
    truth = {
        "insunits": int(unit_code),
        "unit_name": UNIT_NAMES[unit_code],
        "drawing_units_per_metre": k,
        "origin_offset_m": [float(offset_m[0]), float(offset_m[1])],
        "origin_offset_units": [float(offset_units[0]), float(offset_units[1])],
        "rooms": rooms,
    }
    return doc, truth


# --------------------------------------------------------------------------------------
# fixture 2: sheet set with hidden text
# --------------------------------------------------------------------------------------


def _fire_entry(
    key: str,
    entity_handle: str,
    entity_type: str,
    space: str,
    layout: str | None,
    block: str | None,
    layer: str,
    text: str,
    visible: bool | None,
    reason: str,
    parent_handle: str | None = None,
) -> dict[str, Any]:
    return {
        "id": key,
        "handle": entity_handle,
        "type": entity_type,
        "space": space,
        "layout": layout,
        "block": block,
        "layer": layer,
        "parent_handle": parent_handle,
        "text": text,
        "visible": visible,
        "reason": reason,
    }


def build_sheet_set() -> tuple[Drawing, dict[str, Any]]:
    doc = _new_doc(units.MM)
    reg = _Registry(doc)
    msp = doc.modelspace()
    doc.layers.get("A-HIDDEN").freeze()
    rooms = _draw_rooms(msp, reg, units.MM, (0.0, 0.0), with_room_polygons=True)

    # Layer that is frozen in one viewport only: entities on it stay in model space.
    vp_circle = msp.add_circle((6500, 5500), 400, dxfattribs={"layer": "VP-FROZEN"})
    vp_text = msp.add_text(
        "LEGEND ITEM", height=200, dxfattribs={"layer": "VP-FROZEN", "insert": (6000, 6100)}
    )

    occurrences: list[dict[str, Any]] = []

    # (a) TEXT on a layer that is frozen globally
    hidden = msp.add_text(
        "FIRE DOOR SCHEDULE",
        height=200,
        dxfattribs={"layer": "A-HIDDEN", "insert": (500, 7000)},
    )
    occurrences.append(
        _fire_entry(
            "text_on_frozen_layer",
            hidden.dxf.handle,
            "TEXT",
            "model",
            "Model",
            None,
            "A-HIDDEN",
            "FIRE DOOR SCHEDULE",
            False,
            "layer A-HIDDEN is frozen globally",
        )
    )

    # (b) ATTRIB value of a block reference
    _define_door_tag(doc)
    door_1 = _add_tagged_insert(
        msp, "DOOR_TAG", (2000, 3500), {"MARK": "D01", "NOTE": "FIRE RATING 60"}, "A-TEXT"
    )
    _add_tagged_insert(msp, "DOOR_TAG", (4000, 3500), {"MARK": "D02", "NOTE": "STANDARD"}, "A-TEXT")
    note = next(a for a in door_1.attribs if a.dxf.tag == "NOTE")
    occurrences.append(
        _fire_entry(
            "attrib_value",
            note.dxf.handle,
            "ATTRIB",
            "model",
            "Model",
            None,
            note.dxf.layer,
            "FIRE RATING 60",
            True,
            "visible attribute of a block reference",
            parent_handle=door_1.dxf.handle,
        )
    )

    # (c) inside a block definition that is never inserted
    unused = doc.blocks.new("UNUSED_SYMBOL")
    unused_text = unused.add_text(
        "FIRE", height=150, dxfattribs={"layer": "A-TEXT", "insert": (0, 0)}
    )
    unused.add_circle((0, 0), 300, dxfattribs={"layer": "A-TEXT"})
    occurrences.append(
        _fire_entry(
            "text_in_unused_block",
            unused_text.dxf.handle,
            "TEXT",
            "block",
            None,
            "UNUSED_SYMBOL",
            "A-TEXT",
            "FIRE",
            False,
            "block definition is never inserted",
        )
    )

    # (e) layer name (the layer also carries a visible entity)
    fire_layer = doc.layers.get("A-FIRE-ZONE")
    msp.add_lwpolyline(
        [(5200, 200), (7800, 200), (7800, 1200), (5200, 1200)],
        close=True,
        dxfattribs={"layer": "A-FIRE-ZONE"},
    )
    occurrences.append(
        _fire_entry(
            "layer_name",
            fire_layer.dxf.handle,
            "LAYER",
            "table",
            None,
            None,
            "A-FIRE-ZONE",
            "A-FIRE-ZONE",
            None,
            "layer table entry, not a drawing entity",
        )
    )

    # (f) plain visible TEXT in model space
    visible_text = msp.add_text(
        "FIRE EXIT", height=200, dxfattribs={"layer": "A-TEXT", "insert": (1000, -600)}
    )
    occurrences.append(
        _fire_entry(
            "text_model_visible",
            visible_text.dxf.handle,
            "TEXT",
            "model",
            "Model",
            None,
            "A-TEXT",
            "FIRE EXIT",
            True,
            "ordinary visible model-space text",
        )
    )

    # paper-space layouts, title blocks and viewports
    layouts = _make_layouts(doc, tuple(s["name"] for s in SHEETS), resave=False)
    sheets_truth = [
        _add_sheet(layout, sheet, 2, reg) for layout, sheet in zip(layouts, SHEETS, strict=True)
    ]

    # (d) MTEXT in paper space (Sheet-A)
    notes = layouts[0].add_mtext(
        r"FIRE EXIT\PSee general notes",
        dxfattribs={"layer": "TB-TEXT", "insert": (20.0, 30.0), "char_height": 3.5},
    )
    occurrences.append(
        _fire_entry(
            "mtext_paper_space",
            notes.dxf.handle,
            "MTEXT",
            "paper",
            SHEETS[0]["name"],
            None,
            "TB-TEXT",
            r"FIRE EXIT\PSee general notes",
            True,
            "MTEXT in the Sheet-A layout (paper space)",
        )
    )

    # `visible` above means "not hidden in its own space". `prints_on` answers the stricter
    # question "on which sheets does it land on paper": model-space objects must also fall inside
    # the viewport window (view centre +- half extents at the viewport scale, axes swapped when
    # the view is twisted by 90 degrees).
    prints_on = {
        "text_on_frozen_layer": [],
        "text_in_unused_block": [],
        "layer_name": None,
        "mtext_paper_space": [SHEETS[0]["name"]],
    }
    model_points = {"attrib_value": note.dxf.insert, "text_model_visible": visible_text.dxf.insert}
    scale = 6675.0 / 267.0
    half_w, half_h = 275.0 * scale / 2, 267.0 * scale / 2
    for key, point in model_points.items():
        prints_on[key] = []
        for sheet in SHEETS:
            hw, hh = (half_h, half_w) if abs(sheet["twist"]) == 90.0 else (half_w, half_h)
            if abs(point[0] - 4000.0) <= hw and abs(point[1] - 3250.0) <= hh:
                prints_on[key].append(sheet["name"])
    for occ in occurrences:
        occ["prints_on"] = prints_on[occ["id"]]

    truth = {
        "insunits": int(units.MM),
        "rooms": rooms,
        "globally_frozen_layers": ["A-HIDDEN"],
        "viewport_frozen_layer": {
            "layer": "VP-FROZEN",
            "entity_handles": [vp_circle.dxf.handle, vp_text.dxf.handle],
            "visible_in": {"Sheet-A": True, "Sheet-B": False},
        },
        "layouts": sheets_truth,
        "search": {"term": "FIRE", "case_sensitive": True, "occurrences": occurrences},
    }
    return doc, truth


# --------------------------------------------------------------------------------------
# fixtures 3-6: MTEXT, blocks, hatch, xref
# --------------------------------------------------------------------------------------


def build_mtext_cases() -> tuple[Drawing, dict[str, Any]]:
    doc = _new_doc(units.MM)
    msp = doc.modelspace()
    formatted = msp.add_mtext(
        MTEXT_FORMATTED_RAW,
        dxfattribs={"layer": "A-TEXT", "insert": (0, 0), "char_height": 250, "width": 6000},
    )
    columned = msp.add_mtext_static_columns(
        list(MTEXT_COLUMNS),
        width=2000,
        gutter_width=300,
        height=1500,
        dxfattribs={"layer": "A-TEXT", "insert": (0, -3000), "char_height": 250},
    )
    truth = {
        "insunits": int(units.MM),
        "mtext": [
            {
                "id": "inline_codes",
                "handle": formatted.dxf.handle,
                "raw": MTEXT_FORMATTED_RAW,
                "plain_text": MTEXT_FORMATTED_PLAIN,
                "codes": ["\\P", "{\\fArial|b1;bold}", "\\U+00B0"],
                "column_count": 1,
            },
            {
                "id": "two_columns",
                "handle": columned.dxf.handle,
                "raw": "\\N".join(MTEXT_COLUMNS),
                "plain_text": "\n".join(MTEXT_COLUMNS),
                "columns": list(MTEXT_COLUMNS),
                "column_count": 2,
            },
        ],
    }
    return doc, truth


def build_blocks_attribs() -> tuple[Drawing, dict[str, Any]]:
    doc = _new_doc(units.MM)
    msp = doc.modelspace()
    blk = doc.blocks.new("ROOM_TAG")
    blk.add_lwpolyline(
        [(0, 0), (1200, 0), (1200, 600), (0, 600)], close=True, dxfattribs={"layer": "A-TEXT"}
    )
    for tag, y in (("ROOM_NO", 400), ("ROOM_NAME", 220), ("AREA", 50)):
        blk.add_attdef(tag, (60, y), text="-", dxfattribs={"height": 120, "layer": "A-TEXT"})

    values = (
        {"ROOM_NO": "101", "ROOM_NAME": "OFFICE", "AREA": "19.0"},
        {"ROOM_NO": "102", "ROOM_NAME": "STORE", "AREA": "12.0"},
        {"ROOM_NO": "103", "ROOM_NAME": "HALL", "AREA": "6.0"},
        {"ROOM_NO": "104", "ROOM_NAME": "WC", "AREA": "3.5"},
    )
    inserts: list[dict[str, Any]] = []
    for i, vals in enumerate(values):
        at = (2500.0 * i, 0.0)
        attribs = {"rotation": 90.0} if i == 3 else {}
        ref = _add_tagged_insert(msp, "ROOM_TAG", at, vals, "A-TEXT", **attribs)
        inserts.append(
            {
                "handle": ref.dxf.handle,
                "insert": _pt(at),
                "rotation": float(attribs.get("rotation", 0.0)),
                "attribs": {
                    a.dxf.tag: {"handle": a.dxf.handle, "value": a.dxf.text} for a in ref.attribs
                },
            }
        )

    anon = _define_anonymous_block(doc, "*U1")
    anon_ref = msp.add_blockref(anon.name, (12000, 0), dxfattribs={"layer": "A-FURN"})
    anon_text = next(e for e in anon if e.dxftype() == "TEXT")
    truth = {
        "insunits": int(units.MM),
        "block_name": "ROOM_TAG",
        "attdef_tags": ["ROOM_NO", "ROOM_NAME", "AREA"],
        "inserts": inserts,
        "anonymous_blocks": [
            {
                "name": anon.name,
                "flags": int(const.BLK_ANONYMOUS),
                "insert_handle": anon_ref.dxf.handle,
                "content_text_handle": anon_text.dxf.handle,
                "content_text": "ANON CONTENT",
            }
        ],
    }
    return doc, truth


def build_hatch_assoc() -> tuple[Drawing, dict[str, Any]]:
    doc = _new_doc(units.MM)
    msp = doc.modelspace()
    pts = [(0, 0), (6000, 0), (6000, 2000), (3000, 2000), (3000, 5000), (0, 5000)]
    boundary = msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "A-HATCH"})
    hatch = msp.add_hatch(color=1, dxfattribs={"layer": "A-HATCH"})
    hatch.set_pattern_fill("ANSI31", scale=100)
    path = hatch.paths.add_polyline_path(boundary.get_points(format="xyb"), is_closed=True)
    hatch.associate(path, [boundary])
    # decoy: a closed polyline of a different size that carries no hatch
    decoy_pts = [(8000, 0), (9000, 0), (9000, 1000), (8000, 1000)]
    decoy = msp.add_lwpolyline(decoy_pts, close=True, dxfattribs={"layer": "A-ROOM"})
    area = _shoelace([(float(x), float(y)) for x, y in pts])
    truth = {
        "insunits": int(units.MM),
        "unit_name": UNIT_NAMES[units.MM],
        "hatch_handle": hatch.dxf.handle,
        "pattern": "ANSI31",
        "associative": True,
        "boundary_handle": boundary.dxf.handle,
        "boundary_vertices_units": [_pt(p) for p in pts],
        "area_units2": area,
        "area_m2": round(area / (UNITS_PER_METRE[units.MM] ** 2), 9),
        "decoy_polyline": {
            "handle": decoy.dxf.handle,
            "area_units2": _shoelace([(float(x), float(y)) for x, y in decoy_pts]),
            "area_m2": 1.0,
        },
    }
    return doc, truth


def build_xref_missing() -> tuple[Drawing, dict[str, Any]]:
    doc = _new_doc(units.MM)
    msp = doc.modelspace()
    msp.add_text("HOST DRAWING", height=300, dxfattribs={"layer": "A-TEXT", "insert": (0, -800)})
    msp.add_lwpolyline(
        [(0, 0), (4000, 0), (4000, 3000), (0, 3000)], close=True, dxfattribs={"layer": "A-WALL"}
    )
    ref = xref.attach(doc, block_name="REF_SITE", filename="missing_ref.dxf", insert=(5000, 0))
    truth = {
        "insunits": int(units.MM),
        "xrefs": [
            {
                "block_name": "REF_SITE",
                "path": "missing_ref.dxf",
                "file_exists": False,
                "overlay": False,
                "insert_handle": ref.dxf.handle,
            }
        ],
    }
    return doc, truth


# --------------------------------------------------------------------------------------
# fixture 7: v1 / v2 pair (3 real changes + re-save noise)
# --------------------------------------------------------------------------------------


def build_diff_doc(resave: bool, changed: bool) -> tuple[Drawing, dict[str, Any]]:
    """Build the base drawing (v1) or its re-saved, changed counterpart (v2).

    v2 is not derived by editing the v1 file: every entity is re-created, so all handles
    differ (by a non-constant offset), which is what a re-save in a CAD program looks like.
    """
    doc = _new_doc(units.MM)
    rng = random.Random(SEED + 1) if resave else None
    reg = _Registry(doc, rng)
    if resave:
        reg.burn(41)
    msp = doc.modelspace()
    _draw_rooms(msp, reg, units.MM, (0.0, 0.0), with_room_polygons=False)
    # real change 1: text value of the room C label (v1 -> v2)
    if changed:
        reg.items["label.C"].dxf.text = DIFF_TEXT_AFTER
    else:
        reg.items["label.C"].dxf.text = DIFF_TEXT_BEFORE

    if not changed:
        reg.add(
            "marker.circle",
            msp.add_circle((6500, 1000), 300, dxfattribs={"layer": "A-FURN"}),
        )  # real change 2: deleted in v2

    dx, dy = DIFF_MOVE_VECTOR if changed else (0.0, 0.0)
    reg.add(
        "desk.rect",
        msp.add_lwpolyline(
            [
                (1000 + dx, 1000 + dy),
                (2200 + dx, 1000 + dy),
                (2200 + dx, 1600 + dy),
                (1000 + dx, 1600 + dy),
            ],
            close=True,
            dxfattribs={"layer": "A-FURN"},
        ),
    )  # real change 3: moved in v2

    door_tag = _define_door_tag(doc)
    for i, e in enumerate(door_tag):
        reg.add(f"blockdef.DOOR_TAG.{i}", e)
    anon_name = "*U7" if resave else "*U1"
    anon = _define_anonymous_block(doc, anon_name)
    for i, e in enumerate(anon):
        reg.add(f"blockdef.anon.{i}", e)

    reg.add("anon.insert", msp.add_blockref(anon.name, (9000, 500), dxfattribs={"layer": "A-FURN"}))
    for n, (at, vals) in enumerate(
        (
            ((2000, 3500), {"MARK": "D01", "NOTE": "STANDARD"}),
            ((4000, 3500), {"MARK": "D02", "NOTE": "SELF-CLOSING"}),
        ),
        start=1,
    ):
        ref = reg.add(f"tag.{n}", _add_tagged_insert(msp, "DOOR_TAG", at, vals, "A-TEXT"))
        for a in ref.attribs:
            reg.add(f"tag.{n}.attrib.{a.dxf.tag}", a)

    layouts = _make_layouts(doc, tuple(s["name"] for s in SHEETS), resave=resave)
    vp_id = 7 if resave else 2
    sheets = [
        _add_sheet(layout, sheet, vp_id, reg) for layout, sheet in zip(layouts, SHEETS, strict=True)
    ]
    info = {
        "registry": reg.truth(),
        "anonymous_block": anon_name,
        "sheets": {
            s["name"]: {"block_name": s["block_name"], "viewport_id": vp_id} for s in sheets
        },
    }
    return doc, info


def diff_truth(
    v1: dict[str, Any], v2: dict[str, Any], handseed_v1: str, handseed_v2: str
) -> dict[str, Any]:
    reg1, reg2 = v1["registry"], v2["registry"]
    deleted = sorted(set(reg1) - set(reg2))
    added = sorted(set(reg2) - set(reg1))
    if added or deleted != ["marker.circle"]:
        raise RuntimeError(f"unexpected key difference: deleted={deleted} added={added}")
    changes = [
        {
            "id": "C1",
            "kind": "text_changed",
            "type": "TEXT",
            "v1_handle": reg1["label.C"]["handle"],
            "v2_handle": reg2["label.C"]["handle"],
            "before": DIFF_TEXT_BEFORE,
            "after": DIFF_TEXT_AFTER,
            "description": f"TEXT value changed from '{DIFF_TEXT_BEFORE}' to '{DIFF_TEXT_AFTER}'",
        },
        {
            "id": "C2",
            "kind": "entity_deleted",
            "type": "CIRCLE",
            "v1_handle": reg1["marker.circle"]["handle"],
            "v2_handle": None,
            "description": "CIRCLE (center 6500,1000 radius 300, layer A-FURN) removed",
        },
        {
            "id": "C3",
            "kind": "entity_moved",
            "type": "LWPOLYLINE",
            "v1_handle": reg1["desk.rect"]["handle"],
            "v2_handle": reg2["desk.rect"]["handle"],
            "vector": [DIFF_MOVE_VECTOR[0], DIFF_MOVE_VECTOR[1]],
            "description": "closed LWPOLYLINE on A-FURN moved by the vector, shape unchanged",
        },
    ]
    entity_map = [
        {
            "key": key,
            "type": reg1[key]["type"],
            "v1_handle": reg1[key]["handle"],
            "v2_handle": reg2[key]["handle"],
        }
        for key in reg1
        if key in reg2
    ]
    noise = [
        {
            "kind": "handles_regenerated",
            "description": "every entity was re-created; handles differ, offset is not constant",
        },
        {
            "kind": "anonymous_block_renamed",
            "v1": v1["anonymous_block"],
            "v2": v2["anonymous_block"],
        },
        {
            "kind": "paper_space_block_names",
            "v1": {n: s["block_name"] for n, s in v1["sheets"].items()},
            "v2": {n: s["block_name"] for n, s in v2["sheets"].items()},
        },
        {
            "kind": "viewport_ids_regenerated",
            "v1": {n: s["viewport_id"] for n, s in v1["sheets"].items()},
            "v2": {n: s["viewport_id"] for n, s in v2["sheets"].items()},
        },
        {"kind": "handseed_changed", "v1": handseed_v1, "v2": handseed_v2},
    ]
    return {
        "base_file": "sheet_set_v1.dxf",
        "changed_file": "plan_v2.dxf",
        "real_changes": changes,
        "noise": noise,
        "entity_map": entity_map,
        "v1_entities": reg1,
        "v2_entities": reg2,
    }


# --------------------------------------------------------------------------------------
# fixture 8: large drawing
# --------------------------------------------------------------------------------------


def build_large() -> tuple[Drawing, dict[str, Any]]:
    rng = random.Random(SEED)
    doc = _new_doc(units.MM)
    msp = doc.modelspace()
    layers = ("A-WALL", "A-FURN", "A-TEXT", "A-ROOM")
    lo = [float("inf")] * 2
    hi = [float("-inf")] * 2

    def track(x: float, y: float) -> None:
        lo[0], lo[1] = min(lo[0], x), min(lo[1], y)
        hi[0], hi[1] = max(hi[0], x), max(hi[1], y)

    def coord() -> float:
        return float(rng.randrange(0, 200_000))

    for _ in range(LARGE_COUNTS["LINE"]):
        x1, y1, x2, y2 = coord(), coord(), coord(), coord()
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": rng.choice(layers)})
        track(x1, y1)
        track(x2, y2)
    for _ in range(LARGE_COUNTS["LWPOLYLINE"]):
        pts = [(coord(), coord()) for _ in range(rng.randint(3, 6))]
        msp.add_lwpolyline(pts, close=bool(rng.getrandbits(1)), dxfattribs={"layer": "A-WALL"})
        for x, y in pts:
            track(x, y)
    needle_slots = sorted(rng.sample(range(LARGE_COUNTS["TEXT"]), LARGE_NEEDLES))
    needles: list[dict[str, Any]] = []
    for i in range(LARGE_COUNTS["TEXT"]):
        x, y = coord(), coord()
        if i in needle_slots:
            content = f"NEEDLE-{len(needles) + 1:02d}"
        else:
            content = f"T{i:05d}"
        t = msp.add_text(content, height=200.0, dxfattribs={"layer": "A-TEXT", "insert": (x, y)})
        track(x, y)
        if i in needle_slots:
            needles.append({"handle": t.dxf.handle, "text": content, "insert": [x, y]})
    truth = {
        "insunits": int(units.MM),
        "counts": {**LARGE_COUNTS, "total": sum(LARGE_COUNTS.values())},
        "needles": needles,
        "extents_min": lo,
        "extents_max": hi,
        "seed": SEED,
    }
    return doc, truth


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------


def _dump_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def generate(out: Path, large: bool = False) -> dict[str, Any]:
    """Write all fixtures and truth files into ``out``; return the truth dictionary."""
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}

    with _fixed_metadata():
        for name, unit_code, offset in (
            ("plan_cm_v1.dxf", units.CM, (0.0, 0.0)),
            ("plan_mm_v1.dxf", units.MM, MM_PLAN_OFFSET_M),
        ):
            doc, truth = build_plan(unit_code, offset)
            _save(doc, out / name)
            files[name] = truth

        for name, builder in (
            ("sheet_set.dxf", build_sheet_set),
            ("mtext_cases.dxf", build_mtext_cases),
            ("blocks_attribs.dxf", build_blocks_attribs),
            ("hatch_assoc.dxf", build_hatch_assoc),
            ("xref_missing.dxf", build_xref_missing),
        ):
            doc, truth = builder()
            _save(doc, out / name)
            files[name] = truth

        doc1, info1 = build_diff_doc(resave=False, changed=False)
        seed1 = _save(doc1, out / "sheet_set_v1.dxf")
        doc2, info2 = build_diff_doc(resave=True, changed=True)
        seed2 = _save(doc2, out / "plan_v2.dxf")
        changes = diff_truth(info1, info2, seed1, seed2)
        files["sheet_set_v1.dxf"] = {
            "insunits": int(units.MM),
            "role": "diff_base",
            "entities": info1["registry"],
        }
        files["plan_v2.dxf"] = {
            "insunits": int(units.MM),
            "role": "diff_changed",
            "changes": changes,
        }

        if large:
            doc, truth = build_large()
            _save(doc, out / "plan_large.dxf")
            files["plan_large.dxf"] = truth

    truth_all = {"schema_version": SCHEMA_VERSION, "generator": "make_fixtures.py", "files": files}
    _dump_json(out / "truth.json", truth_all)
    _dump_json(out / "changes.json", changes)
    return truth_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic CAD fixtures (DXF).")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "fixtures",
        help="output directory (default: fixtures/ next to this script)",
    )
    parser.add_argument("--large", action="store_true", help="also write plan_large.dxf")
    args = parser.parse_args(argv)
    truth = generate(args.out, large=args.large)
    for name in truth["files"]:
        print(args.out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
