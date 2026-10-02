"""Verify every ground-truth claim of the synthetic fixtures by re-reading the written files.

The fixtures are regenerated into a temporary directory. Checks deliberately use their own
small helpers (shoelace area, MTEXT decoding, text and visibility scan) instead of the
generator's code, so a bug shared by generator and truth file cannot hide.

The ``--large`` variant is marked ``slow`` and skipped unless CAD_DRAWINGS_RUN_SLOW=1.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf
import pytest
from ezdxf.lldxf import const
from ezdxf.tools.text import plain_mtext

SKILL_DIR = Path(__file__).resolve().parents[1]
MAKE_FIXTURES = SKILL_DIR / "evals" / "make_fixtures.py"
CHECK_FORBIDDEN = SKILL_DIR / "tests" / "check_forbidden.py"

RUN_SLOW = os.environ.get("CAD_DRAWINGS_RUN_SLOW") == "1"

EXPECTED_FILES = {
    "plan_cm_v1.dxf",
    "plan_mm_v1.dxf",
    "sheet_set.dxf",
    "mtext_cases.dxf",
    "blocks_attribs.dxf",
    "hatch_assoc.dxf",
    "xref_missing.dxf",
    "sheet_set_v1.dxf",
    "plan_v2.dxf",
}
UNITS_PER_METRE = {4: 1000.0, 5: 100.0, 6: 1.0}  # $INSUNITS code -> drawing units per metre


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module to be registered
    spec.loader.exec_module(module)
    return module


make_fixtures = _load_module("cad_make_fixtures", MAKE_FIXTURES)
check_forbidden = _load_module("cad_check_forbidden", CHECK_FORBIDDEN)


# --------------------------------------------------------------------------------------
# independent helpers
# --------------------------------------------------------------------------------------


def shoelace(points: list[tuple[float, float]]) -> float:
    twice = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        twice += x1 * y2 - x2 * y1
    return abs(twice) / 2.0


def inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Ray casting point-in-polygon test."""
    x, y = point
    result = False
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            result = not result
    return result


def xy_points(polyline: Any) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in polyline.get_points("xy")]


def decode_mtext(raw: str) -> str:
    """Plain text of the MTEXT codes used by the fixtures (not a general MTEXT parser)."""
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), raw)
    text = re.sub(r"\{\\f[^;]*;", "", text)
    text = text.replace("}", "")
    return text.replace("\\P", "\n").replace("\\N", "\n")


def text_of(entity: Any) -> str | None:
    kind = entity.dxftype()
    if kind in ("TEXT", "ATTRIB", "ATTDEF"):
        return str(entity.dxf.text)
    if kind == "MTEXT":
        return str(entity.text)
    return None


@dataclass
class Loc:
    space: str  # model | paper | block
    layout: str | None
    block: str | None
    entity: Any
    parent: Any | None


def iter_all_entities(doc: Any) -> Iterator[Loc]:
    """Every entity in model space, all paper layouts and all block definitions, plus ATTRIBs."""

    def expand(entity: Any, space: str, layout: str | None, block: str | None) -> Iterator[Loc]:
        yield Loc(space, layout, block, entity, None)
        if entity.dxftype() == "INSERT":
            for attrib in entity.attribs:
                yield Loc(space, layout, block, attrib, entity)

    for entity in doc.modelspace():
        yield from expand(entity, "model", "Model", None)
    for name in doc.layouts.names_in_taborder()[1:]:
        for entity in doc.layouts.get(name):
            yield from expand(entity, "paper", name, None)
    for blk in doc.blocks:
        if blk.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        for entity in blk:
            yield from expand(entity, "block", None, blk.name)


def entity_by_handle(doc: Any, handle: str) -> Any:
    entity = doc.entitydb.get(handle)
    assert entity is not None, f"handle {handle} not found in file"
    return entity


def rnd(values: Any) -> tuple[float, ...]:
    return tuple(round(float(v), 6) for v in values)


