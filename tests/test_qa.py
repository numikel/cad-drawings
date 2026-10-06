"""qa: findings with severity on a drawing and on a plotted PDF; exit 7 only for errors."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ezdxf
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import qa
from cadlib.result import ERROR_CODES, CadError, ExitCode, Result

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


def run_qa(argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = qa.COMMANDS["qa"]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def findings(result: Result) -> list[dict[str, Any]]:
    data = json.loads(Path(result.outputs["findings"]["path"]).read_text(encoding="utf-8"))
    return data["findings"]  # type: ignore[no-any-return]


def ids(result: Result) -> set[str]:
    return {f["id"] for f in findings(result)}


def make_dxf(
    path: Path,
    *,
    units: int = 4,
    sheet_content: bool = True,
    vp_status: int = 1,
    xref: bool = False,
    frame: bool = True,
    margins: tuple[float, float, float, float] = (0, 0, 0, 0),
    rotation: int = 0,
    scale: Any = 16,
    build: Callable[[Any, Any], None] | None = None,
) -> Path:
    """A synthetic sheet. ``frame`` adds a closed border (10,10)-(410,287) on layer FRAME and
    ``build(doc, layout)`` may add more before the file is saved; margins are top, right,
    bottom, left (mm)."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = units
    doc.layers.add("FRAME")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0))
    msp.add_line((0, 0), (0, 1000))
    lay = doc.layouts.new("Sheet-A")
    doc.layouts.delete("Layout1")  # the empty default layout would be (rightly) reported
    lay.page_setup(size=(420, 297), margins=margins, units="mm", rotation=rotation, scale=scale)
    vp = lay.add_viewport(
        center=(210, 148), size=(400, 280), view_center_point=(500, 500), view_height=1000
    )
    vp.dxf.status = vp_status
    if sheet_content:
        lay.add_line((5, 5), (415, 5))
        lay.add_text("TITLE", height=5).set_placement((20, 20))
        if frame:
            lay.add_lwpolyline(
                [(10, 10), (410, 10), (410, 287), (10, 287)],
                close=True,
                dxfattribs={"layer": "FRAME"},
            )
    if xref:
        doc.add_xref_def("missing/underlay-base.dxf", "UNDERLAY")
    if build is not None:
        build(doc, lay)
    doc.saveas(path)
    return path


def derive(src: Path, dst: Path, change: Callable[[Any, Any], None]) -> Path:
    """Copy ``src`` to ``dst`` through ezdxf (handles are kept) after ``change(doc, layout)``."""
    doc = ezdxf.readfile(src)
    change(doc, doc.layouts.get("Sheet-A"))
    doc.saveas(dst)
    return dst


def make_pdf(
    path: Path,
    size_mm: tuple[float, float],
    *,
    draw: bool = True,
    text: str | None = None,
    frame_mm: tuple[float, float, float, float] | None = (10, 10, 410, 287),
    shift_mm: tuple[float, float] = (0.0, 0.0),
    extra_mm: tuple[tuple[float, float, float, float], ...] = (),
    background: bool = False,
) -> Path:
    """A synthetic plot drawn in millimetres (y up). Content stays well inside the page unless
    ``frame_mm`` or ``extra_mm`` say otherwise; ``shift_mm`` moves everything like a plot that
    inherited a page setup. ``background`` adds a white rectangle over the whole page."""
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    dx, dy = shift_mm
    with matplotlib.rc_context({"pdf.fonttype": 42}):
        fig = Figure(figsize=(size_mm[0] / 25.4, size_mm[1] / 25.4))
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, size_mm[0])
        ax.set_ylim(0, size_mm[1])
        ax.axis("off")
        if background:
            ax.add_patch(
                Rectangle((0, 0), size_mm[0], size_mm[1], facecolor="white", edgecolor="none")
            )
        if draw:
            for i in range(30):
                t = i / 30
                ax.plot(
                    [30 + dx, 390 + dx],
                    [30 + dy + 240 * t, 270 + dy - 240 * t],
                    color="black",
                    linewidth=0.7,
                )
            if frame_mm is not None:
                x1, y1, x2, y2 = frame_mm
                ax.add_patch(
                    Rectangle(
                        (x1 + dx, y1 + dy),
                        x2 - x1,
                        y2 - y1,
                        fill=False,
                        edgecolor="black",
                        linewidth=0.7,
                    )
                )
            for x1, y1, x2, y2 in extra_mm:
                ax.plot([x1, x2], [y1, y2], color="black", linewidth=0.7)
        if text:
            fig.text(0.5, 0.5, text, fontsize=20)
        fig.savefig(str(path), format="pdf")
    return path


def frames(result: Result) -> dict[str, Any]:
    data = json.loads(Path(result.outputs["findings"]["path"]).read_text(encoding="utf-8"))
    return data.get("frames", {})  # type: ignore[no-any-return]


def of(result: Result, finding_id: str) -> list[dict[str, Any]]:
    return [f for f in findings(result) if f["id"] == finding_id]


def rect(layout: Any, box: tuple[float, float, float, float], layer: str = "0") -> Any:
    x1, y1, x2, y2 = box
    return layout.add_lwpolyline(
        [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], close=True, dxfattribs={"layer": layer}
    )


# --------------------------------------------------------------------------------------
# registry and contract
# --------------------------------------------------------------------------------------


def test_qa_failed_is_a_registered_partial_exit_code() -> None:
    assert ERROR_CODES["QA_FAILED"] == ExitCode.PARTIAL


# --------------------------------------------------------------------------------------
# drawing checks
# --------------------------------------------------------------------------------------


def test_a_clean_drawing_has_no_findings(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "clean.dxf")
    result = run_qa([str(dxf)], runs)
    assert result.exit_code == ExitCode.OK
    assert findings(result) == []
    assert result.summary["errors"] == 0 and result.summary["warnings"] == 0


def test_unset_units_are_a_warning_and_do_not_fail_the_run(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "u.dxf", units=0))], runs)
    assert "UNITS_UNSET" in ids(result)
    assert next(f for f in findings(result) if f["id"] == "UNITS_UNSET")["severity"] == "warning"
    assert result.exit_code == ExitCode.OK


