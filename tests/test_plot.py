"""plot.py: consent, flag parsing, layout choice, page setup mapping, --dest, partial failure.

Unit tests use a fake session injected through ``plot._session_factory``; the single real-CAD test
is marked ``com`` and plots the synthetic sheet set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import ezdxf
import pytest
from typing_extensions import Self

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import plot
from cadlib.result import CadError, ExitCode, Result

NON_ASCII = "zażółć gęślą jaźń"


def run_plot(argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = plot.COMMANDS["plot"]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def make_pdf(path: Path, width_mm: float = 420.0, height_mm: float = 297.0, pages: int = 1) -> None:
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    with PdfPages(path) as pdf:
        for _ in range(pages):
            figure = Figure(figsize=(width_mm / 25.4, height_mm / 25.4))
            axes = figure.add_axes((0.1, 0.1, 0.5, 0.5))
            axes.set_facecolor("black")
            pdf.savefig(figure)


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


class FakeSession:
    """Stands in for ``acad.AcadSession``: writes a PDF per plotted layout, starts nothing."""

    def __init__(self) -> None:
        self.plotted: list[tuple[Path, str, Path, dict[str, Any] | None]] = []
        self.warnings: list[str] = ["session warning"]
        self.fail: dict[str, CadError] = {}
        self.pages: dict[str, int] = {}
        self.skip_write: set[str] = set()
        self.returned: list[str] = ["page setup fallback: media chosen"]
        self.on_plot: Any = None
        self.exited = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def plot_layout_pdf(
        self, src: Path, layout: str, dst: Path, *, page_setup: dict[str, Any] | None = None
    ) -> list[str]:
        self.plotted.append((src, layout, dst, page_setup))
        if self.on_plot is not None:
            self.on_plot(layout)
        if layout in self.fail:
            raise self.fail[layout]
        if layout not in self.skip_write:
            make_pdf(dst, pages=self.pages.get(layout, 1))
        return list(self.returned)


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    fake = FakeSession()
    monkeypatch.setattr(plot, "_session_factory", lambda ctx: fake)
    return fake


@pytest.fixture
def no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(ctx: Any) -> Any:
        raise AssertionError("no CAD session may be created here")

    monkeypatch.setattr(plot, "_session_factory", refuse)


@pytest.fixture
def sheet(fixtures_dir: Path, tmp_path: Path) -> Path:
    """A private copy of the synthetic sheet set (tests compare hashes of it)."""
    target = tmp_path / "src" / "sheet_set.dxf"
    target.parent.mkdir()
    target.write_bytes((fixtures_dir / "sheet_set.dxf").read_bytes())
    return target


# --------------------------------------------------------------------------------------
# consent and arguments
# --------------------------------------------------------------------------------------


def test_without_consent_nothing_starts_and_the_hint_says_to_ask(
    sheet: Path, runs: Path, no_session: None
) -> None:
    with pytest.raises(CadError) as err:
        run_plot([str(sheet)], runs)
    assert err.value.code == "NO_BACKEND" and err.value.exit_code == ExitCode.MISSING_DEPENDENCY
    assert "ask the user" in (err.value.hint or "") and "--allow-com" in (err.value.hint or "")


@pytest.mark.parametrize(
    "flags",
    [
        ["--scale", "1-50"],
        ["--scale", "0:1"],
        ["--scale", "abc"],
        ["--scale", "1:"],
        ["--scale=-1:50"],
        ["--rotate", "45"],
        ["--window", "1,2,3"],
        ["--window", "a,b,c,d"],
        ["--window", "0,0,0,10"],
        ["--window", "0,0,10,10", "--area", "extents"],
        ["--area", "window"],
        ["--area", "bogus"],
    ],
)
def test_bad_flags_are_bad_args_before_cad_is_involved(
    flags: list[str], sheet: Path, runs: Path, no_session: None
) -> None:
    with pytest.raises(CadError) as err:
        run_plot([str(sheet), "--allow-com", *flags], runs)
    assert err.value.code == "BAD_ARGS" and err.value.exit_code == ExitCode.BAD_ARGS


def test_missing_file_is_file_not_found(tmp_path: Path, runs: Path, no_session: None) -> None:
    with pytest.raises(CadError) as err:
        run_plot([str(tmp_path / "nope.dxf"), "--allow-com"], runs)
    assert err.value.code == "FILE_NOT_FOUND"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("fit", "fit"), ("FIT", "fit"), ("1:50", "1:50"), ("2:1", "2:1"), (" 1:0.5 ", "1:0.5")],
)
def test_parse_scale(text: str, expected: str) -> None:
    assert plot.parse_scale(text) == expected


def test_parse_window_orders_the_corners() -> None:
    assert plot.parse_window("10,20,0,5") == (0.0, 5.0, 10.0, 20.0)


# --------------------------------------------------------------------------------------
# layouts, staged copy, outputs
# --------------------------------------------------------------------------------------


def test_default_layouts_staged_copy_outputs_and_report(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    before = sha1(sheet)
    res = run_plot([str(sheet), "--allow-com"], runs)
    assert res.exit_code == ExitCode.OK and res.backend == "com"
    assert [p[1] for p in session.plotted] == ["Sheet-A", "Sheet-B"]
    run_dir = Path(res.run_dir or "")
    for src, _layout, dst, setup in session.plotted:
        assert src != sheet.resolve() and src.name == sheet.name and run_dir in src.parents
        assert dst.parent == run_dir and setup is None  # no flags: the layout's own setup
    assert session.exited
    assert sha1(sheet) == before
    for layout in ("Sheet-A", "Sheet-B"):
        out = res.outputs[layout]
        assert Path(out["path"]).name == f"sheet_set__{layout}.pdf"
        assert "source_mtime" in out and out["bytes"] > 100
    report = json.loads(Path(res.outputs["plot.json"]["path"]).read_text("utf-8"))
    assert [p["layout"] for p in report["plots"]] == ["Sheet-A", "Sheet-B"]
    first = report["plots"][0]
    assert first["page_mm"] == pytest.approx([420, 297], abs=1.5)
    assert {"device", "media", "scale", "rotation", "warnings", "pdf"} <= set(first)
    assert res.summary["count"] == 2 and res.summary["failed"] == []


def test_empty_layouts_are_skipped_with_a_warning(
    sheet: Path, runs: Path, session: FakeSession, tmp_path: Path
) -> None:
    doc = ezdxf.readfile(sheet)
    doc.layouts.new("Blank")
    padded = tmp_path / "padded.dxf"
    doc.saveas(padded)
    res = run_plot([str(padded), "--allow-com"], runs)
    assert [p[1] for p in session.plotted] == ["Sheet-A", "Sheet-B"]
    assert any("skipped empty layout 'Blank'" in w for w in res.warnings)
    named = run_plot([str(padded), "--allow-com", "--layout", "Blank"], runs)
    assert named.exit_code == ExitCode.OK and session.plotted[-1][1] == "Blank"


def test_a_drawing_with_nothing_to_plot_is_an_empty_layout_error(
    runs: Path, tmp_path: Path, no_session: None
) -> None:
    path = tmp_path / "empty.dxf"
    ezdxf.new().saveas(path)
    with pytest.raises(CadError) as err:
        run_plot([str(path), "--allow-com"], runs)
    assert err.value.code == "EMPTY_LAYOUT" and err.value.exit_code == ExitCode.PRECONDITION_FAILED


def test_explicit_layouts_are_case_insensitive_and_deduplicated(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    res = run_plot([str(sheet), "--allow-com", "--layout", "sheet-b", "--layout", "Sheet-B"], runs)
    assert [p[1] for p in session.plotted] == ["Sheet-B"]
    assert list(res.outputs) == ["Sheet-B", "plot.json"]


def test_unknown_layout_lists_the_available_ones(sheet: Path, runs: Path, no_session: None) -> None:
    with pytest.raises(CadError) as err:
        run_plot([str(sheet), "--allow-com", "--layout", "Nope"], runs)
    assert err.value.code == "LAYOUT_NOT_FOUND" and "Sheet-A" in (err.value.hint or "")


def test_non_ascii_source_path(
    sheet: Path, runs: Path, session: FakeSession, tmp_path: Path
) -> None:
    odd = tmp_path / NON_ASCII / f"{NON_ASCII}.dxf"
    odd.parent.mkdir()
    odd.write_bytes(sheet.read_bytes())
    res = run_plot([str(odd), "--allow-com", "--layout", "Sheet-A"], runs)
    assert Path(res.outputs["Sheet-A"]["path"]).name == f"{NON_ASCII}__Sheet-A.pdf"


# --------------------------------------------------------------------------------------
# page setup mapping
# --------------------------------------------------------------------------------------


def test_flags_map_to_the_page_setup_keys(sheet: Path, runs: Path, session: FakeSession) -> None:
    run_plot(
        [
            str(sheet), "--allow-com", "--layout", "Sheet-A",
            "--device", "DWG To PDF.pc3", "--media", "ISO_A3_(420.00_x_297.00_MM)",
            "--area", "window", "--window", "10,20,0,5", "--scale", "1:50",
            "--rotate", "90", "--style-sheet", "mono.ctb",
        ],
        runs,
    )  # fmt: skip
    assert session.plotted[0][3] == {
        "device": "DWG To PDF.pc3",
        "media": "ISO_A3_(420.00_x_297.00_MM)",
        "plot_area": "window",
        "window": (0.0, 5.0, 10.0, 20.0),
        "scale": "1:50",
        "rotation": 90,
        "style_sheet": "mono.ctb",
    }
    from cadlib import acad

    assert set(session.plotted[0][3] or {}) <= acad._PAGE_SETUP_KEYS


def test_a_window_alone_implies_the_window_area_and_fit_is_passed(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    run_plot([str(sheet), "--allow-com", "--layout", "Sheet-A", "--window", "0,0,10,10"], runs)
    assert session.plotted[0][3] == {"plot_area": "window", "window": (0.0, 0.0, 10.0, 10.0)}
    run_plot([str(sheet), "--allow-com", "--layout", "Sheet-A", "--scale", "fit"], runs)
    assert session.plotted[1][3] == {"scale": "fit"}


def test_report_records_requested_setup(sheet: Path, runs: Path, session: FakeSession) -> None:
    res = run_plot(
        [str(sheet), "--allow-com", "--layout", "Sheet-A", "--scale", "1:100", "--rotate", "180"],
        runs,
    )
    report = json.loads(Path(res.outputs["plot.json"]["path"]).read_text("utf-8"))
    entry = report["plots"][0]
    assert entry["scale"] == "1:100" and entry["rotation"] == 180
    assert entry["device"] is None and entry["media"] is None  # the layout's own setup


# --------------------------------------------------------------------------------------
# warnings
# --------------------------------------------------------------------------------------


def test_warnings_from_session_plots_and_the_viewer_notice_reach_the_result(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    from cadlib.acad import VIEWER_WARNING

    res = run_plot([str(sheet), "--allow-com", "--layout", "Sheet-A"], runs)
    assert "session warning" in res.warnings
    assert "page setup fallback: media chosen" in res.warnings
    assert VIEWER_WARNING in res.warnings  # added even when the session did not return it
    report = json.loads(Path(res.outputs["plot.json"]["path"]).read_text("utf-8"))
    assert "page setup fallback: media chosen" in report["plots"][0]["warnings"]


def test_a_cached_conversion_warning_is_kept(
    sheet: Path, runs: Path, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import drawing

    real = drawing.open_drawing

    def with_warning(*args: Any, **kwargs: Any) -> Any:
        loaded = real(*args, **kwargs)
        loaded.warnings.append("CACHED_CONVERSION: DXF taken from the conversion cache")
        return loaded

    monkeypatch.setattr(plot, "open_drawing", with_warning)
    res = run_plot([str(sheet), "--allow-com"], runs)
    assert any(w.startswith("CACHED_CONVERSION") for w in res.warnings)


# --------------------------------------------------------------------------------------
# --dest
# --------------------------------------------------------------------------------------


def test_dest_copies_the_pdfs_atomically(sheet: Path, runs: Path, session: FakeSession) -> None:
    dest = sheet.parent / "deliver" / "pdf"
    res = run_plot([str(sheet), "--allow-com", "--dest", str(dest)], runs)
    assert sorted(p.name for p in dest.iterdir()) == [
        "sheet_set__Sheet-A.pdf",
        "sheet_set__Sheet-B.pdf",
    ]
    for layout in ("Sheet-A", "Sheet-B"):
        assert sha1(dest / f"sheet_set__{layout}.pdf") == res.outputs[layout]["sha1"]
    assert res.summary["dest"]["dir"] == str(dest)
    assert len(res.summary["dest"]["files"]) == 2


def test_existing_dest_file_without_overwrite_changes_nothing(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    dest = sheet.parent / "deliver"
    dest.mkdir()
    keep = dest / "sheet_set__Sheet-B.pdf"
    keep.write_bytes(b"precious")
    with pytest.raises(CadError) as err:
        run_plot([str(sheet), "--allow-com", "--dest", str(dest)], runs)
    assert err.value.code == "EXISTS" and err.value.exit_code == ExitCode.PRECONDITION_FAILED
    assert keep.read_bytes() == b"precious"
    assert [p.name for p in dest.iterdir()] == [keep.name]  # not even Sheet-A was copied
    assert not session.plotted  # refused before CAD was started


def test_overwrite_replaces_dest_files(sheet: Path, runs: Path, session: FakeSession) -> None:
    dest = sheet.parent / "deliver"
    dest.mkdir()
    old = dest / "sheet_set__Sheet-B.pdf"
    old.write_bytes(b"old")
    res = run_plot([str(sheet), "--allow-com", "--dest", str(dest), "--overwrite"], runs)
    assert sha1(old) == res.outputs["Sheet-B"]["sha1"]
    assert not [p for p in dest.iterdir() if p.name.endswith(".tmp")]


def test_dest_that_is_a_file_is_bad_args(sheet: Path, runs: Path, no_session: None) -> None:
    with pytest.raises(CadError) as err:
        run_plot([str(sheet), "--allow-com", "--dest", str(sheet)], runs)
    assert err.value.code == "BAD_ARGS"


# --------------------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------------------


def test_one_failed_layout_is_a_partial_result_with_the_good_pdfs(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    session.fail["Sheet-B"] = CadError("PLOT_FAILED", "the plotter stopped")
    dest = sheet.parent / "deliver"
    res = run_plot([str(sheet), "--allow-com", "--dest", str(dest)], runs)
    assert res.exit_code == ExitCode.PARTIAL
    assert "Sheet-A" in res.outputs and "Sheet-B" not in res.outputs
    assert res.summary["failed"] == [
        {"layout": "Sheet-B", "code": "PLOT_FAILED", "message": "the plotter stopped"}
    ]
    assert [e["code"] for e in res.errors] == ["PLOT_FAILED"]
    assert [p.name for p in dest.iterdir()] == ["sheet_set__Sheet-A.pdf"]
    report = json.loads(Path(res.outputs["plot.json"]["path"]).read_text("utf-8"))
    assert report["failed"][0]["layout"] == "Sheet-B"
    assert session.exited


def test_all_layouts_failing_raises_the_first_error(
    sheet: Path, runs: Path, session: FakeSession
) -> None:
    session.fail["Sheet-A"] = CadError("PLOT_FAILED", "first")
    session.fail["Sheet-B"] = CadError("PLOT_FAILED", "second")
    with pytest.raises(CadError) as err:
        run_plot([str(sheet), "--allow-com"], runs)
    assert err.value.code == "PLOT_FAILED" and err.value.message == "first"
    assert session.exited


@pytest.mark.parametrize("kind", ["two-pages", "missing"])
def test_output_verification_rejects_bad_pdfs(
    kind: str, sheet: Path, runs: Path, session: FakeSession
) -> None:
    if kind == "two-pages":
        session.pages["Sheet-A"] = 2
    else:
        session.skip_write.add("Sheet-A")
    res = run_plot([str(sheet), "--allow-com"], runs)
    assert res.exit_code == ExitCode.PARTIAL
    assert res.summary["failed"][0]["layout"] == "Sheet-A"
    assert res.summary["failed"][0]["code"] == "PLOT_BAD_OUTPUT"
    assert "Sheet-A" not in res.outputs and "Sheet-B" in res.outputs
    assert not Path(session.plotted[0][2]).exists()  # a rejected PDF is not left behind


def test_session_start_failure_propagates(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ctx: Any) -> Any:
        raise CadError("NO_BACKEND", "no CAD found", hint="install one")

    monkeypatch.setattr(plot, "_session_factory", broken)
    with pytest.raises(CadError) as err:
        run_plot([str(sheet), "--allow-com"], runs)
    assert err.value.code == "NO_BACKEND"


def test_the_time_limit_stops_between_layouts_and_keeps_finished_pdfs(
    sheet: Path, runs: Path, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(plot, "_clock", lambda: now[0])
    session.on_plot = lambda layout: now.__setitem__(0, now[0] + 200)
    res = run_plot([str(sheet), "--allow-com", "--timeout", "100"], runs)
    assert res.exit_code == ExitCode.PARTIAL and next(iter(res.outputs)) == "Sheet-A"
    assert [e["code"] for e in res.errors] == ["TIMEOUT"]
    assert [p[1] for p in session.plotted] == ["Sheet-A"] and session.exited


# --------------------------------------------------------------------------------------
# real CAD (run manually, one at a time)
# --------------------------------------------------------------------------------------


def _acad_pids() -> set[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq acad.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].strip('"').lower() == "acad.exe":
            pids.add(int(parts[1]))
    return pids


@pytest.mark.com
@pytest.mark.skipif(sys.platform != "win32", reason="COM automation is Windows only")
def test_real_cad_plots_both_sheets_to_single_page_pdfs(
    fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pypdfium2 as pdfium

    src = tmp_path / "sheet_set.dxf"
    src.write_bytes((fixtures_dir / "sheet_set.dxf").read_bytes())
    before_hash = sha1(src)
    runs = Path(os.environ.get("CAD_DRAWINGS_RUNS") or tmp_path / "runs")
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(runs))
    pids_before = _acad_pids()
    started = time.time()
    res = run_plot(
        [
            str(src), "--allow-com", "--device", "DWG To PDF.pc3",
            "--media", "ISO_A3_(420.00_x_297.00_MM)", "--scale", "fit",
            "--timeout", "300",
        ],
        runs,
    )  # fmt: skip
    assert res.exit_code == ExitCode.OK, res.errors
    for layout in ("Sheet-A", "Sheet-B"):
        out = res.outputs[layout]
        pdf_path = Path(out["path"])
        assert pdf_path.name == f"sheet_set__{layout}.pdf"
        assert pdf_path.stat().st_mtime >= started - 2 and out["bytes"] > 1000
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            assert len(document) == 1
            width_pt, height_pt = document[0].get_size()
        finally:
            document.close()
        assert width_pt / 72 * 25.4 == pytest.approx(420, abs=3)
        assert height_pt / 72 * 25.4 == pytest.approx(297, abs=3)
    assert sha1(src) == before_hash
    deadline = time.time() + 30
    while _acad_pids() - pids_before and time.time() < deadline:
        time.sleep(1)
    assert not (_acad_pids() - pids_before), "a CAD process started by plot is still running"