def canon(entity: Any) -> tuple[Any, ...]:
    """Handle-independent content of an entity (used to compare v1 with v2)."""
    kind, layer = entity.dxftype(), entity.dxf.layer
    if kind == "TEXT":
        return (kind, layer, rnd(entity.dxf.insert), entity.dxf.text, round(entity.dxf.height, 6))
    if kind == "LWPOLYLINE":
        pts = tuple(rnd(p) for p in entity.get_points("xy"))
        return (kind, layer, pts, bool(entity.closed), round(entity.dxf.const_width, 6))
    if kind == "CIRCLE":
        return (kind, layer, rnd(entity.dxf.center), round(entity.dxf.radius, 6))
    if kind == "LINE":
        return (kind, layer, rnd(entity.dxf.start), rnd(entity.dxf.end))
    if kind == "INSERT":
        name = "*U" if entity.dxf.name.startswith("*U") else entity.dxf.name
        return (kind, layer, name, rnd(entity.dxf.insert), round(entity.dxf.rotation, 6))
    if kind in ("ATTRIB", "ATTDEF"):
        return (kind, layer, entity.dxf.tag, entity.dxf.text, rnd(entity.dxf.insert))
    if kind == "VIEWPORT":
        return (
            kind,
            layer,
            rnd(entity.dxf.center),
            rnd((entity.dxf.width, entity.dxf.height)),
            rnd(entity.dxf.view_center_point),
            round(entity.dxf.view_height, 6),
            round(entity.dxf.view_twist_angle, 6),
            tuple(sorted(entity.frozen_layers)),
        )
    return (kind, layer)


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def out_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("fixtures")
    make_fixtures.generate(directory)
    return directory


