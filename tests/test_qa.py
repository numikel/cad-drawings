"""qa: findings with severity on a drawing and on a plotted PDF; exit 7 only for errors."""

from __future__ import annotations

import argparse
import json
import sys
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
) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0))
    msp.add_line((0, 0), (0, 1000))
    lay = doc.layouts.new("Sheet-A")
    doc.layouts.delete("Layout1")  # the empty default layout would be (rightly) reported
    lay.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
    vp = lay.add_viewport(
        center=(210, 148), size=(400, 280), view_center_point=(500, 500), view_height=1000
    )
    vp.dxf.status = vp_status
    if sheet_content:
        lay.add_line((5, 5), (415, 5))
        lay.add_text("TITLE", height=5).set_placement((10, 10))
    if xref:
        doc.add_xref_def("missing/underlay-base.dxf", "UNDERLAY")
    doc.saveas(path)
    return path


def make_pdf(
    path: Path, size_mm: tuple[float, float], *, draw: bool = True, text: str | None = None
) -> Path:
    import matplotlib
    from matplotlib.figure import Figure

    with matplotlib.rc_context({"pdf.fonttype": 42}):
        fig = Figure(figsize=(size_mm[0] / 25.4, size_mm[1] / 25.4))
        if draw:
            ax = fig.add_axes((0, 0, 1, 1))
            for i in range(30):
                ax.plot([0, 1], [i / 30, 1 - i / 30], color="black")
            ax.axis("off")
        if text:
            fig.text(0.5, 0.5, text, fontsize=20)
        fig.savefig(str(path), format="pdf")
    return path


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