def test_a_layout_with_only_a_viewport_is_reported_empty(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "e.dxf", sheet_content=False))], runs)
    hit = next(f for f in findings(result) if f["id"] == "LAYOUT_EMPTY")
    assert hit["where"] == "Sheet-A" and hit["severity"] == "warning"


def test_viewport_with_unknown_print_state_is_info(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "v.dxf", vp_status=0))], runs)
    hit = next(f for f in findings(result) if f["id"] == "VIEWPORT_STATUS_UNKNOWN")
    assert hit["severity"] == "info"
    assert result.exit_code == ExitCode.OK


def test_a_missing_xref_is_a_warning(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "x.dxf", xref=True))], runs)
    hit = next(f for f in findings(result) if f["id"] == "XREF_MISSING")
    assert hit["severity"] == "warning" and "UNDERLAY" in hit["where"]


def test_each_finding_carries_id_severity_where_and_message(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "m.dxf", units=0))], runs)
    for f in findings(result):
        assert {"id", "severity", "where", "message"} <= f.keys()
        assert f["severity"] in {"info", "warning", "error"}


# --------------------------------------------------------------------------------------
# PDF checks
# --------------------------------------------------------------------------------------


def test_a_good_pdf_passes(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "ok.pdf", (420, 297))
    assert qa.check_pdf(pdf, expected_mm=(420, 297)) == []


def test_an_empty_pdf_is_an_error(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "blank.pdf", (420, 297), draw=False)
    assert "PDF_EMPTY" in {f.id for f in qa.check_pdf(pdf)}
    assert {f.severity for f in qa.check_pdf(pdf) if f.id == "PDF_EMPTY"} == {"error"}


def test_a_wrong_page_size_is_an_error(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "a4.pdf", (210, 297))
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    hit = next(f for f in found if f.id == "PDF_SIZE")
    assert hit.severity == "error" and "210" in hit.message and "420" in hit.message


def test_a_rotated_page_of_the_same_sheet_is_not_a_size_error(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "rot.pdf", (297, 420))
    assert "PDF_SIZE" not in {f.id for f in qa.check_pdf(pdf, expected_mm=(420, 297))}


def test_size_within_tolerance_is_accepted(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "tol.pdf", (420.8, 297.4))
    assert "PDF_SIZE" not in {f.id for f in qa.check_pdf(pdf, expected_mm=(420, 297))}


def test_page_count_is_checked_against_the_expected_number(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "one.pdf", (420, 297))
    assert "PDF_PAGES" not in {f.id for f in qa.check_pdf(pdf)}
    assert "PDF_PAGES" in {f.id for f in qa.check_pdf(pdf, expect_pages=2)}


def test_required_and_forbidden_words_are_searched_in_the_text_layer(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "words.pdf", (420, 297), text="Revision C 2026-10-03")
    ok = qa.check_pdf(pdf, require=("Revision C",), forbid=("DRAFT",))
    assert ok == []
    bad = qa.check_pdf(pdf, require=("Revision D",), forbid=("2026-10-03",))
    assert {f.id for f in bad} == {"PDF_WORD_MISSING", "PDF_WORD_FORBIDDEN"}
    assert {f.severity for f in bad} == {"error"}


def test_word_search_is_case_insensitive_and_ignores_line_breaks(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "case.pdf", (420, 297), text="Fire\nSafety")
    assert qa.check_pdf(pdf, require=("fire safety",)) == []


def test_a_pdf_without_text_layer_says_so_when_words_are_requested(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "notext.pdf", (420, 297))
    found = qa.check_pdf(pdf, require=("anything",))
    assert "PDF_NO_TEXT" in {f.id for f in found}
    assert {f.severity for f in found if f.id == "PDF_NO_TEXT"} == {"info"}


def test_a_file_that_is_not_a_pdf_is_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert [f.id for f in qa.check_pdf(bad)] == ["PDF_UNREADABLE"]


# --------------------------------------------------------------------------------------
# command wiring
# --------------------------------------------------------------------------------------


def test_errors_make_the_run_partial_with_qa_failed(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(tmp_path / "a4.pdf", (210, 297))
    result = run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Sheet-A"], runs)
    assert result.exit_code == ExitCode.PARTIAL
    assert [e["code"] for e in result.errors] == ["QA_FAILED"]
    assert "PDF_SIZE" in ids(result)


def test_the_expected_size_comes_from_the_named_layout(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(tmp_path / "a3.pdf", (420, 297))
    result = run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Sheet-A"], runs)
    assert result.exit_code == ExitCode.OK and findings(result) == []


def test_a_pdf_can_be_checked_alone_with_an_explicit_size(tmp_path: Path, runs: Path) -> None:
    pdf = make_pdf(tmp_path / "a3.pdf", (420, 297))
    result = run_qa(["--pdf", str(pdf), "--size", "420x297"], runs)
    assert result.exit_code == ExitCode.OK
    result = run_qa(["--pdf", str(pdf), "--size", "210x297"], runs)
    assert result.exit_code == ExitCode.PARTIAL


def test_no_input_is_bad_args(runs: Path) -> None:
    with pytest.raises(CadError) as err:
        run_qa([], runs)
    assert err.value.code == "BAD_ARGS"


def test_an_unknown_layout_is_reported(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(tmp_path / "a3.pdf", (420, 297))
    with pytest.raises(CadError) as err:
        run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Nope"], runs)
    assert err.value.code == "LAYOUT_NOT_FOUND"


def test_the_summary_stays_small_and_lists_the_first_findings(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "s.dxf", units=0, xref=True))], runs)
    assert len(json.dumps(result.summary)) < 2500
    assert result.summary["warnings"] >= 2
    assert result.summary["first"][0]["id"]


# --------------------------------------------------------------------------------------
# sheet frame: detection
# --------------------------------------------------------------------------------------


def test_finds_closed_lwpolyline_frame(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "f.dxf"))], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])
    assert not of(result, "FRAME_NOT_FOUND")


def test_finds_frame_made_of_four_lines(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        for a, b in [((10, 10), (410, 10)), ((410, 10), (410, 287)), ((410, 287), (10, 287))]:
            lay.add_line(a, b)
        lay.add_line((10, 287), (10, 10))

    dxf = make_dxf(tmp_path / "l.dxf", frame=False, build=build)
    result = run_qa([str(dxf)], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])
    assert not of(result, "FRAME_NOT_FOUND")


