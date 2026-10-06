"""measure: lengths and areas with units from $INSUNITS; hatch islands; honest totals."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import ezdxf
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import measure
from cadlib.result import CadError, ExitCode, Result

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


def run_measure(argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = measure.COMMANDS["measure"]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def entities(result: Result) -> list[dict[str, Any]]:
    data = json.loads(Path(result.outputs["measurements"]["path"]).read_text(encoding="utf-8"))
    return data["entities"]  # type: ignore[no-any-return]


def new_doc(units: int = 4) -> Any:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = units
    return doc


def save(doc: Any, tmp_path: Path, name: str = "m.dxf") -> Path:
    path = tmp_path / name
    doc.saveas(path)
    return path


# --------------------------------------------------------------------------------------
# one entity
# --------------------------------------------------------------------------------------


def test_line_length() -> None:
    line = new_doc().modelspace().add_line((0, 0), (3, 4))
    m = measure.measure_entity(line)
    assert m is not None and m.length == pytest.approx(5.0, rel=1e-6)
    assert m.area is None and m.closed is False


def test_arc_length() -> None:
    arc = new_doc().modelspace().add_arc((0, 0), 10, 0, 90)
    m = measure.measure_entity(arc)
    assert m is not None and m.length == pytest.approx(5 * math.pi, rel=1e-5)
    assert m.area is None


def test_circle_has_circumference_and_area() -> None:
    circle = new_doc().modelspace().add_circle((0, 0), 5)
    m = measure.measure_entity(circle)
    assert m is not None and m.closed is True
    assert m.length == pytest.approx(10 * math.pi, rel=1e-5)
    assert m.area == pytest.approx(25 * math.pi, rel=1e-5)


def test_closed_polyline_has_area_and_open_one_does_not() -> None:
    msp = new_doc().modelspace()
    closed = measure.measure_entity(
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    )
    assert closed is not None
    assert closed.area == pytest.approx(100.0) and closed.length == pytest.approx(40.0)
    opened = measure.measure_entity(msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)]))
    assert opened is not None
    assert opened.area is None and opened.length == pytest.approx(20.0)


def test_a_polyline_arc_segment_is_measured_as_an_arc() -> None:
    poly = (
        new_doc()
        .modelspace()
        .add_lwpolyline([(0, 0, 0, 0, 1), (10, 0, 0, 0, 0)], format="xyseb", close=True)
    )
    m = measure.measure_entity(poly)
    assert m is not None
    assert m.area == pytest.approx(25 * math.pi / 2, rel=1e-5)  # bulge 1 is a semicircle
    assert m.length == pytest.approx(10 + 5 * math.pi, rel=1e-5)


def test_hatch_area_is_outline_minus_islands() -> None:
    hatch = new_doc().modelspace().add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True, flags=1)
    hatch.paths.add_polyline_path([(3, 3), (7, 3), (7, 7), (3, 7)], is_closed=True, flags=16)
    m = measure.measure_entity(hatch)
    assert m is not None
    assert m.area == pytest.approx(84.0)
    assert m.length == pytest.approx(40.0 + 16.0)  # boundary length including the island
    assert "island" in m.note


def test_text_is_not_measurable() -> None:
    text = new_doc().modelspace().add_text("x")
    assert measure.measure_entity(text) is None


# --------------------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------------------


def test_unit_factors_come_from_insunits() -> None:
    assert measure.unit_factor(4, "m") == pytest.approx(0.001)
    assert measure.unit_factor(6, "mm") == pytest.approx(1000.0)
    assert measure.unit_factor(1, "mm") == pytest.approx(25.4)


def test_unknown_unit_name_is_bad_args() -> None:
    with pytest.raises(CadError) as err:
        measure.unit_factor(4, "parsec")
    assert err.value.code == "BAD_ARGS"


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def square(msp: Any, side: float, layer: str = "ROOM", kind: str = "poly") -> None:
    pts = [(0, 0), (side, 0), (side, side), (0, side)]
    if kind == "poly":
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    else:
        h = msp.add_hatch(dxfattribs={"layer": layer})
        h.paths.add_polyline_path(pts, is_closed=True, flags=1)


def test_area_in_square_metres_from_a_millimetre_drawing(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    square(doc.modelspace(), 2000)
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--unit", "m"], runs)
    assert result.exit_code == ExitCode.OK
    assert result.summary["unit"] == "m"
    assert result.summary["area"] == pytest.approx(4.0, rel=1e-6)
    assert result.summary["length"] == pytest.approx(8.0, rel=1e-6)
    assert result.summary["drawing_units"] == "mm"


def test_the_same_shape_in_a_metre_drawing(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(6)
    square(doc.modelspace(), 2)
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--unit", "mm"], runs)
    assert result.summary["area"] == pytest.approx(4_000_000.0, rel=1e-6)


def test_unitless_drawing_is_refused_with_a_hint(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(0)
    square(doc.modelspace(), 10)
    with pytest.raises(CadError) as err:
        run_measure([str(save(doc, tmp_path)), "--layer", "ROOM"], runs)
    assert err.value.code == "NO_UNITS"
    assert "--assume-unit" in (err.value.hint or "")


def test_assume_unit_overrides_a_unitless_drawing_and_warns(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(0)
    square(doc.modelspace(), 2000)
    result = run_measure(
        [str(save(doc, tmp_path)), "--layer", "ROOM", "--assume-unit", "mm", "--unit", "m"], runs
    )
    assert result.summary["area"] == pytest.approx(4.0, rel=1e-6)
    assert any("assum" in w.lower() for w in result.warnings)


def test_a_filter_is_required(tmp_path: Path, runs: Path) -> None:
    with pytest.raises(CadError) as err:
        run_measure([str(save(new_doc(), tmp_path))], runs)
    assert err.value.code == "BAD_ARGS"


def test_default_space_is_model_and_layouts_can_be_chosen(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    square(doc.modelspace(), 1000)
    lay = doc.layouts.new("Sheet-A")
    square(lay, 50)
    path = save(doc, tmp_path)
    in_model = run_measure([str(path), "--layer", "ROOM"], runs)
    assert in_model.summary["count"] == 1
    in_sheet = run_measure([str(path), "--layer", "ROOM", "--space", "Sheet-A"], runs)
    assert in_sheet.summary["count"] == 1 and in_sheet.summary["area"] == pytest.approx(2500 / 1e6)


def test_hatch_and_its_outline_are_not_added_together(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    square(doc.modelspace(), 1000, kind="poly")
    square(doc.modelspace(), 1000, kind="hatch")
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM"], runs)
    assert "area" not in result.summary  # no single total when several entity types matched
    assert set(result.summary["by_type"]) == {"HATCH", "LWPOLYLINE"}
    assert result.summary["by_type"]["HATCH"]["area"] == pytest.approx(1.0)
    assert any("several entity types" in w for w in result.warnings)


def test_filter_by_handle_and_type(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    a = msp.add_line((0, 0), (1000, 0))
    msp.add_line((0, 0), (0, 500))
    path = save(doc, tmp_path)
    by_handle = run_measure([str(path), "--handle", a.dxf.handle], runs)
    assert by_handle.summary["length"] == pytest.approx(1.0)
    by_type = run_measure([str(path), "--type", "LINE"], runs)
    assert by_type.summary["count"] == 2 and by_type.summary["length"] == pytest.approx(1.5)


def test_unsupported_matches_are_counted_not_hidden(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0), dxfattribs={"layer": "MIX"})
    msp.add_text("label", dxfattribs={"layer": "MIX"})
    result = run_measure([str(save(doc, tmp_path)), "--layer", "MIX"], runs)
    assert result.summary["count"] == 1
    assert result.summary["skipped"] == {"TEXT": 1}


def test_nothing_matched_is_a_warning_with_zero_totals(tmp_path: Path, runs: Path) -> None:
    result = run_measure([str(save(new_doc(), tmp_path)), "--layer", "NOPE"], runs)
    assert result.summary["count"] == 0
    assert any("no measurable" in w.lower() for w in result.warnings)


def test_per_entity_records_carry_handle_layer_and_values(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    square(doc.modelspace(), 1000)
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM"], runs)
    rec = entities(result)[0]
    assert {"handle", "type", "layer", "space", "length", "area", "closed"} <= rec.keys()
    assert rec["layer"] == "ROOM" and rec["closed"] is True


def test_the_summary_stays_small_for_many_entities(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    for i in range(300):
        msp.add_line((0, i), (100, i), dxfattribs={"layer": f"L{i % 40}"})
    result = run_measure([str(save(doc, tmp_path)), "--type", "LINE"], runs)
    assert len(json.dumps(result.summary)) < 2500
    assert len(entities(result)) == 300


# --------------------------------------------------------------------------------------
# far from the origin (survey coordinates)
# --------------------------------------------------------------------------------------


def test_area_survives_coordinates_far_from_the_origin() -> None:
    """A real drawing had circles near x = 5e8, y = 1.5e9: the shoelace sum on absolute
    coordinates lost every digit of a small circle's area."""
    msp = new_doc().modelspace()
    far = (523_030_190.186, 1_558_582_582.901)
    m = measure.measure_entity(msp.add_circle(far, 8.0))
    assert m is not None
    assert m.area == pytest.approx(math.pi * 64, rel=1e-5)
    square = measure.measure_entity(
        msp.add_lwpolyline(
            [
                (far[0], far[1]),
                (far[0] + 3, far[1]),
                (far[0] + 3, far[1] + 2),
                (far[0], far[1] + 2),
            ],
            close=True,
        )
    )
    assert square is not None and square.area == pytest.approx(6.0, rel=1e-9)


