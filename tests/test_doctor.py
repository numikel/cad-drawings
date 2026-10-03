"""doctor: capability matrix, schema-valid JSON, read-only detection, converter discovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

import doctor as std  # the standalone, stdlib-only script
from cadlib import doctor as lib_doctor
from cadlib.result import ExitCode

SCHEMA = Draft202012Validator(json.loads((SKILL / "assets/output.schema.json").read_text("utf-8")))
FULL = {
    "ezdxf": "1.4.4",
    "pypdfium2": "5.1.0",
    "pywin32": "312",
    "pillow": "11.0",
    "matplotlib": "3.9",
    "pymupdf": None,
    "jsonschema": "4.0",
}
NOTHING: dict[str, str | None] = dict.fromkeys(FULL)


def make_env(platform: str = "win32", **kw: Any) -> Any:
    kw.setdefault("packages", dict(FULL))
    return std.Environment(platform=platform, python=(3, 13, 1), **kw)


def caps(env: Any) -> dict[str, Any]:
    summary, _ = std.assess(env)
    return summary["capabilities"]


AUTOCAD = {
    "product": "AutoCAD",
    "progid": "AutoCAD.Application.24.3",
    "exe": "x",
    "exe_exists": True,
}


# -- versions -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("found", "minimum", "ok"),
    [
        ("1.4.4", "1.4.4", True),
        ("1.4.3", "1.4.4", False),
        ("1.10.0", "1.4.4", True),
        ("312", "312", True),
        ("311", "312", False),
        ("5.0.0rc1", "5", True),
        ("5", "5.0.1", False),
    ],
)
def test_version_comparison(found: str, minimum: str, ok: bool) -> None:
    assert std.version_at_least(found, minimum) is ok


def test_package_state() -> None:
    assert std.package_state("ezdxf", None) == "missing"
    assert std.package_state("ezdxf", "1.3.0") == "old"
    assert std.package_state("ezdxf", "1.4.4") == "ok"
    assert std.package_state("jsonschema", "0.1") == "ok"  # no minimum


# -- capability matrix ----------------------------------------------------------------------


def test_everything_missing_still_reports_with_install_commands() -> None:
    c = caps(make_env("linux", packages=dict(NOTHING)))
    assert c["read_dxf"]["status"] == "missing"
    assert any("ezdxf" in cmd for cmd in c["read_dxf"]["install"])
    for name in ("read_dwg", "convert", "render", "pdf_to_png"):
        assert c[name]["status"] == "missing", name
        assert c[name]["install"], name
    for name in ("plot_deliverable", "edit_dwg"):
        # shipped commands that need a COM-capable CAD host, not "not implemented"
        assert c[name]["status"] == "missing", name
        assert "COM" in c[name]["note"], name


def test_worst_case_output_fits_in_4_kb() -> None:
    report = std.build_report(env=make_env("linux", packages=dict(NOTHING)))
    assert len(json.dumps(report).encode("utf-8")) <= 4096
    busy = make_env(
        hosts=[AUTOCAD] * 6,
        installs=[{"product": "AutoCAD 2024 - Polish", "version": "R24.3", "key": "ACAD-7101:415"}]
        * 3,
        oda="C:/Program Files/ODA/x/ODAFileConverter.exe",
    )
    assert len(json.dumps(std.build_report(env=busy)).encode("utf-8")) <= 4096


def test_windows_with_a_com_host_and_oda_has_every_shipped_capability() -> None:
    c = caps(make_env(hosts=[AUTOCAD], oda="oda.exe"))
    assert all(
        c[n]["status"] == "available"
        for n in ("read_dxf", "read_dwg", "convert", "render", "pdf_to_png")
    )
    assert {c[n]["status"] for n in ("plot_deliverable", "edit_dwg")} == {"available"}
    assert c["plot_deliverable"]["via"] == "com" and c["edit_dwg"]["via"] == "com"
    assert "--allow-com" in c["plot_deliverable"]["note"]
    assert c["read_dwg"]["via"] == "com" and "install" not in c["read_dwg"]


def test_com_host_without_pywin32_falls_back_and_warns() -> None:
    packages = dict(FULL, pywin32=None)
    summary, warnings = std.assess(make_env(hosts=[AUTOCAD], packages=packages, oda="oda.exe"))
    assert summary["capabilities"]["read_dwg"]["via"] == "oda"
    assert any("pywin32" in w for w in warnings)


def test_registered_host_whose_exe_is_gone_is_not_counted() -> None:
    gone = dict(AUTOCAD, exe_exists=False)
    assert caps(make_env(hosts=[gone]))["read_dwg"]["status"] == "missing"


def test_oda_gives_dwg_reading_without_a_cad() -> None:
    c = caps(make_env(oda="oda.exe"))
    assert (c["read_dwg"]["status"], c["read_dwg"]["via"]) == ("available", "oda")
    assert c["render"]["status"] == "available" and c["render"]["via"] == "ezdxf"
    assert (c["convert"]["status"], c["convert"]["via"]) == ("available", "oda")


def test_libredwg_only_is_degraded_and_proposes_oda() -> None:
    c = caps(make_env("linux", libredwg={"dwg2dxf": "/usr/bin/dwg2dxf"}))
    assert (c["read_dwg"]["status"], c["read_dwg"]["via"]) == ("degraded", "libredwg")
    assert any("ODA" in line for line in c["read_dwg"]["install"])
    assert c["convert"]["status"] == "degraded"  # no dxf2dwg: one direction only
    both = caps(make_env("linux", libredwg={"dwg2dxf": "a", "dxf2dwg": "b"}))
    assert both["convert"]["status"] == "degraded" and "r2004" in both["convert"]["note"]


def test_com_converts_both_ways() -> None:
    c = caps(make_env(hosts=[AUTOCAD]))
    assert (c["convert"]["status"], c["convert"]["via"]) == ("available", "com")


def test_libredwg_dwg2dxf_alone_converts_one_way_only() -> None:
    c = caps(make_env("linux", libredwg={"dwg2dxf": "a"}))
    assert c["convert"]["status"] == "degraded" and c["read_dwg"]["status"] == "degraded"


def test_linux_oda_without_display_or_xvfb_is_not_usable() -> None:
    env = make_env("linux", oda="/usr/bin/ODAFileConverter", display=False, xvfb=None)
    c = caps(env)
    assert c["read_dwg"]["status"] == "missing" and "Xvfb" in c["read_dwg"]["note"]
    assert (
        caps(
            make_env(
                "linux", oda="/usr/bin/ODAFileConverter", display=False, xvfb="/usr/bin/xvfb-run"
            )
        )["read_dwg"]["via"]
        == "oda"
    )


def test_old_ezdxf_is_degraded_with_an_upgrade_command() -> None:
    c = caps(make_env(packages=dict(FULL, ezdxf="1.3.0")))
    assert c["read_dxf"]["status"] == "degraded" and "1.4.4" in c["read_dxf"]["install"][0]
    assert c["read_dwg"]["status"] == "missing"


def test_pdf_to_png_needs_pypdfium2_5() -> None:
    assert (
        caps(make_env(packages=dict(FULL, pypdfium2="4.30.0")))["pdf_to_png"]["status"]
        == "degraded"
    )
    assert caps(make_env(packages=dict(FULL, pypdfium2=None)))["pdf_to_png"]["status"] == "missing"


def test_pywin32_is_not_listed_off_windows() -> None:
    summary, _ = std.assess(make_env("linux"))
    assert "pywin32" not in summary["packages"] and "xvfb" in summary
    summary, _ = std.assess(make_env("win32"))
    assert "pywin32" in summary["packages"]


def test_warnings_for_agpl_macos_window_and_other_hosts() -> None:
    packages = dict(FULL, pymupdf="1.24")
    _, warnings = std.assess(make_env("darwin", packages=packages, oda="/Applications/x"))
    assert any("AGPL" in w for w in warnings) and any("macOS" in w for w in warnings)
    other = dict(AUTOCAD, product="BricsCAD", progid="BricscadApp.AcadApplication")
    _, warnings = std.assess(make_env(hosts=[other]))
    assert any("untested" in w for w in warnings)


def test_install_options_differ_per_os_and_never_contain_a_user_path() -> None:
    win, mac, lin = (std.install_options(p) for p in ("win32", "darwin", "linux"))
    assert mac["libredwg"] == ["brew install libredwg"] and "apt" in lin["libredwg"][0]
    assert "python -m pip" in win["ezdxf"][0] and "python3 -m pip" in lin["ezdxf"][0]
    assert "ODA" in std.converter_install_hint(
        "win32"
    ) and "libredwg" in std.converter_install_hint("darwin")


# -- JSON contract --------------------------------------------------------------------------


def test_live_report_validates_against_the_schema() -> None:
    report = std.build_report()
    SCHEMA.validate(report)
    assert report["exit_code"] == 0 and set(report["summary"]["capabilities"]) >= {
        "read_dxf",
        "read_dwg",
        "render",
        "convert",
        "plot_deliverable",
        "edit_dwg",
    }
    assert "cad_hosts" not in report["summary"]
    assert all(set(i) == {"product", "version", "key"} for i in report["summary"]["cad_installs"])
    assert all(isinstance(p, str) for p in report["summary"]["cad_progids"])
    assert len(json.dumps(report).encode("utf-8")) <= 4096


def test_python_too_old_is_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    report = std.build_report()
    monkeypatch.undo()
    SCHEMA.validate(report)
    assert report["exit_code"] == 3 and report["errors"][0]["code"] == "PYTHON_TOO_OLD"


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SKILL / "scripts/doctor.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_script_prints_one_valid_json_document() -> None:
    proc = run_script()
    assert proc.returncode == 0
    assert len(proc.stdout.strip().splitlines()) == 1
    SCHEMA.validate(json.loads(proc.stdout))


def test_script_bad_args_is_json_exit_2() -> None:
    proc = run_script("--no-such-flag")
    assert proc.returncode == 2
    data = json.loads(proc.stdout)
    SCHEMA.validate(data)
    assert data["errors"][0]["code"] == "BAD_ARGS"


def test_script_imports_with_nothing_installed() -> None:
    """The script must not import anything third-party at module level."""
    code = "import sys; sys.modules['ezdxf'] = None; sys.modules['win32com'] = None; import runpy; runpy.run_path(sys.argv[1], run_name='x')"
    proc = subprocess.run(
        [sys.executable, "-c", code, str(SKILL / "scripts/doctor.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# -- read-only detection --------------------------------------------------------------------


def test_detection_never_installs_or_starts_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        assert argv[0] in ("tasklist", "pgrep", "ps"), argv
        return subprocess.CompletedProcess(argv, 1, "", "")

    def forbidden(*a: Any, **kw: Any) -> None:
        raise AssertionError("no process may be started")

    monkeypatch.setattr(std.subprocess, "run", fake_run)
    monkeypatch.setattr(std.subprocess, "Popen", forbidden)
    monkeypatch.setattr(std, "probe_com_instance", forbidden)
    env = std.gather_environment(probe_com=False)
    std.assess(env)
    assert calls and env.com_probe is None


def test_probe_com_only_runs_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(std, "running_cad_processes", list)
    monkeypatch.setattr(std, "probe_com_instance", lambda: {"started": True, "pid": 7})
    assert std.gather_environment(probe_com=True).com_probe == {"started": True, "pid": 7}


def test_probe_com_instance_with_fakes() -> None:
    class Session:
        pid = 4242

        def __enter__(self) -> Session:  # noqa: PYI034
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    assert std.probe_com_instance(Session)["pid"] == 4242

    def boom() -> None:
        raise RuntimeError("licence server unreachable")

    result = std.probe_com_instance(boom)
    assert result["started"] is False and "licence" in result["error"]


def test_probe_com_instance_reports_a_missing_acad_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cadlib.acad", None)
    assert std.probe_com_instance() == {"started": False, "error": "acad module missing"}


def test_hosts_are_empty_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(std.sys, "platform", "linux")
    assert std.detect_cad_hosts() == [] and std.detect_autocad_installs() == []


def test_live_host_detection_is_well_formed() -> None:
    for host in std.detect_cad_hosts():
        assert set(host) == {"product", "progid", "exe", "exe_exists"}


# -- converter discovery --------------------------------------------------------------------


def make_oda(root: Path, version: str) -> Path:
    exe = root / "ODA" / f"ODA File Converter {version}" / "ODAFileConverter.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


def test_oda_in_a_versioned_folder_highest_version_wins(tmp_path: Path) -> None:
    make_oda(tmp_path, "25.12.0")
    newest = make_oda(tmp_path, "27.1.0")
    make_oda(tmp_path, "9.9.0")
    found = std.find_oda(
        platform="win32", which=lambda n: None, program_dirs=[tmp_path], environ={}
    )
    assert found == str(newest)


def test_oda_search_roots_and_non_ascii(tmp_path: Path) -> None:
    root = tmp_path / "Programy zażółć"
    exe = make_oda(root, "27.1.0")
    assert std.find_oda(
        platform="win32", which=lambda n: None, program_dirs=[tmp_path / "none", root], environ={}
    ) == str(exe)


def test_oda_not_found(tmp_path: Path) -> None:
    assert (
        std.find_oda(platform="win32", which=lambda n: None, program_dirs=[tmp_path], environ={})
        is None
    )
    assert std.find_oda(platform="linux", which=lambda n: None, home=tmp_path, environ={}) in (
        None,
        "/usr/bin/ODAFileConverter",
        "/usr/local/bin/ODAFileConverter",
    )


def test_oda_path_and_env_override_come_first(tmp_path: Path) -> None:
    make_oda(tmp_path, "27.1.0")
    assert (
        std.find_oda(
            platform="win32", which=lambda n: "/on/path", program_dirs=[tmp_path], environ={}
        )
        == "/on/path"
    )
    override = tmp_path / "custom.exe"
    override.write_bytes(b"")
    found = std.find_oda(
        platform="win32",
        which=lambda n: "/on/path",
        program_dirs=[tmp_path],
        environ={"CAD_DRAWINGS_ODA": str(override)},
    )
    assert found == str(override)


def test_oda_appimage_on_linux(tmp_path: Path) -> None:
    app = tmp_path / "Apps" / "ODAFileConverter_QT6_lnxX64_8.3dll_27.1.AppImage"
    app.parent.mkdir()
    app.write_bytes(b"")
    if not Path("/usr/bin/ODAFileConverter").exists():
        assert std.find_oda(
            platform="linux", which=lambda n: None, home=tmp_path, environ={}
        ) == str(app)


def test_libredwg_tools_by_name() -> None:
    assert std.find_libredwg(which=lambda n: f"/bin/{n}" if n == "dwg2dxf" else None) == {
        "dwg2dxf": "/bin/dwg2dxf"
    }
    assert std.find_libredwg(which=lambda n: None) == {}


# -- cadlib wrapper -------------------------------------------------------------------------


def test_cadlib_wrapper_exposes_the_command_and_shared_helpers() -> None:
    assert set(lib_doctor.COMMANDS) == {"doctor"}
    assert lib_doctor.find_oda is std.find_oda or lib_doctor.find_oda.__name__ == "find_oda"
    import argparse

    parser = argparse.ArgumentParser()
    lib_doctor.COMMANDS["doctor"].add_arguments(parser)
    result = lib_doctor.COMMANDS["doctor"].run(parser.parse_args([]))
    assert result.exit_code == ExitCode.OK and "capabilities" in result.summary
    SCHEMA.validate(json.loads(result.to_json()))
    assert parser.parse_args(["--probe-com"]).probe_com is True


# -- hardening: render matrix, install hints, orphans ---------------------------------------


def test_render_is_available_only_with_the_whole_stack() -> None:
    c = caps(make_env("linux"))
    assert c["render"]["status"] == "available" and "install" not in c["render"]
    for missing in ("pillow", "matplotlib"):
        c = caps(make_env("linux", packages=dict(FULL, **{missing: None})))
        assert c["render"]["status"] == "missing", missing
        assert any(missing in cmd for cmd in c["render"]["install"])


def test_render_without_pypdfium2_degrades_instead_of_failing() -> None:
    c = caps(make_env("linux", packages=dict(FULL, pypdfium2=None)))
    assert c["render"]["status"] == "degraded"
    assert any("pypdfium2" in cmd for cmd in c["render"]["install"])


def test_old_matplotlib_is_not_enough() -> None:
    assert std.MIN_VERSIONS["matplotlib"] == "3.8"
    c = caps(make_env("linux", packages=dict(FULL, matplotlib="3.7.5")))
    assert c["render"]["status"] == "missing"
    assert any("matplotlib>=3.8" in cmd for cmd in c["render"]["install"])


def test_com_render_survives_without_the_ezdxf_stack_but_says_so() -> None:
    packages = dict(FULL, matplotlib=None)
    c = caps(make_env(hosts=[AUTOCAD], packages=packages))
    assert c["render"]["status"] == "degraded" and c["render"]["via"] == "com+pypdfium2"
    assert any("matplotlib" in cmd for cmd in c["render"]["install"])


def test_install_hints_only_name_what_is_actually_missing() -> None:
    c = caps(make_env("linux", packages=dict(FULL, pillow=None)))
    hints = " ".join(c["render"]["install"])
    assert "pillow" in hints and "matplotlib" not in hints and "ezdxf" not in hints
    assert "install" not in caps(make_env("linux"))["read_dxf"]


def test_orphans_are_children_of_dead_runs_that_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    run_dir = tmp_path / "20260101-000000-edit-ab12"
    run_dir.mkdir()
    child = {"pid": __import__("os").getpid(), "image": "python.exe", "role": "cad", "started": "t"}
    (run_dir / "status.json").write_text(
        json.dumps({"pid": dead.pid, "state": "running", "children": [child]}), encoding="utf-8"
    )
    alive_run = tmp_path / "20260101-000001-edit-cd34"
    alive_run.mkdir()
    (alive_run / "status.json").write_text(
        json.dumps({"pid": __import__("os").getpid(), "state": "running", "children": [child]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(std, "_image_of_pid", lambda pid: "python.exe")
    found = std.find_orphans(tmp_path)
    assert [o["pid"] for o in found] == [child["pid"]]
    assert found[0]["run"] == run_dir.name and found[0]["image"] == "python.exe"


def test_orphan_with_a_reused_pid_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    run_dir = tmp_path / "20260101-000000-edit-ab12"
    run_dir.mkdir()
    child = {"pid": __import__("os").getpid(), "image": "acad.exe", "role": "cad", "started": "t"}
    (run_dir / "status.json").write_text(
        json.dumps({"pid": dead.pid, "state": "running", "children": [child]}), encoding="utf-8"
    )
    monkeypatch.setattr(std, "_image_of_pid", lambda pid: "notepad.exe")
    assert std.find_orphans(tmp_path) == []


def test_orphans_are_listed_in_the_summary() -> None:
    orphan = {"pid": 4242, "image": "acad.exe", "role": "cad", "run": "r1"}
    summary, warnings = std.assess(make_env(orphans=[orphan]))
    assert summary["orphans"] == [orphan] and any("4242" in w for w in warnings)
    summary, _ = std.assess(make_env())
    assert summary["orphans"] == []


def test_runs_base_resolution_matches_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import runs

    monkeypatch.delenv("CAD_DRAWINGS_RUNS", raising=False)
    assert std.default_runs_base() == runs.runs_base()
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(tmp_path))
    assert std.default_runs_base() == runs.runs_base() == tmp_path