@pytest.fixture(scope="module")
def truth(out_dir: Path) -> dict[str, Any]:
    data = json.loads((out_dir / "truth.json").read_text(encoding="utf-8"))
    return data["files"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def load(out_dir: Path) -> Any:
    cache: dict[str, Any] = {}

    def _load(name: str) -> Any:
        if name not in cache:
            cache[name] = ezdxf.readfile(out_dir / name)
        return cache[name]

    return _load


# --------------------------------------------------------------------------------------
# generator contract
# --------------------------------------------------------------------------------------


def test_expected_files_and_truth_schema(out_dir: Path, truth: dict[str, Any]) -> None:
    written = {p.name for p in out_dir.iterdir()}
    assert written == EXPECTED_FILES | {"truth.json", "changes.json"}  # no plan_large by default
    assert set(truth) == EXPECTED_FILES
    full = json.loads((out_dir / "truth.json").read_text(encoding="utf-8"))
    assert full["schema_version"] == 1
    assert (
        json.loads((out_dir / "changes.json").read_text(encoding="utf-8"))
        == (truth["plan_v2.dxf"]["changes"])
    )


def test_all_files_are_dxf_r2018_and_readable(out_dir: Path) -> None:
    for name in sorted(EXPECTED_FILES):
        doc = ezdxf.readfile(out_dir / name)
        assert doc.dxfversion == "AC1032", name
        assert doc.header["$INSUNITS"] in (4, 5), name


def test_output_is_byte_identical_between_runs(out_dir: Path, tmp_path: Path) -> None:
    second = tmp_path / "again"
    make_fixtures.generate(second)
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == sorted(p.name for p in second.iterdir())
    for name in names:
        assert (out_dir / name).read_bytes() == (second / name).read_bytes(), name


def test_output_does_not_depend_on_hash_seed(tmp_path: Path) -> None:
    """ezdxf iterates a set when writing CLASS records; the generator must neutralise that."""
    outputs: list[Path] = []
    for seed in ("0", "4"):
        target = tmp_path / f"seed{seed}"
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
        subprocess.run(
            [sys.executable, str(MAKE_FIXTURES), "--out", str(target)],
            check=True,
            capture_output=True,
            env=env,
        )
        outputs.append(target)
    for name in sorted(EXPECTED_FILES) + ["truth.json", "changes.json"]:
        assert (outputs[0] / name).read_bytes() == (outputs[1] / name).read_bytes(), name


# --------------------------------------------------------------------------------------
# 1. cm / mm plan pair
# --------------------------------------------------------------------------------------


def test_plan_units_areas_and_labels(truth: dict[str, Any], load: Any) -> None:
    expected_m2 = {"ROOM A": 19.0, "ROOM B": 12.0, "ROOM C": 6.0}
    for name, code in (("plan_cm_v1.dxf", 5), ("plan_mm_v1.dxf", 4)):
        doc, claims = load(name), truth[name]
        assert doc.header["$INSUNITS"] == claims["insunits"] == code
        k = UNITS_PER_METRE[code]
        assert claims["drawing_units_per_metre"] == k
        assert {r["name"] for r in claims["rooms"]} == set(expected_m2)
        for room in claims["rooms"]:
            polygon = entity_by_handle(doc, room["polygon_handle"])
            assert polygon.dxftype() == "LWPOLYLINE"
            assert polygon.closed
            assert polygon.dxf.layer == "A-ROOM"
            pts = xy_points(polygon)
            area = shoelace(pts)
            assert area == pytest.approx(room["area_units2"])
            assert area / (k * k) == pytest.approx(expected_m2[room["name"]])
            assert room["area_m2"] == pytest.approx(expected_m2[room["name"]])
            flat = [c for p in pts for c in p]
            assert flat == pytest.approx([c for p in room["vertices_units"] for c in p])
            label = entity_by_handle(doc, room["label_handle"])
            assert label.dxftype() == "TEXT"
            assert label.dxf.text == room["name"]
            assert label.dxf.layer == "A-TEXT"
            assert inside((label.dxf.insert.x, label.dxf.insert.y), pts)
            wall = entity_by_handle(doc, room["wall_handle"])
            assert wall.dxftype() == "LWPOLYLINE"
            assert wall.dxf.layer == "A-WALL"
            assert wall.dxf.const_width > 0


def test_plan_origin_offset_between_cm_and_mm(truth: dict[str, Any], load: Any) -> None:
    def rooms_in_metres(name: str) -> dict[str, list[tuple[float, float]]]:
        doc = load(name)
        k = UNITS_PER_METRE[doc.header["$INSUNITS"]]
        result = {}
        for room in truth[name]["rooms"]:
            pts = xy_points(entity_by_handle(doc, room["polygon_handle"]))
            result[room["name"]] = [(x / k, y / k) for x, y in pts]
        return result

    cm, mm = rooms_in_metres("plan_cm_v1.dxf"), rooms_in_metres("plan_mm_v1.dxf")
    claimed = truth["plan_mm_v1.dxf"]["origin_offset_m"]
    assert claimed == [12.5, 7.25]
    assert truth["plan_cm_v1.dxf"]["origin_offset_m"] == [0.0, 0.0]
    assert truth["plan_mm_v1.dxf"]["origin_offset_units"] == [12500.0, 7250.0]
    for room, cm_pts in cm.items():
        assert len(mm[room]) == len(cm_pts)
        for (cx, cy), (mx, my) in zip(cm_pts, mm[room], strict=True):
            assert mx - cx == pytest.approx(claimed[0])
            assert my - cy == pytest.approx(claimed[1])


# --------------------------------------------------------------------------------------
# 2. sheet set
# --------------------------------------------------------------------------------------


def test_sheet_set_layouts_title_blocks_and_viewports(truth: dict[str, Any], load: Any) -> None:
    doc, claims = load("sheet_set.dxf"), truth["sheet_set.dxf"]
    assert doc.layouts.names_in_taborder() == ["Model", "Sheet-A", "Sheet-B"]
    assert [lay["name"] for lay in claims["layouts"]] == ["Sheet-A", "Sheet-B"]
    titles = {}
    for lay in claims["layouts"]:
        layout = doc.layouts.get(lay["name"])
        assert layout.block_record_name == lay["block_name"]
        assert set(lay["title_block"]) == {"DRAWN", "DATE", "REV", "TITLE"}
        in_layout = {e.dxf.handle for e in layout}
        for field, rec in lay["title_block"].items():
            label = entity_by_handle(doc, rec["label_handle"])
            value = entity_by_handle(doc, rec["value_handle"])
            assert label.dxftype() == value.dxftype() == "TEXT"
            assert label.dxf.text == rec["label_text"] == f"{field}:"
            assert value.dxf.text == rec["value"]
            assert label.dxf.layer == value.dxf.layer == "TB-TEXT"
            assert rec["label_handle"] in in_layout and rec["value_handle"] in in_layout
            assert value.dxf.insert.y == label.dxf.insert.y
            assert value.dxf.insert.x > label.dxf.insert.x
        titles[lay["name"]] = {f: r["value"] for f, r in lay["title_block"].items()}
        for vp_claim in lay["viewports"]:
            vp = entity_by_handle(doc, vp_claim["handle"])
            assert vp.dxftype() == "VIEWPORT"
            assert vp_claim["handle"] in in_layout
            assert vp.dxf.id == vp_claim["id"] > 1
            assert vp.dxf.view_twist_angle == pytest.approx(vp_claim["twist_deg"])
            assert list(vp.frozen_layers) == vp_claim["frozen_layers"]
    assert titles["Sheet-A"] != titles["Sheet-B"]
    twists = {lay["name"]: lay["viewports"][0]["twist_deg"] for lay in claims["layouts"]}
    assert twists == {"Sheet-A": 0.0, "Sheet-B": 90.0}


def test_fire_prints_on_matches_viewport_windows(truth: dict[str, Any], load: Any) -> None:
    """`prints_on` is recomputed here from the viewports and entities as written to the file."""
    doc, claims = load("sheet_set.dxf"), truth["sheet_set.dxf"]
    windows = {}
    for lay in claims["layouts"]:
        vp = entity_by_handle(doc, lay["viewports"][0]["handle"])
        scale = vp.dxf.view_height / vp.dxf.height
        half = (vp.dxf.width * scale / 2, vp.dxf.height * scale / 2)
        theta = math.radians(vp.dxf.view_twist_angle)
        windows[lay["name"]] = (vp.dxf.view_center_point, half, theta)
    frozen = set(claims["globally_frozen_layers"])
    expected = {}
    for occ in claims["search"]["occurrences"]:
        ent = entity_by_handle(doc, occ["handle"]) if occ["type"] != "LAYER" else None
        if ent is None:
            expected[occ["id"]] = None
        elif occ["space"] == "paper":
            expected[occ["id"]] = [occ["layout"]]
        elif occ["space"] == "block" or ent.dxf.layer in frozen:
            expected[occ["id"]] = []
        else:
            x, y = ent.dxf.insert.x, ent.dxf.insert.y
            # model point -> display coordinates: DCS = R(+twist) * WCS (target at the origin)
            expected[occ["id"]] = [
                name
                for name, (c, h, th) in windows.items()
                if abs(x * math.cos(th) - y * math.sin(th) - c.x) <= h[0]
                and abs(x * math.sin(th) + y * math.cos(th) - c.y) <= h[1]
            ]
    assert {o["id"]: o["prints_on"] for o in claims["search"]["occurrences"]} == expected
    # guard against a vacuous check: both outcomes occur
    assert expected["attrib_value"] == ["Sheet-A", "Sheet-B"]
    assert expected["text_model_visible"] == []


def test_layer_frozen_in_one_viewport_only(truth: dict[str, Any], load: Any) -> None:
    doc, claims = load("sheet_set.dxf"), truth["sheet_set.dxf"]["viewport_frozen_layer"]
    layer = doc.layers.get(claims["layer"])
    assert not layer.is_frozen() and not layer.is_off()  # not hidden globally
    per_layout = {}
    for name in ("Sheet-A", "Sheet-B"):
        vps = [v for v in doc.layouts.get(name).query("VIEWPORT") if v.dxf.id > 1]
        assert len(vps) == 1
        per_layout[name] = claims["layer"] not in vps[0].frozen_layers
    assert per_layout == claims["visible_in"] == {"Sheet-A": True, "Sheet-B": False}
    for handle in claims["entity_handles"]:
        entity = entity_by_handle(doc, handle)
        assert entity.dxf.layer == claims["layer"]
        assert entity.get_layout().name == "Model"
    frozen = sorted(lay.dxf.name for lay in doc.layers if lay.is_frozen())
    assert frozen == truth["sheet_set.dxf"]["globally_frozen_layers"] == ["A-HIDDEN"]


def _search_term_hits(doc: Any, term: str) -> set[str]:
    """Independent brute-force search: handles of everything that contains ``term``."""
    hits = {
        loc.entity.dxf.handle
        for loc in iter_all_entities(doc)
        if (text := text_of(loc.entity)) is not None and term in text.upper()
    }
    hits |= {lay.dxf.handle for lay in doc.layers if term in lay.dxf.name.upper()}
    return hits


def test_fire_occurrences_are_complete_and_described_correctly(
    truth: dict[str, Any], load: Any
) -> None:
    doc, claims = load("sheet_set.dxf"), truth["sheet_set.dxf"]["search"]
    assert claims["term"] == "FIRE"
    occurrences = claims["occurrences"]
    assert len(occurrences) == 6
    assert {o["id"] for o in occurrences} == {
        "text_on_frozen_layer",
        "attrib_value",
        "text_in_unused_block",
        "layer_name",
        "text_model_visible",
        "mtext_paper_space",
    }
    # completeness: nothing else in the file contains the term, and nothing claimed is missing
    assert _search_term_hits(doc, "FIRE") == {o["handle"] for o in occurrences}
    assert not any("FIRE" in b.name.upper() for b in doc.blocks)
    assert not any("FIRE" in n.upper() for n in doc.layouts.names())

    locs = {loc.entity.dxf.handle: loc for loc in iter_all_entities(doc)}
    inserted = {loc.entity.dxf.name for loc in locs.values() if loc.entity.dxftype() == "INSERT"}
    for occ in occurrences:
        if occ["type"] == "LAYER":
            layer = doc.layers.get(occ["layer"])
            assert layer.dxf.handle == occ["handle"]
            assert occ["space"] == "table" and occ["visible"] is None
            continue
        loc = locs[occ["handle"]]
        entity = loc.entity
        assert entity.dxftype() == occ["type"]
        assert (loc.space, loc.layout, loc.block) == (occ["space"], occ["layout"], occ["block"])
        assert entity.dxf.layer == occ["layer"]
        assert text_of(entity) == occ["text"]
        assert (loc.parent.dxf.handle if loc.parent else None) == occ["parent_handle"]
        layers = [doc.layers.get(entity.dxf.layer)]
        if loc.parent is not None:
            layers.append(doc.layers.get(loc.parent.dxf.layer))
        hidden_by_layer = any(lay.is_frozen() or lay.is_off() for lay in layers)
        rendered = loc.space != "block" or loc.block in inserted
        assert occ["visible"] == (rendered and not hidden_by_layer), occ["id"]
    by_id = {o["id"]: o for o in occurrences}
    assert by_id["text_on_frozen_layer"]["visible"] is False
    assert by_id["text_in_unused_block"]["visible"] is False
    assert by_id["attrib_value"]["visible"] is True
    assert by_id["mtext_paper_space"]["space"] == "paper"


# --------------------------------------------------------------------------------------
# 3. MTEXT
# --------------------------------------------------------------------------------------


def test_mtext_raw_plain_and_columns(truth: dict[str, Any], load: Any) -> None:
    doc, claims = load("mtext_cases.dxf"), truth["mtext_cases.dxf"]["mtext"]
    by_id = {c["id"]: c for c in claims}
    assert set(by_id) == {"inline_codes", "two_columns"}

    inline = by_id["inline_codes"]
    entity = entity_by_handle(doc, inline["handle"])
    assert entity.dxftype() == "MTEXT"
    assert entity.text == inline["raw"]
    for code in inline["codes"]:
        assert code in entity.text
    assert decode_mtext(entity.text) == inline["plain_text"]
    # ezdxf agrees once the \U+XXXX escape (which it leaves untouched) is decoded
    ezdxf_plain = re.sub(
        r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), plain_mtext(entity.text)
    )
    assert ezdxf_plain == inline["plain_text"]
    assert not entity.has_columns

    cols = by_id["two_columns"]
    entity = entity_by_handle(doc, cols["handle"])
    assert entity.text == cols["raw"]
    assert entity.has_columns
    assert entity.columns.count == cols["column_count"] == 2
    assert entity.text.split("\\N") == cols["columns"]
    assert decode_mtext(entity.text) == cols["plain_text"]


