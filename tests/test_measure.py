"""measure: lengths and areas with units from $INSUNITS; hatch islands; honest totals."""

from __future__ import annotations

import argparse
import json
import math
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