def test_finds_frame_inside_paper_space_block(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        blk = doc.blocks.new("BORDER")
        rect(blk, (0, 0, 400, 277))
        lay.add_blockref("BORDER", (10, 10))

    dxf = make_dxf(tmp_path / "b.dxf", frame=False, build=build)
    result = run_qa([str(dxf)], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])


def test_prefers_inner_frame_over_paper_outline(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        rect(lay, (0, 0, 420, 297))

    result = run_qa([str(make_dxf(tmp_path / "o.dxf", build=build))], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])


def test_small_rectangle_is_not_a_frame(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        rect(lay, (20, 20, 120, 70))

    result = run_qa([str(make_dxf(tmp_path / "s.dxf", frame=False, build=build))], runs)
    hit = of(result, "FRAME_NOT_FOUND")
    assert [(f["severity"], f["where"]) for f in hit] == [("info", "Sheet-A")]
    assert frames(result)["Sheet-A"] is None
    assert result.exit_code == ExitCode.OK


def test_frame_layer_overrides_heuristic(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        doc.layers.add("TB")
        rect(lay, (20, 20, 120, 70), layer="TB")

    dxf = make_dxf(tmp_path / "t.dxf", build=build)
    assert frames(run_qa([str(dxf)], runs))["Sheet-A"] == pytest.approx([10, 10, 410, 287])
    result = run_qa([str(dxf), "--frame-layer", "tb"], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([20, 20, 120, 70])


def test_frame_layer_is_inherited_from_the_insert_by_layer_zero_content(
    tmp_path: Path, runs: Path
) -> None:
    def build(doc: Any, lay: Any) -> None:
        doc.layers.add("BORDERS")
        blk = doc.blocks.new("BORDER")
        rect(blk, (0, 0, 400, 277), layer="0")
        lay.add_blockref("BORDER", (10, 10), dxfattribs={"layer": "BORDERS"})

    dxf = make_dxf(tmp_path / "i.dxf", frame=False, build=build)
    result = run_qa([str(dxf), "--frame-layer", "BORDERS"], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])


def test_frame_layer_without_a_frame_on_it_is_not_found(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        doc.layers.add("EMPTY-LAYER")

    dxf = make_dxf(tmp_path / "n.dxf", build=build)
    result = run_qa([str(dxf), "--frame-layer", "EMPTY-LAYER"], runs)
    assert [f["severity"] for f in of(result, "FRAME_NOT_FOUND")] == ["info"]


def test_unknown_frame_layer_is_bad_args(tmp_path: Path, runs: Path) -> None:
    with pytest.raises(CadError) as err:
        run_qa([str(make_dxf(tmp_path / "u.dxf")), "--frame-layer", "NOPE"], runs)
    assert err.value.code == "BAD_ARGS"
    assert "FRAME" in (err.value.hint or "")


def test_frame_layer_needs_a_drawing(tmp_path: Path, runs: Path) -> None:
    pdf = make_pdf(tmp_path / "p.pdf", (420, 297))
    with pytest.raises(CadError) as err:
        run_qa(["--pdf", str(pdf), "--frame-layer", "FRAME"], runs)
    assert err.value.code == "BAD_ARGS"


def test_empty_layout_gets_no_frame_finding(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "e.dxf", sheet_content=False))], runs)
    assert not of(result, "FRAME_NOT_FOUND")
    assert of(result, "LAYOUT_EMPTY")


# --------------------------------------------------------------------------------------
# sheet frame: paper and printable area
# --------------------------------------------------------------------------------------


def test_frame_inside_printable_area_is_clean(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "c.dxf", margins=(5, 5, 5, 5)))], runs)
    assert not of(result, "FRAME_OUTSIDE_PAPER")