# --------------------------------------------------------------------------------------
# 4. blocks
# --------------------------------------------------------------------------------------


def test_block_attributes_and_anonymous_block(truth: dict[str, Any], load: Any) -> None:
    doc, claims = load("blocks_attribs.dxf"), truth["blocks_attribs.dxf"]
    block = doc.blocks.get(claims["block_name"])
    assert [a.dxf.tag for a in block.attdefs()] == claims["attdef_tags"]
    assert len(claims["inserts"]) >= 3
    seen_numbers = set()
    for rec in claims["inserts"]:
        insert = entity_by_handle(doc, rec["handle"])
        assert insert.dxftype() == "INSERT"
        assert insert.dxf.name == claims["block_name"]
        assert (insert.dxf.insert.x, insert.dxf.insert.y) == tuple(rec["insert"])
        assert insert.dxf.rotation == pytest.approx(rec["rotation"])
        actual = {a.dxf.tag: (a.dxf.handle, a.dxf.text) for a in insert.attribs}
        expected = {t: (v["handle"], v["value"]) for t, v in rec["attribs"].items()}
        assert actual == expected
        assert set(actual) == set(claims["attdef_tags"])
        seen_numbers.add(actual["ROOM_NO"][1])
    assert len(seen_numbers) == len(claims["inserts"])  # values differ per insert
    assert any(rec["rotation"] == 90.0 for rec in claims["inserts"])

    (anon,) = claims["anonymous_blocks"]
    anon_block = doc.blocks.get(anon["name"])
    assert anon["name"].startswith("*U")
    assert anon_block.block.dxf.flags & const.BLK_ANONYMOUS
    assert anon["flags"] == const.BLK_ANONYMOUS
    reference = entity_by_handle(doc, anon["insert_handle"])
    assert reference.dxftype() == "INSERT" and reference.dxf.name == anon["name"]
    assert reference.get_layout().name == "Model"
    content = entity_by_handle(doc, anon["content_text_handle"])
    assert content.dxf.text == anon["content_text"]
    assert content.dxf.owner == anon_block.block_record_handle