def test_hatch_area_survives_coordinates_far_from_the_origin() -> None:
    hatch = new_doc().modelspace().add_hatch()
    ox, oy = 523_030_190.0, 1_558_582_582.0
    hatch.paths.add_polyline_path(
        [(ox, oy), (ox + 10, oy), (ox + 10, oy + 10), (ox, oy + 10)], is_closed=True, flags=1
    )
    hatch.paths.add_polyline_path(
        [(ox + 3, oy + 3), (ox + 7, oy + 3), (ox + 7, oy + 7), (ox + 3, oy + 7)],
        is_closed=True,
        flags=16,
    )
    m = measure.measure_entity(hatch)
    assert m is not None and m.area == pytest.approx(84.0, rel=1e-9)


def test_small_values_keep_their_significant_digits(tmp_path: Path, runs: Path) -> None:
    """Rounding every entity to 6 decimals of the result unit cost ~1e-4 on a millimetre
    drawing full of tiny arcs measured in metres."""
    doc = new_doc(4)
    msp = doc.modelspace()
    for i in range(200):
        msp.add_line((0, i), (0.1234567, i))  # 0.1234567 mm each
    result = run_measure([str(save(doc, tmp_path)), "--type", "LINE", "--unit", "m"], runs)
    assert result.summary["length"] == pytest.approx(200 * 0.1234567e-3, rel=1e-6)
    assert entities(result)[0]["length"] == pytest.approx(0.1234567e-3, rel=1e-6)


