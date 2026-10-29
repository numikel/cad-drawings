"""conventions: counts over several drawings, folder sampling, skipped files, small output."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import ezdxf
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import conventions, convert, dxf
from cadlib.result import ERROR_CODES, MAX_JSON_BYTES, CadError, ExitCode, Result

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TRIO = ("sheet_set.dxf", "plan_v2.dxf", "plan_cm_v1.dxf")


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture
def no_converters(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DWG backend at all: nothing is detected, nothing is started."""
    monkeypatch.setattr(convert, "detect_converters", lambda *_a, **_k: [])


def run_conventions(argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = conventions.COMMANDS["conventions"]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def report(result: Result) -> dict[str, Any]:
    return json.loads(  # type: ignore[no-any-return]
        Path(result.outputs["conventions"]["path"]).read_text(encoding="utf-8")
    )


def entry(entries: list[dict[str, Any]], **match: Any) -> dict[str, Any]:
    found = [e for e in entries if all(e.get(k) == v for k, v in match.items())]
    assert len(found) == 1, f"{match} not unique in {entries}"
    return found[0]


def copy_all(fixtures_dir: Path, dest: Path, names: tuple[str, ...] = TRIO) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in names:
        target = dest / name
        shutil.copy2(fixtures_dir / name, target)
        out.append(target)
    return out


# --------------------------------------------------------------------------------------
# aggregates
# --------------------------------------------------------------------------------------


def test_conventions_aggregates_counts_over_files(fixtures_dir: Path, runs: Path) -> None:
    files = [str(fixtures_dir / n) for n in TRIO]
    result = run_conventions(files, runs)
    assert result.exit_code == ExitCode.OK
    assert result.summary["sampled"] == 3
    assert result.summary["skipped"] == 0
    assert result.summary["not_sampled"] == 0

    agg = report(result)["aggregates"]
    # units: two millimetre files, one centimetre file
    mm = entry(agg["units"], insunits="mm")
    assert (mm["count"], mm["of"]) == (2, 3)
    cm = entry(agg["units"], insunits="cm")
    assert (cm["count"], cm["of"]) == (1, 3)
    # layouts: 2 + 2 + 1 paper layouts, all A3 landscape
    a3 = agg["paper_sizes"][0]
    assert a3["name"] == "A3" and a3["size_mm"] == [420, 297]
    assert (a3["count"], a3["of"]) == (5, 5)
    # a layer present in every file, a text height present in two of three
    layer = entry(agg["layers"], name="Defpoints")
    assert (layer["count"], layer["of"]) == (3, 3)
    h35 = entry(agg["text_heights"], height=3.5)
    assert (h35["count"], h35["of"]) == (2, 3)
    style = entry(agg["text_styles"], name="Standard")
    assert (style["count"], style["of"]) == (3, 3) and style["font"] == "txt"
    prefix = entry(agg["layer_prefixes"], prefix="TB")
    assert (prefix["count"], prefix["of"]) == (3, 3)


def test_every_aggregate_entry_has_count_and_of(fixtures_dir: Path, runs: Path) -> None:
    result = run_conventions([str(fixtures_dir / n) for n in TRIO], runs)
    agg = report(result)["aggregates"]
    assert set(agg) == set(conventions.AGGREGATES)  # a stable shape, empty tables included
    for name in ("units", "paper_sizes", "text_styles", "text_heights", "layers"):
        assert agg[name], name
    for name, entries in agg.items():
        for e in entries:
            assert 1 <= e["count"] <= e["of"], (name, e)
        counts = [e["count"] for e in entries]
        assert counts == sorted(counts, reverse=True), name


def test_report_lists_every_file(fixtures_dir: Path, runs: Path) -> None:
    result = run_conventions([str(fixtures_dir / n) for n in TRIO], runs)
    files = report(result)["files"]
    assert sorted(f["name"] for f in files) == sorted(TRIO)
    assert all(f["ok"] and f["error"] is None for f in files)


# --------------------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------------------


def test_conventions_accepts_a_folder_newest_first_with_max_files(
    fixtures_dir: Path, runs: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "issued"
    paths = copy_all(fixtures_dir, folder)
    for age, path in zip((300, 100, 200), paths, strict=True):  # plan_cm_v1 newest of the three
        stamp = 1_700_000_000 + 1_000 - age
        os.utime(path, (stamp, stamp))
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")

    result = run_conventions([str(folder), "--max-files", "2"], runs)
    assert result.summary["sampled"] == 2
    assert result.summary["not_sampled"] == 1
    names = [f["name"] for f in report(result)["files"]]
    # mtime order: plan_v2 (age 100) first, then plan_cm_v1 (age 200); sheet_set (age 300) is cut
    assert names == ["plan_v2.dxf", "plan_cm_v1.dxf"]
    assert result.exit_code == ExitCode.OK


def test_folder_is_not_recursive_unless_asked(
    fixtures_dir: Path, runs: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "tree"
    copy_all(fixtures_dir, folder, ("sheet_set.dxf",))
    copy_all(fixtures_dir, folder / "older", ("plan_v2.dxf",))
    flat = run_conventions([str(folder)], runs)
    assert flat.summary["sampled"] == 1
    deep = run_conventions([str(folder), "--recursive"], runs)
    assert deep.summary["sampled"] == 2


def test_non_ascii_paths_work(fixtures_dir: Path, runs: Path, tmp_path: Path) -> None:
    folder = tmp_path / "zażółć gęślą"
    copy_all(fixtures_dir, folder, ("sheet_set.dxf",))
    shutil.copy2(fixtures_dir / "plan_v2.dxf", folder / "rysunek żółty.dxf")
    result = run_conventions([str(folder)], runs)
    assert result.summary["sampled"] == 2
    assert "rysunek żółty.dxf" in [f["name"] for f in report(result)["files"]]
    json.loads(result.to_json())


def test_files_and_folders_can_be_mixed_without_duplicates(
    fixtures_dir: Path, runs: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mix"
    paths = copy_all(fixtures_dir, folder, ("sheet_set.dxf", "plan_v2.dxf"))
    result = run_conventions([str(folder), str(paths[0])], runs)
    assert result.summary["sampled"] == 2


# --------------------------------------------------------------------------------------
# skipped files
# --------------------------------------------------------------------------------------


def test_conventions_skips_dwg_without_converter_with_warning(
    fixtures_dir: Path, runs: Path, tmp_path: Path, no_converters: None
) -> None:
    folder = tmp_path / "mixed"
    copy_all(fixtures_dir, folder, ("sheet_set.dxf",))
    (folder / "site.dwg").write_bytes(b"AC1032 not a real drawing")

    result = run_conventions([str(folder)], runs)
    assert result.exit_code == ExitCode.PARTIAL
    assert result.summary["sampled"] == 1 and result.summary["skipped"] == 1
    assert any("site.dwg" in w for w in result.warnings)
    skipped = [f for f in report(result)["files"] if not f["ok"]]
    assert [f["name"] for f in skipped] == ["site.dwg"]
    assert skipped[0]["error"]["code"] == "NO_BACKEND"


def test_unreadable_dxf_is_skipped_with_a_warning(
    fixtures_dir: Path, runs: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "broken"
    copy_all(fixtures_dir, folder, ("plan_v2.dxf",))
    (folder / "garbage.dxf").write_bytes(b"\x00\x01\x02 this is not a drawing")
    result = run_conventions([str(folder)], runs)
    assert result.exit_code == ExitCode.PARTIAL
    assert result.summary["sampled"] == 1 and result.summary["skipped"] == 1
    assert any("garbage.dxf" in w for w in result.warnings)


def test_all_files_unreadable_raises_a_registered_error(
    runs: Path, tmp_path: Path, no_converters: None
) -> None:
    folder = tmp_path / "dwgs"
    folder.mkdir()
    (folder / "a.dwg").write_bytes(b"AC1032 a")
    (folder / "b.dwg").write_bytes(b"AC1032 b")
    with pytest.raises(CadError) as excinfo:
        run_conventions([str(folder)], runs)
    assert excinfo.value.code == "NO_BACKEND" and excinfo.value.code in ERROR_CODES
    assert excinfo.value.exit_code == ExitCode.MISSING_DEPENDENCY


class _FakeCad:
    """Stands in for an AcadSession: export_dxf copies a fixture DXF; counts starts and quits."""

    starts = 0
    quits = 0

    def __init__(self, dxf: Path) -> None:
        self.dxf = dxf
        self.warnings: list[str] = []

    def __enter__(self) -> _FakeCad:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        _FakeCad.quits += 1

    def export_dxf(self, src: Path, dst: Path) -> list[str]:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.dxf, dst)
        return []


def test_allow_com_starts_one_cad_session_for_all_dwgs(
    fixtures_dir: Path, runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "dwgs"
    folder.mkdir()
    for name in ("a", "b", "c"):
        (folder / f"{name}.dwg").write_bytes(f"AC1032 {name}".encode())
    monkeypatch.setattr(convert, "detect_converters", lambda *_a, **_k: [convert.ComConverter()])
    _FakeCad.starts = _FakeCad.quits = 0

    def start(timeout: float) -> _FakeCad:
        _FakeCad.starts += 1
        return _FakeCad(fixtures_dir / "sheet_set.dxf")

    monkeypatch.setattr(conventions, "_start_cad", start)
    result = run_conventions([str(folder), "--allow-com"], runs)
    assert result.exit_code == ExitCode.OK and result.summary["sampled"] == 3
    assert (_FakeCad.starts, _FakeCad.quits) == (1, 1)  # one CAD start for the whole run


def test_without_a_dwg_to_convert_no_cad_is_started(
    fixtures_dir: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = []
    monkeypatch.setattr(conventions, "_start_cad", lambda timeout: started.append(1))
    run_conventions([str(fixtures_dir / "sheet_set.dxf"), "--allow-com"], runs)
    assert not started


def test_an_unexpected_error_in_one_file_skips_only_that_file(
    fixtures_dir: Path, runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = copy_all(fixtures_dir, tmp_path / "set")
    real = conventions._read

    def flaky(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name == "plan_v2.dxf":
            raise TypeError("unexpected entity data")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(conventions, "_read", flaky)
    result = run_conventions([str(p) for p in files], runs)
    assert result.exit_code == ExitCode.PARTIAL
    assert result.summary["sampled"] == 2 and result.summary["skipped"] == 1
    skipped = [f for f in report(result)["files"] if not f["ok"]]
    assert skipped[0]["error"]["code"] == "UNREADABLE"


def test_layouts_without_a_paper_size_are_counted(
    fixtures_dir: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = conventions.facts_from_info

    def no_size(info: dict[str, Any]) -> Any:
        facts = real(info)
        facts.layouts_without_size += len(facts.paper_sizes)
        facts.paper_sizes = []
        return facts

    monkeypatch.setattr(conventions, "facts_from_info", no_size)
    result = run_conventions([str(fixtures_dir / "sheet_set.dxf")], runs)
    assert result.summary["layouts_without_paper_size"] == 2
    assert report(result)["layouts_without_paper_size"] == 2


def test_all_files_corrupt_is_unreadable(runs: Path, tmp_path: Path) -> None:
    (tmp_path / "a.dxf").write_bytes(b"\x00\x01 nope")
    with pytest.raises(CadError) as excinfo:
        run_conventions([str(tmp_path / "a.dxf")], runs)
    assert excinfo.value.code == "UNREADABLE"
    assert excinfo.value.exit_code == ExitCode.ERROR


# --------------------------------------------------------------------------------------
# bad arguments
# --------------------------------------------------------------------------------------


def test_conventions_missing_path_is_bad_args(runs: Path, tmp_path: Path) -> None:
    with pytest.raises(CadError) as excinfo:
        run_conventions([str(tmp_path / "nope.dxf")], runs)
    assert excinfo.value.code == "FILE_NOT_FOUND"
    assert excinfo.value.exit_code == ExitCode.BAD_ARGS


def test_empty_folder_is_reported_as_not_found(runs: Path, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "readme.txt").write_text("x", encoding="utf-8")
    with pytest.raises(CadError) as excinfo:
        run_conventions([str(tmp_path / "empty")], runs)
    assert excinfo.value.code == "FILE_NOT_FOUND"
    assert "no DXF or DWG" in excinfo.value.message


def test_unsupported_file_type_and_bad_max_files(
    fixtures_dir: Path, runs: Path, tmp_path: Path
) -> None:
    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    with pytest.raises(CadError) as excinfo:
        run_conventions([str(txt)], runs)
    assert excinfo.value.code == "UNSUPPORTED"
    with pytest.raises(CadError) as excinfo:
        run_conventions([str(fixtures_dir / "plan_v2.dxf"), "--max-files", "0"], runs)
    assert excinfo.value.code == "BAD_ARGS"


# --------------------------------------------------------------------------------------
# size of the answer
# --------------------------------------------------------------------------------------


def test_conventions_summary_fits_in_4_kb(runs: Path, tmp_path: Path) -> None:
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(30):
        doc = ezdxf.new("R2018", setup=True)
        doc.header["$INSUNITS"] = 4 if i % 2 else 5
        for j in range(120):
            doc.layers.add(f"LONG-LAYER-NAME-NUMBER-{i:02d}-{j:03d}-" + "x" * 30)
        for j in range(15):
            doc.styles.add(f"Style_{i}_{j}_" + "s" * 25, font="arial.ttf")
        msp = doc.modelspace()
        for j in range(12):
            msp.add_text(f"t{j}", height=1.0 + i * 0.37 + j, dxfattribs={"layer": "0"})
        for j in range(5):
            doc.layouts.new(f"Sheet-{i:02d}-{j}")
        doc.saveas(folder / f"drawing-{i:02d}-{'n' * 40}.dxf")
    for i in range(10):  # unreadable ones: many warnings
        (folder / f"bad{i}-{'b' * 60}.dxf").write_bytes(b"\x00junk")
    result = run_conventions([str(folder), "--max-files", "40"], runs)
    # the command fits its own summary; to_json() must not have to shrink anything
    assert len(json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8")) <= MAX_JSON_BYTES
    text = result.to_json()
    assert len(text.encode("utf-8")) <= MAX_JSON_BYTES
    assert json.loads(text) == json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert result.summary["top"]["layers"], "the summary keeps at least one entry per table"
    data = json.loads(text)
    assert "truncated" not in data["summary"]
    assert data["summary"]["sampled"] == 30 and data["summary"]["skipped"] == 10
    assert data["exit_code"] == int(ExitCode.PARTIAL)
    # the full tables are in the report, not in the summary
    assert len(report(result)["aggregates"]["layers"]) > 100
    assert data["outputs"]["conventions"]["path"].endswith("conventions.json")


# --------------------------------------------------------------------------------------
# info stays as it was
# --------------------------------------------------------------------------------------


def test_info_output_unchanged(fixtures_dir: Path, runs: Path) -> None:
    parser = argparse.ArgumentParser()
    dxf.COMMANDS["info"].add_arguments(parser)
    args = parser.parse_args([str(fixtures_dir / "sheet_set.dxf"), "--run-dir", str(runs)])
    result = dxf.COMMANDS["info"].run(args)
    assert result.exit_code == ExitCode.OK
    assert set(result.summary) == {
        "file",
        "dxf_version",
        "insunits",
        "measurement",
        "layouts",
        "viewports",
        "viewports_status_unreliable",
        "layers",
        "blocks",
        "xrefs",
        "lock_files",
    }
    assert set(result.outputs) == {"info"}
    info_args = {a.dest for a in parser._actions}
    assert "conventions" in info_args and "files" not in info_args and "max_files" not in info_args


def test_conventions_is_registered_for_cad_py() -> None:
    import cadlib

    assert "conventions" in cadlib.MODULES
    assert cadlib.COMMAND_MODULES["conventions"] == "conventions"


def test_fit_shrinks_the_top_lists_until_the_summary_fits() -> None:
    tables = {
        name: [{"name": f"{name}-{i}-" + "x" * 80, "count": 20 - i, "of": 20} for i in range(20)]
        for name in conventions.AGGREGATES
    }
    result = Result("conventions", run_dir="r" * 200, log="l" * 200)
    result.outputs["conventions"] = {"path": "p" * 200, "bytes": 1, "mtime": "t", "sha1": "s" * 40}
    conventions._fit(result, {"sampled": 20, "skipped": 0, "not_sampled": 0}, tables)
    assert len(json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8")) <= MAX_JSON_BYTES
    assert result.summary["top"]["layers"], "at least one entry is kept per table"
    assert len(result.summary["top"]["layers"]) < conventions.SUMMARY_TOP