# --------------------------------------------------------------------------------------
# 5. associative hatch
# --------------------------------------------------------------------------------------


def test_associative_hatch_area(truth: dict[str, Any], load: Any) -> None:
    doc, claims = load("hatch_assoc.dxf"), truth["hatch_assoc.dxf"]
    code = doc.header["$INSUNITS"]
    assert code == claims["insunits"] == 4
    hatch = entity_by_handle(doc, claims["hatch_handle"])
    boundary = entity_by_handle(doc, claims["boundary_handle"])
    assert hatch.dxftype() == "HATCH" and boundary.dxftype() == "LWPOLYLINE"
    assert hatch.dxf.associative == 1
    assert hatch.dxf.pattern_name == claims["pattern"] == "ANSI31"
    assert hatch.dxf.solid_fill == 0
    (path,) = list(hatch.paths)
    assert list(path.source_boundary_objects) == [claims["boundary_handle"]]

    hatch_pts = [(float(v[0]), float(v[1])) for v in path.vertices]
    assert path.is_closed and boundary.closed
    assert hatch_pts == xy_points(boundary)
    area = shoelace(hatch_pts)
    assert area == pytest.approx(21_000_000.0)  # mm^2
    assert area == pytest.approx(claims["area_units2"])
    assert area / UNITS_PER_METRE[code] ** 2 == pytest.approx(21.0)
    assert claims["area_m2"] == pytest.approx(21.0)

    decoy = entity_by_handle(doc, claims["decoy_polyline"]["handle"])
    decoy_area = shoelace(xy_points(decoy))
    assert decoy_area == pytest.approx(claims["decoy_polyline"]["area_units2"])
    assert decoy_area / UNITS_PER_METRE[code] ** 2 == pytest.approx(1.0)
    assert len(doc.modelspace().query("HATCH")) == 1  # the decoy carries no hatch