# --------------------------------------------------------------------------------------
# --join: loose segments into contours
# --------------------------------------------------------------------------------------


def lines_rectangle(
    msp: Any, w: float, h: float, layer: str = "ROOM", origin: tuple[float, float] = (0.0, 0.0)
) -> list[Any]:
    x, y = origin
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return [
        msp.add_line(corners[i], corners[(i + 1) % 4], dxfattribs={"layer": layer})
        for i in range(4)
    ]


def contours(result: Result) -> list[dict[str, Any]]:
    return [r for r in entities(result) if r["type"] == "CONTOUR"]


def test_four_lines_join_into_one_contour_with_area_and_members(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    made = lines_rectangle(doc.modelspace(), 2000, 1000)
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    (contour,) = entities(result)
    assert contour["type"] == "CONTOUR" and contour["closed"] is True
    assert contour["area"] == pytest.approx(2.0, rel=1e-9)
    assert contour["length"] == pytest.approx(6.0, rel=1e-9)
    assert sorted(contour["members"]) == sorted(str(e.dxf.handle) for e in made)
    assert contour["handle"] == contour["members"][0]
    assert contour["layer"] == "ROOM" and contour["space"] == "Model"
    assert "note" in contour
    assert result.summary["area"] == pytest.approx(2.0, rel=1e-9)  # one type: a total
    assert result.exit_code == ExitCode.OK


def test_members_do_not_appear_as_separate_records(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    lines_rectangle(msp, 2000, 1000)
    far = msp.add_line((50_000, 0), (51_000, 0), dxfattribs={"layer": "ROOM"})
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    kinds = sorted(r["type"] for r in entities(result))
    assert kinds == ["CONTOUR", "LINE"]
    assert [r["handle"] for r in entities(result) if r["type"] == "LINE"] == [str(far.dxf.handle)]
    assert result.summary["count"] == 2 and "area" not in result.summary


def test_line_arc_and_bulged_open_polyline_contour_area_is_exact(
    tmp_path: Path, runs: Path
) -> None:
    """A stadium made of a reversed LINE, an ARC in a mirrored OCS (extrusion 0,0,-1), a LINE
    and an open polyline with a bulge, drawn in mixed directions."""
    doc = new_doc(6)
    msp = doc.modelspace()
    msp.add_line((10, 0), (0, 0))  # reversed
    msp.add_arc(
        (-10, 5, 0),
        5,
        90,
        270,
        dxfattribs={"extrusion": (0, 0, -1)},
    )  # WCS: (10,10) -> (15,5) -> (10,0)
    msp.add_line((10, 10), (0, 10))
    msp.add_lwpolyline([(0, 10, 1), (0, 0, 0)], format="xyb")  # bulges to the left
    result = run_measure(
        [
            str(save(doc, tmp_path)),
            "--type",
            "LINE",
            "--type",
            "ARC",
            "--type",
            "LWPOLYLINE",
            "--join",
            "--unit",
            "m",
        ],
        runs,
    )
    (contour,) = entities(result)
    assert len(contour["members"]) == 4
    assert contour["area"] == pytest.approx(100 + 25 * math.pi, rel=1e-5)
    assert contour["length"] == pytest.approx(20 + 10 * math.pi, rel=1e-5)


def test_survey_coordinates_contour_area_exact(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(6)
    lines_rectangle(doc.modelspace(), 3, 2, origin=(523_030_190.186, 1_558_582_582.901))
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    (contour,) = entities(result)
    assert contour["area"] == pytest.approx(6.0, rel=1e-9)
    assert contour["length"] == pytest.approx(10.0, rel=1e-9)


def test_branching_network_is_not_joined_and_warns(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    lines_rectangle(msp, 1000, 1000)
    msp.add_line((0, 0), (1000, 1000), dxfattribs={"layer": "ROOM"})  # a diagonal: degree 3
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    assert [r["type"] for r in entities(result)] == ["LINE"] * 5
    assert any(
        "5 segments form a branching network; contour ambiguous" in w for w in result.warnings
    )
    assert result.summary["joined"]["contours"] == 0
    assert result.summary["joined"]["segments_left"] == 5


def _u_shape(tmp_path: Path, name: str = "u.dxf") -> Path:
    """A 10 x 10 square whose left side stops 0.5 short of the top-left corner."""
    doc = new_doc(4)
    msp = doc.modelspace()
    for a, b in [((0, 0), (10, 0)), ((10, 0), (10, 10)), ((10, 10), (0, 10)), ((0, 0), (0, 9.5))]:
        msp.add_line(a, b, dxfattribs={"layer": "ROOM"})
    return save(doc, tmp_path, name)


def test_open_chain_is_not_joined_and_reports_end_distance(tmp_path: Path, runs: Path) -> None:
    result = run_measure([str(_u_shape(tmp_path)), "--layer", "ROOM", "--join"], runs)
    assert [r["type"] for r in entities(result)] == ["LINE"] * 4
    warning = next(w for w in result.warnings if "open chain" in w)
    assert "0.5" in warning and "--gap" in warning
    assert result.summary["joined"]["contours"] == 0


def test_gap_closes_only_with_explicit_flag(tmp_path: Path, runs: Path) -> None:
    path = _u_shape(tmp_path)
    plain = run_measure([str(path), "--layer", "ROOM", "--join", "--unit", "mm"], runs)
    assert not contours(plain)
    closed = run_measure(
        [str(path), "--layer", "ROOM", "--join", "--gap", "0.6", "--unit", "mm"], runs
    )
    (contour,) = entities(closed)
    assert contour["type"] == "CONTOUR" and contour["area"] == pytest.approx(100.0, rel=1e-9)
    assert contour["length"] == pytest.approx(39.5, rel=1e-9)  # the bridged gap is not length
    assert closed.summary["joined"]["max_gap"] == pytest.approx(0.5, rel=1e-6)
    assert closed.summary["joined"]["gap"] == pytest.approx(0.6)


def test_the_suggested_gap_really_closes_the_chain(tmp_path: Path, runs: Path) -> None:
    path = _u_shape(tmp_path)
    plain = run_measure([str(path), "--layer", "ROOM", "--join", "--unit", "mm"], runs)
    warning = next(w for w in plain.warnings if "open chain" in w)
    suggested = re.search(r"--gap ([0-9.e+-]+)", warning)
    assert suggested is not None
    closed = run_measure(
        [str(path), "--layer", "ROOM", "--join", "--gap", suggested.group(1), "--unit", "mm"], runs
    )
    assert len(contours(closed)) == 1


def test_a_gap_as_wide_as_half_a_segment_warns(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    lines_rectangle(doc.modelspace(), 10, 10)
    result = run_measure(
        [str(save(doc, tmp_path)), "--layer", "ROOM", "--join", "--gap", "6"], runs
    )
    assert any("--gap" in w and "shortest" in w for w in result.warnings)


def test_closed_polyline_stays_a_normal_record(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    square(msp, 1000)
    lines_rectangle(msp, 3000, 1000, origin=(5000, 0))
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    kinds = sorted(r["type"] for r in entities(result))
    assert kinds == ["CONTOUR", "LWPOLYLINE"]
    assert result.summary["joined"]["segments_used"] == 4
    assert result.summary["joined"]["segments_left"] == 0  # a closed polyline is not a segment


def test_no_join_across_spaces(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    layout = doc.layouts.new("Sheet-A")
    sides = [((0, 0), (100, 0)), ((100, 0), (100, 50)), ((100, 50), (0, 50)), ((0, 50), (0, 0))]
    for i, (a, b) in enumerate(sides):
        target = doc.modelspace() if i % 2 == 0 else layout
        target.add_line(a, b, dxfattribs={"layer": "ROOM"})
    result = run_measure(
        [str(save(doc, tmp_path)), "--layer", "ROOM", "--join", "--space", "all"], runs
    )
    assert not contours(result)
    assert result.summary["count"] == 4


def test_joined_summary_fields(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    lines_rectangle(doc.modelspace(), 2000, 1000)
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    joined = result.summary["joined"]
    assert set(joined) == {"contours", "segments_used", "segments_left", "gap", "max_gap"}
    assert joined["contours"] == 1 and joined["segments_used"] == 4 and joined["segments_left"] == 0
    # default gap: 1e-6 of the selection (2000 mm), reported in the result unit (m)
    assert joined["gap"] == pytest.approx(2e-6, rel=1e-6)
    assert joined["max_gap"] == pytest.approx(0.0, abs=1e-12)
    assert len(json.dumps(result.summary)) < 2500


def test_without_join_nothing_changes_and_there_is_no_joined_summary(
    tmp_path: Path, runs: Path
) -> None:
    doc = new_doc(4)
    lines_rectangle(doc.modelspace(), 2000, 1000)
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM"], runs)
    assert [r["type"] for r in entities(result)] == ["LINE"] * 4
    assert "joined" not in result.summary


@pytest.mark.parametrize(
    "extra", [["--gap", "1"], ["--join", "--gap", "0"], ["--join", "--gap", "-1"]]
)
def test_gap_without_join_is_bad_args(tmp_path: Path, runs: Path, extra: list[str]) -> None:
    doc = new_doc(4)
    lines_rectangle(doc.modelspace(), 2000, 1000)
    with pytest.raises(CadError) as err:
        run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", *extra], runs)
    assert err.value.code == "BAD_ARGS"


def test_contour_converts_units(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(6)  # metres
    lines_rectangle(doc.modelspace(), 2, 1)
    result = run_measure(
        [str(save(doc, tmp_path)), "--layer", "ROOM", "--join", "--unit", "mm", "--gap", "0.001"],
        runs,
    )
    (contour,) = entities(result)
    assert contour["area"] == pytest.approx(2_000_000.0, rel=1e-9)
    assert contour["length"] == pytest.approx(6000.0, rel=1e-9)
    assert result.summary["joined"]["gap"] == pytest.approx(1.0)  # 0.001 m in mm


def test_a_contour_over_several_layers_says_so(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    for i, (a, b) in enumerate(
        [((0, 0), (10, 0)), ((10, 0), (10, 10)), ((10, 10), (0, 10)), ((0, 10), (0, 0))]
    ):
        msp.add_line(a, b, dxfattribs={"layer": "A" if i < 2 else "B"})
    result = run_measure([str(save(doc, tmp_path)), "--type", "LINE", "--join"], runs)
    (contour,) = entities(result)
    assert "layers" in contour["note"] and "A" in contour["note"] and "B" in contour["note"]


def test_coincident_segments_are_not_a_contour(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "ROOM"})
    msp.add_line((10, 0), (0, 0), dxfattribs={"layer": "ROOM"})
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    assert not contours(result)
    assert any("no area" in w for w in result.warnings)


def test_two_loops_are_two_contours(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    lines_rectangle(msp, 1000, 1000)
    lines_rectangle(msp, 2000, 1000, origin=(5000, 0))
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    assert sorted(round(c["area"], 6) for c in contours(result)) == [1.0, 2.0]
    assert result.summary["joined"]["contours"] == 2


def test_two_half_arcs_join_into_a_circle(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    msp.add_arc((0, 0), 5, 0, 180)
    msp.add_arc((0, 0), 5, 180, 360)
    result = run_measure(
        [str(save(doc, tmp_path)), "--type", "ARC", "--join", "--unit", "mm"], runs
    )
    (contour,) = entities(result)
    assert contour["area"] == pytest.approx(25 * math.pi, rel=1e-5)
    assert contour["length"] == pytest.approx(10 * math.pi, rel=1e-5)


def test_segments_in_different_blocks_are_not_joined(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    left = doc.blocks.new("LEFT")
    left.add_line((0, 0), (10, 0))
    left.add_line((10, 0), (10, 10))
    right = doc.blocks.new("RIGHT")
    right.add_line((10, 10), (0, 10))
    right.add_line((0, 10), (0, 0))
    result = run_measure(
        [str(save(doc, tmp_path)), "--type", "LINE", "--space", "all", "--join"], runs
    )
    assert not contours(result) and result.summary["count"] == 4
    assert result.summary["joined"]["segments_left"] == 4


def test_a_block_definition_can_be_joined_by_name(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    blk = doc.blocks.new("ROOM-TAG")
    lines_rectangle(blk, 10, 10)
    result = run_measure(
        [
            str(save(doc, tmp_path)),
            "--type",
            "LINE",
            "--space",
            "ROOM-TAG",
            "--join",
            "--unit",
            "mm",
        ],
        runs,
    )
    (contour,) = entities(result)
    assert contour["area"] == pytest.approx(100.0) and contour["space"] == "ROOM-TAG"


def test_half_ellipse_and_a_line_join_exactly(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    msp.add_ellipse((0, 0), major_axis=(5, 0), ratio=0.5, start_param=0, end_param=math.pi)
    msp.add_line((5, 0), (-5, 0))
    result = run_measure(
        [str(save(doc, tmp_path)), "--type", "ELLIPSE", "--type", "LINE", "--join", "--unit", "mm"],
        runs,
    )
    (contour,) = entities(result)
    assert contour["area"] == pytest.approx(math.pi * 5 * 2.5 / 2, rel=1e-5)


def test_a_spline_edge_joins_a_contour(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    msp.add_spline(fit_points=[(0, 0), (5, 2), (10, 0)])
    msp.add_line((10, 0), (0, 0))
    result = run_measure(
        [str(save(doc, tmp_path)), "--type", "SPLINE", "--type", "LINE", "--join", "--unit", "mm"],
        runs,
    )
    (contour,) = entities(result)
    assert contour["type"] == "CONTOUR" and 10 < contour["area"] < 20


def test_many_squares_join_quickly_and_all(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    for i in range(300):
        lines_rectangle(msp, 8, 8, origin=((i % 30) * 10.0, (i // 30) * 10.0))
    result = run_measure([str(save(doc, tmp_path)), "--type", "LINE", "--join"], runs)
    assert result.summary["joined"]["contours"] == 300
    assert result.summary["joined"]["segments_left"] == 0
    assert len(json.dumps(result.summary)) < 2500
    assert len(result.to_json().encode("utf-8")) < 4096


def test_join_leaves_closed_shapes_and_other_types_alone(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    msp.add_circle((0, 0), 5)
    h = msp.add_hatch()
    h.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True, flags=1)
    msp.add_text("t")
    result = run_measure(
        [str(save(doc, tmp_path)), "--type", "CIRCLE", "--type", "HATCH", "--join"], runs
    )
    assert sorted(r["type"] for r in entities(result)) == ["CIRCLE", "HATCH"]
    assert result.summary["joined"]["contours"] == 0


def test_no_total_after_join_is_explained_in_terms_of_contours(tmp_path: Path, runs: Path) -> None:
    doc = new_doc(4)
    msp = doc.modelspace()
    lines_rectangle(msp, 1000, 1000)
    msp.add_line((9000, 0), (9500, 0), dxfattribs={"layer": "ROOM"})
    result = run_measure([str(save(doc, tmp_path)), "--layer", "ROOM", "--join"], runs)
    assert "area" not in result.summary
    assert any("contours" in w for w in result.warnings)
    assert not any("hatch" in w for w in result.warnings)