def test_asymmetric_iso_margins_are_not_flagged(tmp_path: Path, runs: Path) -> None:
    """Margins 20 mm left and 5 mm elsewhere: the printable area is (0,0)-(395,287) in layout
    units, and a frame that fills it exactly is fine."""

    def build(doc: Any, lay: Any) -> None:
        rect(lay, (0, 0, 395, 287), layer="FRAME")

    dxf = make_dxf(tmp_path / "iso.dxf", frame=False, margins=(5, 5, 5, 20), build=build)
    result = run_qa([str(dxf)], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([0, 0, 395, 287])
    assert not of(result, "FRAME_OUTSIDE_PAPER")


def test_frame_beyond_printable_area_is_warning(tmp_path: Path, runs: Path) -> None:
    result = run_qa([str(make_dxf(tmp_path / "w.dxf", margins=(10, 10, 10, 10)))], runs)
    (hit,) = of(result, "FRAME_OUTSIDE_PAPER")
    assert hit["severity"] == "warning" and hit["where"].startswith("Sheet-A:")
    assert "10.0" in hit["message"] and "right" in hit["message"] and "top" in hit["message"]
    assert "within the paper" in hit["message"]
    assert result.exit_code == ExitCode.OK


def test_frame_beyond_the_paper_says_so(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        rect(lay, (-5, 10, 410, 287), layer="FRAME")

    dxf = make_dxf(tmp_path / "b.dxf", frame=False, build=build)
    (hit,) = of(run_qa([str(dxf)], runs), "FRAME_OUTSIDE_PAPER")
    assert "left" in hit["message"] and "beyond the paper" in hit["message"]


def test_add_sheet_layout_is_reported_outside_printable_area(
    fixtures_dir: Path, runs: Path
) -> None:
    """The synthetic sheets have 10 mm margins and a border at 10..410: the border really does
    leave the printable area, so the warning is correct (D9)."""
    result = run_qa([str(fixtures_dir / "sheet_set.dxf"), "--layout", "Sheet-A"], runs)
    hits = [f for f in of(result, "FRAME_OUTSIDE_PAPER") if f["where"].startswith("Sheet-A:")]
    assert len(hits) == 1 and hits[0]["severity"] == "warning"
    assert result.exit_code == ExitCode.OK


# --------------------------------------------------------------------------------------
# text against the frame
# --------------------------------------------------------------------------------------


def test_text_far_outside_frame_is_warning_and_says_estimate(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        lay.add_text("WELL OUTSIDE THE BORDER", height=5).set_placement((395, 100))

    result = run_qa([str(make_dxf(tmp_path / "t.dxf", build=build))], runs)
    (hit,) = of(result, "TEXT_OUTSIDE_FRAME")
    assert hit["severity"] == "warning" and hit["where"].startswith("Sheet-A:")
    assert "estimate" in hit["message"]
    assert result.exit_code == ExitCode.OK


def test_mtext_far_outside_frame_is_reported(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        lay.add_mtext("note " * 10, dxfattribs={"insert": (380, 100), "char_height": 4})

    result = run_qa([str(make_dxf(tmp_path / "m.dxf", build=build))], runs)
    assert len(of(result, "TEXT_OUTSIDE_FRAME")) == 1


def test_short_text_inside_is_clean(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        lay.add_text("inside", height=5).set_placement((100, 100))
        lay.add_mtext("also inside", dxfattribs={"insert": (100, 200), "char_height": 4})

    result = run_qa([str(make_dxf(tmp_path / "i.dxf", build=build))], runs)
    assert findings(result) == []


def test_attrib_outside_frame_is_reported(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        blk = doc.blocks.new("TB")
        blk.add_attdef("NAME", (0, 0), text="x", height=5)
        lay.add_blockref("TB", (395, 100)).add_auto_attribs({"NAME": "VERY LONG VALUE HERE"})

    result = run_qa([str(make_dxf(tmp_path / "a.dxf", build=build))], runs)
    (hit,) = of(result, "TEXT_OUTSIDE_FRAME")
    assert hit["where"].startswith("Sheet-A:")


def test_hidden_or_invisible_text_is_ignored(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        doc.layers.add("OFF").off()
        doc.layers.add("ICE").freeze()
        doc.layers.add("NOPLOT").dxf.plot = 0
        for name in ("OFF", "ICE", "NOPLOT"):
            lay.add_text("outside", height=5, dxfattribs={"layer": name}).set_placement((395, 100))
        blk = doc.blocks.new("TB")
        blk.add_attdef("NAME", (0, 0), text="x", height=5)
        ref = lay.add_blockref("TB", (395, 150)).add_auto_attribs({"NAME": "VALUE VALUE"})
        for attrib in ref.attribs:
            attrib.dxf.flags = 1  # invisible
        lay.add_text("", height=5).set_placement((395, 200))  # empty

    result = run_qa([str(make_dxf(tmp_path / "h.dxf", build=build))], runs)
    assert not of(result, "TEXT_OUTSIDE_FRAME")


def test_text_findings_are_capped_and_the_summary_stays_small(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        for i in range(80):
            # long enough to stick out of the frame whatever font the machine has
            lay.add_text(f"far away {i} " + "X" * 60, height=3).set_placement((400, 10 + i * 3))

    result = run_qa([str(make_dxf(tmp_path / "many.dxf", build=build))], runs)
    assert len(of(result, "TEXT_OUTSIDE_FRAME")) == 50
    assert len(json.dumps(result.summary)) < 2500
    data = json.loads(Path(result.outputs["findings"]["path"]).read_text(encoding="utf-8"))
    assert data["truncated"]["TEXT_OUTSIDE_FRAME"] == 30


# --------------------------------------------------------------------------------------
# text against a baseline
# --------------------------------------------------------------------------------------


def _baseline_pair(tmp_path: Path, new: Callable[[Any, Any], None]) -> tuple[Path, Path]:
    def old_build(doc: Any, lay: Any) -> None:
        lay.add_mtext("short", dxfattribs={"insert": (100, 200), "char_height": 3, "width": 100})
        lay.add_text("AB", height=5).set_placement((20, 100))

    old = make_dxf(tmp_path / "old.dxf", build=old_build)
    return old, derive(old, tmp_path / "new.dxf", new)


def _text_handle(path: Path, kind: str) -> str:
    doc = ezdxf.readfile(path)
    layout = doc.layouts.get("Sheet-A")
    return str(next(e for e in layout.query(kind) if e.dxf.text != "TITLE").dxf.handle)


def _first(lay: Any, kind: str) -> Any:
    return next(e for e in lay.query(kind) if kind == "MTEXT" or e.dxf.text != "TITLE")


def test_mtext_that_wraps_more_is_reported(tmp_path: Path, runs: Path) -> None:
    def change(doc: Any, lay: Any) -> None:
        _first(lay, "MTEXT").text = "word " * 60

    old, new = _baseline_pair(tmp_path, change)
    result = run_qa([str(new), "--baseline", str(old)], runs)
    handle = str(next(iter(ezdxf.readfile(new).layouts.get("Sheet-A").query("MTEXT"))).dxf.handle)
    (hit,) = of(result, "TEXT_WRAPPED")
    assert hit["severity"] == "warning" and hit["where"] == f"Sheet-A:{handle}"
    assert not [f for f in of(result, "TEXT_GREW") if f["where"].endswith(handle)]
    assert result.exit_code == ExitCode.OK


def test_text_grown_far_beyond_tolerance_is_reported(tmp_path: Path, runs: Path) -> None:
    def change(doc: Any, lay: Any) -> None:
        _first(lay, "TEXT").dxf.text = "AB plus a much longer replacement"

    old, new = _baseline_pair(tmp_path, change)
    result = run_qa([str(new), "--baseline", str(old)], runs)
    handles = [f["where"] for f in of(result, "TEXT_GREW")]
    assert f"Sheet-A:{_text_handle(new, 'TEXT')}" in handles
    assert {f["severity"] for f in of(result, "TEXT_GREW")} == {"warning"}


def test_text_that_shrank_is_not_reported(tmp_path: Path, runs: Path) -> None:
    def change(doc: Any, lay: Any) -> None:
        _first(lay, "TEXT").dxf.text = "AB plus a much longer replacement"

    old, new = _baseline_pair(tmp_path, change)
    result = run_qa([str(old), "--baseline", str(new)], runs)  # the baseline is the bigger one
    assert not of(result, "TEXT_GREW") and not of(result, "TEXT_WRAPPED")


def test_unchanged_texts_give_no_findings(tmp_path: Path, runs: Path) -> None:
    old, new = _baseline_pair(tmp_path, lambda doc, lay: None)
    result = run_qa([str(new), "--baseline", str(old)], runs)
    assert findings(result) == []


def test_unrelated_baseline_is_info(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        for i in range(5):
            lay.add_mtext(f"unrelated {i}", dxfattribs={"insert": (100, 50 + i * 10)})

    def current_build(doc: Any, lay: Any) -> None:
        for i in range(4):
            lay.add_text(f"current {i}", height=3).set_placement((100, 50 + i * 10))

    other = make_dxf(tmp_path / "other.dxf", build=build)
    current = make_dxf(tmp_path / "cur.dxf", build=current_build)
    result = run_qa([str(current), "--baseline", str(other)], runs)
    (hit,) = of(result, "BASELINE_MISMATCH")
    assert hit["severity"] == "info" and hit["where"] == "baseline"
    assert not of(result, "TEXT_GREW") and not of(result, "TEXT_WRAPPED")


def test_baseline_without_drawing_is_bad_args(tmp_path: Path, runs: Path) -> None:
    pdf = make_pdf(tmp_path / "p.pdf", (420, 297))
    old = make_dxf(tmp_path / "old.dxf")
    with pytest.raises(CadError) as err:
        run_qa(["--pdf", str(pdf), "--baseline", str(old)], runs)
    assert err.value.code == "BAD_ARGS"


def test_missing_baseline_file_is_reported(tmp_path: Path, runs: Path) -> None:
    with pytest.raises(CadError) as err:
        run_qa([str(make_dxf(tmp_path / "n.dxf")), "--baseline", str(tmp_path / "no.dxf")], runs)
    assert err.value.code == "FILE_NOT_FOUND"


# --------------------------------------------------------------------------------------
# PDF: clipped content, trim outline, shifted plot
# --------------------------------------------------------------------------------------


def pdf_ids(found: list[qa.Finding]) -> set[str]:
    return {f.id for f in found}


def test_frame_touching_edge_is_pdf_clipped_error(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "c.pdf", (420, 297), frame_mm=(0.1, 10, 400, 150))
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    (hit,) = [f for f in found if f.id == "PDF_CLIPPED"]
    assert hit.severity == "error" and hit.where == "c.pdf" and "left" in hit.message


def test_trim_outline_on_page_edge_is_info_not_error(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "t.pdf", (420, 297), frame_mm=(0, 0, 420, 297))
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    assert "PDF_CLIPPED" not in pdf_ids(found)
    (hit,) = [f for f in found if f.id == "PDF_TRIM_OUTLINE"]
    assert hit.severity == "info"


def test_partial_content_on_edge_next_to_trim_outline_is_still_clipped(tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "p.pdf", (420, 297), frame_mm=(0, 0, 420, 297), extra_mm=((0.4, 100, 0.4, 150),)
    )
    assert "PDF_CLIPPED" in pdf_ids(qa.check_pdf(pdf, expected_mm=(420, 297)))


def test_white_page_background_is_not_content(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "bg.pdf", (420, 297), background=True)
    assert qa.check_pdf(pdf, expected_mm=(420, 297)) == []


def test_blank_page_is_not_clipped(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "blank.pdf", (420, 297), draw=False)
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    assert "PDF_CLIPPED" not in pdf_ids(found) and "PDF_EMPTY" in pdf_ids(found)


def test_every_page_is_checked_and_named(tmp_path: Path) -> None:
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    pdf = tmp_path / "two.pdf"
    with PdfPages(pdf) as pages:
        for x1 in (10, -5):  # the second page has content along its left edge
            fig = Figure(figsize=(420 / 25.4, 297 / 25.4))
            ax = fig.add_axes((0, 0, 1, 1))
            ax.set_xlim(0, 420)
            ax.set_ylim(0, 297)
            ax.axis("off")
            for i in range(10):
                ax.plot([40, 400], [20 + i * 20, 20 + i * 20], color="black", linewidth=0.7)
            if x1 < 0:
                ax.plot([0.1, 0.1], [20, 200], color="black", linewidth=0.7)  # along the edge
            pages.savefig(fig)
    found = qa.check_pdf(pdf, expect_pages=2)
    assert [f.where for f in found if f.id == "PDF_CLIPPED"] == ["two.pdf p.2"]


def _shift_run(tmp_path: Path, runs: Path, shift: tuple[float, float], **dxf_args: Any) -> Result:
    dxf = make_dxf(tmp_path / "d.dxf", **dxf_args)
    pdf = make_pdf(tmp_path / "s.pdf", (420, 297), shift_mm=shift)
    return run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Sheet-A"], runs)


def test_shifted_plot_is_warning(tmp_path: Path, runs: Path) -> None:
    result = _shift_run(tmp_path, runs, (8.0, 0.0))
    (hit,) = of(result, "PDF_SHIFTED")
    assert hit["severity"] == "warning" and hit["where"] == "s.pdf"
    assert "dx +8" in hit["message"] and "dy" in hit["message"]
    assert result.exit_code == ExitCode.OK


def test_shift_under_2mm_is_clean(tmp_path: Path, runs: Path) -> None:
    result = _shift_run(tmp_path, runs, (1.0, -1.0))
    assert findings(result) == []


@pytest.mark.parametrize(
    "dxf_args", [{"rotation": 1}, {"scale": (1, 2)}], ids=["rotated", "scaled"]
)
def test_scaled_or_rotated_page_setup_skips_shift_with_info(
    tmp_path: Path, runs: Path, dxf_args: dict[str, Any]
) -> None:
    result = _shift_run(tmp_path, runs, (8.0, 0.0), **dxf_args)
    assert not of(result, "PDF_SHIFTED")
    skipped = of(result, "FRAME_CHECK_SKIPPED")
    assert skipped and {f["severity"] for f in skipped} == {"info"}
    assert any(f["where"] == "s.pdf" for f in skipped)
    assert result.exit_code == ExitCode.OK


def test_shift_check_needs_layout(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(tmp_path / "s.pdf", (420, 297), shift_mm=(8.0, 0.0))
    result = run_qa([str(dxf), "--pdf", str(pdf)], runs)
    assert not of(result, "PDF_SHIFTED")


def test_shift_check_needs_the_frame_to_be_visible_on_the_pdf(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(tmp_path / "nf.pdf", (420, 297), frame_mm=None)
    result = run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Sheet-A"], runs)
    assert not of(result, "PDF_SHIFTED")
    assert any(f["where"] == "nf.pdf" for f in of(result, "FRAME_CHECK_SKIPPED"))


def test_no_shift_check_when_the_page_size_is_wrong(tmp_path: Path, runs: Path) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(tmp_path / "a4.pdf", (210, 297), shift_mm=(8.0, 0.0))
    result = run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Sheet-A"], runs)
    assert "PDF_SIZE" in ids(result) and not of(result, "PDF_SHIFTED")


def test_pdf_edge_checks_need_pillow_but_importing_qa_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def no_pillow(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    pdf = make_pdf(tmp_path / "np.pdf", (420, 297))
    monkeypatch.setattr(builtins, "__import__", no_pillow)
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    assert [f.id for f in found] == ["PDF_UNCHECKED"] and found[0].severity == "info"


def test_summary_stays_under_2500_bytes_with_many_text_findings(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        for i in range(60):
            lay.add_text(f"far away number {i}", height=3).set_placement((380, 10 + i * 4))

    result = run_qa([str(make_dxf(tmp_path / "x.dxf", build=build, units=0, xref=True))], runs)
    assert len(json.dumps(result.summary)) < 2500
    assert len(result.to_json().encode("utf-8")) < 4096


# --------------------------------------------------------------------------------------
# more edges of the drawing checks
# --------------------------------------------------------------------------------------


def test_an_inch_layout_is_judged_in_millimetres(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        lay.page_setup(size=(11, 8.5), margins=(0.25, 0.25, 0.25, 0.25), units="inch")
        rect(lay, (0, 0, 10.5, 8.1), layer="FRAME")

    dxf = make_dxf(tmp_path / "in.dxf", frame=False, sheet_content=False, build=build)
    doc = ezdxf.readfile(dxf)
    doc.layouts.get("Sheet-A").add_text("x", height=0.1)  # content, so the layout is checked
    doc.saveas(dxf)
    result = run_qa([str(dxf)], runs)
    (hit,) = of(result, "FRAME_OUTSIDE_PAPER")
    assert "top 2.5 mm" in hit["message"]


def test_model_space_text_is_never_checked_against_a_frame(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        doc.modelspace().add_text("far from any frame", height=5).set_placement((5000, 5000))

    result = run_qa([str(make_dxf(tmp_path / "m.dxf", build=build))], runs)
    assert findings(result) == []


def test_rotated_page_setup_skips_the_printable_area_check_on_the_drawing(
    tmp_path: Path, runs: Path
) -> None:
    result = run_qa([str(make_dxf(tmp_path / "r.dxf", rotation=1, margins=(10, 10, 10, 10)))], runs)
    assert not of(result, "FRAME_OUTSIDE_PAPER")
    (hit,) = of(result, "FRAME_CHECK_SKIPPED")
    assert hit["severity"] == "info" and hit["where"] == "Sheet-A" and "rotated" in hit["message"]


def test_every_layout_is_checked_on_its_own(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        other = doc.layouts.new("Sheet-B")
        other.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
        other.add_text("no frame here", height=5).set_placement((20, 20))

    result = run_qa([str(make_dxf(tmp_path / "two.dxf", build=build))], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])
    assert frames(result)["Sheet-B"] is None
    assert [f["where"] for f in of(result, "FRAME_NOT_FOUND")] == ["Sheet-B"]


def test_the_frame_check_survives_a_layout_without_paper_limits(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        lay.dxf.limmin = (0, 0)
        lay.dxf.limmax = (0, 0)

    result = run_qa([str(make_dxf(tmp_path / "lim.dxf", build=build))], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])
    assert any("paper limits" in f["message"] for f in of(result, "FRAME_CHECK_SKIPPED"))
    assert result.exit_code == ExitCode.OK


def test_a_pdf_alone_gets_the_edge_checks_but_no_frame_comparison(
    tmp_path: Path, runs: Path
) -> None:
    pdf = make_pdf(tmp_path / "c.pdf", (420, 297), frame_mm=(0.1, 10, 400, 150))
    result = run_qa(["--pdf", str(pdf), "--size", "420x297"], runs)
    assert [f["id"] for f in findings(result)] == ["PDF_CLIPPED"]
    assert result.exit_code == ExitCode.PARTIAL
    assert "frames" not in json.loads(
        Path(result.outputs["findings"]["path"]).read_text(encoding="utf-8")
    )


def test_a_rasterised_page_is_bounded_in_pixels(tmp_path: Path) -> None:
    """A huge page is rendered at a lower resolution, not at 100 dpi."""
    from cadlib import sheet_frame

    pdf = make_pdf(tmp_path / "big.pdf", (4000, 3000), frame_mm=None)
    (page,), total = sheet_frame.pdf_pages_ink(pdf)
    assert total == 1 and page.mask.width * page.mask.height <= sheet_frame.PDF_RASTER_MAX_PIXELS


def test_summary_counts_are_true_even_when_the_listing_is_capped(
    tmp_path: Path, runs: Path
) -> None:
    def build(doc: Any, lay: Any) -> None:
        for i in range(121):
            # long enough to stick out of the frame whatever font the machine has
            lay.add_text(f"far {i} " + "X" * 60, height=2).set_placement((402, 10 + i * 2))

    result = run_qa([str(make_dxf(tmp_path / "c.dxf", build=build))], runs)
    assert result.summary["warnings"] == 121
    assert len(of(result, "TEXT_OUTSIDE_FRAME")) == 50
    assert result.summary["not_listed"] == {"TEXT_OUTSIDE_FRAME": 71}


def test_the_time_limit_covers_pdf_rasterisation(tmp_path: Path) -> None:
    from cadlib import sheet_frame
    from cadlib.util import Deadline

    pdf = make_pdf(tmp_path / "t.pdf", (420, 297))
    expired = Deadline(0.001)
    time.sleep(0.1)  # longer than the clock resolution (about 16 ms on Windows)
    with pytest.raises(CadError) as err:
        sheet_frame.pdf_pages_ink(pdf, deadline=expired)
    assert err.value.code == "TIMEOUT"
    with pytest.raises(CadError) as err2:
        qa.check_pdf(pdf, expected_mm=(420, 297), deadline=expired)
    assert err2.value.code == "TIMEOUT"


def test_a_frame_that_was_not_plotted_is_not_mistaken_for_content(
    tmp_path: Path, runs: Path
) -> None:
    dxf = make_dxf(tmp_path / "d.dxf")
    pdf = make_pdf(
        tmp_path / "nf.pdf",
        (420, 297),
        frame_mm=None,
        extra_mm=((30, 30, 30, 200), (390, 30, 390, 200), (60, 30, 360, 30), (60, 270, 360, 270)),
    )
    result = run_qa([str(dxf), "--pdf", str(pdf), "--layout", "Sheet-A"], runs)
    assert not of(result, "PDF_SHIFTED")
    assert any(f["where"] == "nf.pdf" for f in of(result, "FRAME_CHECK_SKIPPED"))


# --------------------------------------------------------------------------------------
# block expansion limit
# --------------------------------------------------------------------------------------


def _bomb(doc: Any, lay: Any, *, frame_inside: bool = False) -> None:
    """Eight block levels with six INSERTs of the next level each: 6**8 expansions."""
    levels = 8
    for i in range(levels, 0, -1):
        blk = doc.blocks.new(f"L{i}")
        if i == levels:
            blk.add_line((0, 0), (1, 1))
        else:
            for k in range(6):
                blk.add_blockref(f"L{i + 1}", (k, 0))
    lay.add_blockref("L1", (0, 0))


def test_block_expansion_is_bounded_and_reported(tmp_path: Path, runs: Path) -> None:
    import time

    dxf = make_dxf(tmp_path / "bomb.dxf", frame=False, build=_bomb)
    start = time.monotonic()
    result = run_qa([str(dxf), "--timeout", "20"], runs)
    assert time.monotonic() - start < 10.0  # unbounded expansion takes minutes
    hits = [f for f in of(result, "FRAME_CHECK_SKIPPED") if f["where"] == "Sheet-A"]
    assert len(hits) == 1 and hits[0]["severity"] == "info"
    assert "block expansion limit reached (50000 entities)" in hits[0]["message"]
    assert not of(result, "FRAME_NOT_FOUND")
    assert frames(result)["Sheet-A"] is None
    assert result.exit_code == ExitCode.OK


def test_the_expansion_counter_is_shared_by_the_whole_recursion() -> None:
    import time

    from cadlib import sheet_frame

    doc = ezdxf.new("R2018")
    lay = doc.layouts.new("S")
    lay.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
    _bomb(doc, lay)
    setup = sheet_frame.paper_setup(lay)
    budget = sheet_frame.ExpansionBudget()
    start = time.monotonic()
    assert sheet_frame.find_frame(lay, setup, budget=budget) is None
    assert time.monotonic() - start < 8.0  # unbounded expansion takes minutes
    assert budget.exhausted
    assert (
        sheet_frame.MAX_EXPANDED_ENTITIES <= budget.used <= sheet_frame.MAX_EXPANDED_ENTITIES + 100
    )


def test_a_frame_outside_blocks_is_still_found_when_blocks_blow_up(
    tmp_path: Path, runs: Path
) -> None:
    dxf = make_dxf(tmp_path / "both.dxf", build=_bomb)  # the plain frame is there too
    result = run_qa([str(dxf), "--timeout", "20"], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([10, 10, 410, 287])
    assert any("block expansion limit" in f["message"] for f in of(result, "FRAME_CHECK_SKIPPED"))


# --------------------------------------------------------------------------------------
# marks at the page edge are not a cut-off sheet
# --------------------------------------------------------------------------------------


def test_short_marks_through_the_edge_are_info_not_error(tmp_path: Path, runs: Path) -> None:
    pdf = make_pdf(
        tmp_path / "marks.pdf",
        (420, 297),
        frame_mm=(20, 20, 400, 277),
        extra_mm=((-5, 150, 5, 150), (200, -5, 200, 5)),
    )
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    assert "PDF_CLIPPED" not in pdf_ids(found)
    hits = [f for f in found if f.id == "PDF_EDGE_MARKS"]
    assert hits and {f.severity for f in hits} == {"info"} and hits[0].where == "marks.pdf"
    assert "marks touch the page edge" in hits[0].message
    assert "left edge" in hits[0].message and "the sheet itself is not cut" in hits[0].message
    result = run_qa(["--pdf", str(pdf), "--size", "420x297"], runs)
    assert result.exit_code == ExitCode.OK
    assert [f["id"] for f in findings(result)] == ["PDF_EDGE_MARKS"]


@pytest.mark.parametrize(("length", "is_error"), [(4.0, False), (6.0, True)])
def test_the_clipped_threshold_is_a_run_of_ink_along_the_edge(
    tmp_path: Path, length: float, is_error: bool
) -> None:
    from cadlib import sheet_frame

    assert sheet_frame.PDF_CLIPPED_MIN_MM == 5.0
    pdf = make_pdf(
        tmp_path / "run.pdf",
        (420, 297),
        frame_mm=(20, 20, 400, 277),
        extra_mm=((0.1, 100, 0.1, 100 + length),),
    )
    found = pdf_ids(qa.check_pdf(pdf, expected_mm=(420, 297)))
    assert ("PDF_CLIPPED" in found) is is_error
    assert ("PDF_EDGE_MARKS" in found) is (not is_error)


def test_one_mark_through_the_edge_is_reported_as_info(tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "m.pdf", (420, 297), frame_mm=(20, 20, 400, 277), extra_mm=((-5, 150, 5, 150),)
    )
    (hit,) = [f for f in qa.check_pdf(pdf, expected_mm=(420, 297)) if f.id == "PDF_EDGE_MARKS"]
    assert hit.severity == "info"


# --------------------------------------------------------------------------------------
# a stroke that runs far in from the edge is a cut sheet, a short one is a mark
# --------------------------------------------------------------------------------------


def test_a_frame_with_a_side_beyond_the_page_is_clipped(tmp_path: Path, runs: Path) -> None:
    pdf = make_pdf(tmp_path / "cut.pdf", (420, 297), frame_mm=(-5, 10, 400, 287))
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    (hit,) = [f for f in found if f.id == "PDF_CLIPPED"]
    assert hit.severity == "error" and "left" in hit.message and "mm deep" in hit.message
    result = run_qa(["--pdf", str(pdf), "--size", "420x297"], runs)
    assert result.exit_code == ExitCode.PARTIAL


def test_a_cut_page_of_several_is_named(tmp_path: Path) -> None:
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    pdf = tmp_path / "pages.pdf"
    with PdfPages(pdf) as pages:
        for x1 in (30, -5):
            fig = Figure(figsize=(420 / 25.4, 297 / 25.4))
            ax = fig.add_axes((0, 0, 1, 1))
            ax.set_xlim(0, 420)
            ax.set_ylim(0, 297)
            ax.axis("off")
            for i in range(10):
                ax.plot([x1, 400], [20 + i * 20, 20 + i * 20], color="black", linewidth=0.7)
            pages.savefig(fig)
    found = qa.check_pdf(pdf, expect_pages=2)
    assert [f.where for f in found if f.id == "PDF_CLIPPED"] == ["pages.pdf p.2"]


def test_marks_as_deep_as_on_real_plots_stay_info(tmp_path: Path, runs: Path) -> None:
    """Corner marks run 15-19 mm in from the edge at the thickness of a hairline."""
    pdf = make_pdf(
        tmp_path / "deep.pdf",
        (420, 297),
        frame_mm=(20, 20, 400, 277),
        extra_mm=((-3, 150, 16, 150), (200, -3, 200, 19), (417, 50, 423, 50), (60, 294, 60, 300)),
    )
    found = qa.check_pdf(pdf, expected_mm=(420, 297))
    assert "PDF_CLIPPED" not in pdf_ids(found) and "PDF_EDGE_MARKS" in pdf_ids(found)
    assert run_qa(["--pdf", str(pdf), "--size", "420x297"], runs).exit_code == ExitCode.OK


@pytest.mark.parametrize(("depth", "is_error"), [(30.0, False), (70.0, True)])
def test_the_stroke_threshold_is_the_depth_from_the_edge(
    tmp_path: Path, depth: float, is_error: bool
) -> None:
    from cadlib import sheet_frame

    assert sheet_frame.PDF_CLIPPED_STROKE_MM == 50.0
    pdf = make_pdf(
        tmp_path / "depth.pdf",
        (420, 297),
        frame_mm=(20, 20, 400, 277),
        extra_mm=((-3, 100, depth, 100),),  # not at y=150: a diagonal of make_pdf is flat there,
    )
    found = pdf_ids(qa.check_pdf(pdf, expected_mm=(420, 297)))
    assert ("PDF_CLIPPED" in found) is is_error
    assert ("PDF_EDGE_MARKS" in found) is (not is_error)


# --------------------------------------------------------------------------------------
# text that crosses the frame, not text that lies beside it
# --------------------------------------------------------------------------------------


def _drawing_area_sheet(tmp_path: Path, build: Callable[[Any, Any], None]) -> Path:
    """A 36 x 24 in sheet: the frame bounds the drawing area, a title block sits beside it."""

    def setup(doc: Any, lay: Any) -> None:
        lay.page_setup(size=(36, 24), margins=(0, 0, 0, 0), units="inch")
        rect(lay, (0.66, 0.26, 30.38, 22.29), layer="FRAME")
        build(doc, lay)

    return make_dxf(tmp_path / "area.dxf", frame=False, sheet_content=False, build=setup)


def _with_content(path: Path) -> Path:
    doc = ezdxf.readfile(path)
    doc.layouts.get("Sheet-A").add_line((1, 1), (2, 1))  # content, so the layout is checked
    doc.saveas(path)
    return path


def test_text_wholly_beside_the_frame_is_not_reported(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        blk = doc.blocks.new("TB")
        blk.add_attdef("NAME", (0, 0), text="x", height=0.2)
        for i in range(6):
            lay.add_blockref("TB", (32.0, 3.0 + i)).add_auto_attribs({"NAME": f"title value {i}"})
        lay.add_mtext(
            "SIDE NOTE",
            dxfattribs={"insert": (0.3, 5.0), "char_height": 0.1, "rotation": 90},
        )

    dxf = _with_content(_drawing_area_sheet(tmp_path, build))
    result = run_qa([str(dxf)], runs)
    assert frames(result)["Sheet-A"] == pytest.approx([0.66, 0.26, 30.38, 22.29])
    assert not of(result, "TEXT_OUTSIDE_FRAME")


def test_text_crossing_the_frame_edge_is_reported(tmp_path: Path, runs: Path) -> None:
    def build(doc: Any, lay: Any) -> None:
        lay.add_text("CROSSES THE RIGHT EDGE", height=0.3).set_placement((29.0, 10.0))

    result = run_qa([str(_with_content(_drawing_area_sheet(tmp_path, build)))], runs)
    (hit,) = of(result, "TEXT_OUTSIDE_FRAME")
    assert hit["severity"] == "warning" and "estimate" in hit["message"]


def test_text_crossing_the_frame_by_less_than_a_tenth_is_clean(tmp_path: Path, runs: Path) -> None:
    from cadlib import sheet_frame

    text = ezdxf.new("R2018").modelspace().add_text("EDGE", height=0.3)
    text.set_placement((29.0, 10.0))
    box = sheet_frame.text_box(text)
    assert box is not None
    width = box[2] - box[0]  # 5 % of the text sticks out of the frame (right edge 30.38)

    def build(d: Any, lay: Any) -> None:
        lay.add_text("EDGE", height=0.3).set_placement((30.38 - width * 0.95, 10.0))

    result = run_qa([str(_with_content(_drawing_area_sheet(tmp_path, build)))], runs)
    assert not of(result, "TEXT_OUTSIDE_FRAME")