# --------------------------------------------------------------------------------------
# 6. missing xref
# --------------------------------------------------------------------------------------


def test_missing_xref(out_dir: Path, truth: dict[str, Any], load: Any) -> None:
    doc, claims = load("xref_missing.dxf"), truth["xref_missing.dxf"]["xrefs"]
    (xr,) = claims
    block = doc.blocks.get(xr["block_name"])
    flags = block.block.dxf.flags
    assert flags & const.BLK_XREF
    assert bool(flags & const.BLK_XREF_OVERLAY) == xr["overlay"] is False
    assert block.block.dxf.xref_path == xr["path"] == "missing_ref.dxf"
    assert xr["file_exists"] is False
    assert not (out_dir / xr["path"]).exists()
    insert = entity_by_handle(doc, xr["insert_handle"])
    assert insert.dxftype() == "INSERT" and insert.dxf.name == xr["block_name"]


# --------------------------------------------------------------------------------------
# 7. v1 / v2 with three real changes and re-save noise
# --------------------------------------------------------------------------------------


def _content_universe(doc: Any) -> dict[str, Loc]:
    """All entities except the implicit layout viewport (id 1) that every paper space has."""
    result = {}
    for loc in iter_all_entities(doc):
        if loc.entity.dxftype() == "VIEWPORT" and loc.entity.dxf.id == 1:
            continue
        result[loc.entity.dxf.handle] = loc
    return result


def test_v1_v2_truth_covers_every_entity(truth: dict[str, Any], load: Any) -> None:
    changes = truth["plan_v2.dxf"]["changes"]
    d1, d2 = load("sheet_set_v1.dxf"), load("plan_v2.dxf")
    assert set(_content_universe(d1)) == {v["handle"] for v in changes["v1_entities"].values()}
    assert set(_content_universe(d2)) == {v["handle"] for v in changes["v2_entities"].values()}
    assert truth["sheet_set_v1.dxf"]["entities"] == changes["v1_entities"]
    for key, rec in changes["v1_entities"].items():
        assert entity_by_handle(d1, rec["handle"]).dxftype() == rec["type"], key
    for key, rec in changes["v2_entities"].items():
        assert entity_by_handle(d2, rec["handle"]).dxftype() == rec["type"], key


def test_v1_v2_exactly_three_real_changes(truth: dict[str, Any], load: Any) -> None:
    changes = truth["plan_v2.dxf"]["changes"]
    d1, d2 = load("sheet_set_v1.dxf"), load("plan_v2.dxf")
    real = {c["id"]: c for c in changes["real_changes"]}
    assert set(real) == {"C1", "C2", "C3"}

    differing = set()
    for item in changes["entity_map"]:
        e1 = entity_by_handle(d1, item["v1_handle"])
        e2 = entity_by_handle(d2, item["v2_handle"])
        assert e1.dxftype() == e2.dxftype() == item["type"]
        if canon(e1) != canon(e2):
            differing.add(item["v1_handle"])
    assert differing == {real["C1"]["v1_handle"], real["C3"]["v1_handle"]}
    mapped_v1 = {item["v1_handle"] for item in changes["entity_map"]}
    unmapped_v1 = {v["handle"] for v in changes["v1_entities"].values()} - mapped_v1
    assert unmapped_v1 == {real["C2"]["v1_handle"]}  # the only entity that disappeared
    mapped_v2 = {item["v2_handle"] for item in changes["entity_map"]}
    assert mapped_v2 == {v["handle"] for v in changes["v2_entities"].values()}  # nothing added

    # C1: one text value changed, nothing else about that entity
    t1 = entity_by_handle(d1, real["C1"]["v1_handle"])
    t2 = entity_by_handle(d2, real["C1"]["v2_handle"])
    assert (t1.dxf.text, t2.dxf.text) == (real["C1"]["before"], real["C1"]["after"])
    assert (t1.dxf.text, t2.dxf.text) == ("STORE", "ARCHIVE")
    assert canon(t1)[:3] == canon(t2)[:3] and canon(t1)[4:] == canon(t2)[4:]

    # C2: the circle is gone; v2 has no circle at all
    circle = entity_by_handle(d1, real["C2"]["v1_handle"])
    assert circle.dxftype() == "CIRCLE" and real["C2"]["v2_handle"] is None
    assert len(d1.modelspace().query("CIRCLE")) == 1
    assert len(d2.modelspace().query("CIRCLE")) == 0

    # C3: same shape, every vertex shifted by the claimed vector
    r1 = entity_by_handle(d1, real["C3"]["v1_handle"])
    r2 = entity_by_handle(d2, real["C3"]["v2_handle"])
    vx, vy = real["C3"]["vector"]
    assert (vx, vy) == (1500.0, -750.0)
    assert r1.dxf.layer == r2.dxf.layer and r1.closed == r2.closed
    for (x1, y1), (x2, y2) in zip(xy_points(r1), xy_points(r2), strict=True):
        assert (x2 - x1, y2 - y1) == pytest.approx((vx, vy))


def test_v1_v2_noise_is_present_and_declared(truth: dict[str, Any], load: Any) -> None:
    changes = truth["plan_v2.dxf"]["changes"]
    d1, d2 = load("sheet_set_v1.dxf"), load("plan_v2.dxf")
    noise = {n["kind"]: n for n in changes["noise"]}
    assert set(noise) == {
        "handles_regenerated",
        "anonymous_block_renamed",
        "paper_space_block_names",
        "viewport_ids_regenerated",
        "handseed_changed",
    }

    pairs = changes["entity_map"]
    moved = [p for p in pairs if p["v1_handle"] != p["v2_handle"]]
    assert len(moved) >= 0.9 * len(pairs)  # practically every handle was regenerated
    offsets = {int(p["v2_handle"], 16) - int(p["v1_handle"], 16) for p in pairs}
    assert len(offsets) > 3  # and not by a constant shift

    anon = noise["anonymous_block_renamed"]
    assert anon["v1"] != anon["v2"]
    assert anon["v1"] in d1.blocks and anon["v2"] not in d1.blocks
    assert anon["v2"] in d2.blocks and anon["v1"] not in d2.blocks
    inserts_v2 = {e.dxf.name for e in d2.modelspace().query("INSERT")}
    assert anon["v2"] in inserts_v2 and anon["v1"] not in inserts_v2

    names = noise["paper_space_block_names"]
    for which, doc in (("v1", d1), ("v2", d2)):
        for sheet, block_name in names[which].items():
            assert doc.layouts.get(sheet).block_record_name == block_name
    assert names["v1"] != names["v2"]
    assert any(names["v1"][s] != names["v2"][s] for s in names["v1"])

    ids = noise["viewport_ids_regenerated"]
    for which, doc in (("v1", d1), ("v2", d2)):
        for sheet, vp_id in ids[which].items():
            user_ids = [v.dxf.id for v in doc.layouts.get(sheet).query("VIEWPORT") if v.dxf.id > 1]
            assert user_ids == [vp_id]
    assert ids["v1"] != ids["v2"]

    seed = noise["handseed_changed"]
    assert d1.header["$HANDSEED"] == seed["v1"]
    assert d2.header["$HANDSEED"] == seed["v2"]
    assert seed["v1"] != seed["v2"]


# --------------------------------------------------------------------------------------
# 8. large variant (slow)
# --------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not RUN_SLOW, reason="slow; set CAD_DRAWINGS_RUN_SLOW=1 to run")
def test_large_variant(tmp_path: Path) -> None:
    truth_all = make_fixtures.generate(tmp_path, large=True)
    path = tmp_path / "plan_large.dxf"
    try:
        claims = truth_all["files"]["plan_large.dxf"]
        assert claims["counts"]["total"] == 30000
        doc = ezdxf.readfile(path)
        msp = doc.modelspace()
        counts = Counter(e.dxftype() for e in msp)
        assert len(msp) == 30000
        for kind in ("LINE", "LWPOLYLINE", "TEXT"):
            assert counts[kind] == claims["counts"][kind]
        for needle in claims["needles"]:
            text = entity_by_handle(doc, needle["handle"])
            assert text.dxf.text == needle["text"]
            assert [text.dxf.insert.x, text.dxf.insert.y] == needle["insert"]
        assert sorted(t.dxf.text for t in msp.query("TEXT") if t.dxf.text.startswith("NEEDLE")) == [
            n["text"] for n in claims["needles"]
        ]
        xs: list[float] = []
        ys: list[float] = []
        for e in msp:
            kind = e.dxftype()
            if kind == "LINE":
                points = [e.dxf.start, e.dxf.end]
            elif kind == "TEXT":
                points = [e.dxf.insert]
            else:
                points = list(e.get_points("xy"))
            xs += [p[0] for p in points]
            ys += [p[1] for p in points]
        assert [min(xs), min(ys)] == claims["extents_min"]
        assert [max(xs), max(ys)] == claims["extents_max"]
        # same seed, same bytes
        again = tmp_path / "again.dxf"
        with make_fixtures._fixed_metadata():
            doc_again, _ = make_fixtures.build_large()
            doc_again.saveas(again)
        try:
            assert again.read_bytes() == path.read_bytes()
        finally:
            again.unlink(missing_ok=True)
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# check_forbidden.py (the scanner itself must not be vacuous)
# --------------------------------------------------------------------------------------


def _needle(index: int) -> str:
    return str(check_forbidden.FORBIDDEN_STRINGS[index][0])


def test_check_forbidden_reports_planted_items(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(f"fine\nsee {_needle(2)} here\n", encoding="utf-8")
    (tmp_path / "docs" / "path.txt").write_text(f"open {_needle(0)}x\n", encoding="utf-8")
    (tmp_path / "plot.CTB").write_bytes(b"\x00\x01")
    (tmp_path / "stray.dxf").write_text("0\nEOF\n", encoding="utf-8")
    (tmp_path / "gen_py").mkdir()
    (tmp_path / "gen_py" / "cache.txt").write_text("x", encoding="utf-8")

    found = {(v.path, v.line) for v in check_forbidden.find_violations(tmp_path)}
    assert ("docs/note.md", 2) in found
    assert ("docs/path.txt", 1) in found
    assert ("plot.CTB", 0) in found
    assert ("stray.dxf", 0) in found
    assert ("gen_py/cache.txt", 0) in found
    assert check_forbidden.main(["--root", str(tmp_path)]) == 1


def test_check_forbidden_respects_exclusions_and_avoids_false_hits(tmp_path: Path) -> None:
    bad = f"{_needle(2)} {_needle(0)}\n"
    for excluded in ("_local", ".venv", "skills/cad-drawings/evals/fixtures"):
        folder = tmp_path / excluded
        folder.mkdir(parents=True)
        (folder / "x.txt").write_text(bad, encoding="utf-8")
        (folder / "x.dxf").write_text(bad, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(bad, encoding="utf-8")  # policy document
    (tmp_path / "ok.md").write_text("mo" + "delling of a sheet set\n", encoding="utf-8")
    assert check_forbidden.find_violations(tmp_path) == []
    assert check_forbidden.main(["--root", str(tmp_path)]) == 0
