"""convert: backend policy, verification, ODA/LibreDWG command lines (faked), cache, command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "evals"))

import cadlib
import make_fixtures
from cadlib import convert as cv
from cadlib.result import CadError, ExitCode, Result
from cadlib.runs import RunContext, file_sha1

SCHEMA = Draft202012Validator(json.loads((SKILL / "assets/output.schema.json").read_text("utf-8")))
DWG_BYTES = b"AC1032" + b"\x00" * 64


@pytest.fixture(scope="module")
def valid_dxf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dxf") / "plan.dxf"
    doc, _ = make_fixtures.build_plan(make_fixtures.units.MM, (0.0, 0.0))
    doc.saveas(path)
    return path


@pytest.fixture
def dwg(tmp_path: Path) -> Path:
    path = tmp_path / "src" / "drawing.dwg"
    path.parent.mkdir()
    path.write_bytes(DWG_BYTES)
    return path


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext.create("convert", tmp_path / "runs")


class Fake:
    """A scriptable converter. ``mode``: ok | nothing | garbage | stale | boom | timeout."""

    def __init__(
        self,
        name: str,
        dxf: Path,
        *,
        mode: str = "ok",
        available: bool = True,
        approximate: bool = False,
        formats: tuple[str, ...] = ("dxf", "dwg"),
    ) -> None:
        self.name, self.approximate, self.formats = name, approximate, formats
        self._dxf, self.mode, self._available = dxf, mode, available
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return self._available, "" if self._available else "not installed"

    def convert(self, src: Path, dst: Path, fmt: str, ctx: RunContext) -> list[str]:
        self.calls += 1
        if self.mode == "boom":
            raise CadError("COM_ERROR", "application crashed")
        if self.mode == "timeout":
            raise CadError("TIMEOUT", "no answer", exit_code=ExitCode.TIMEOUT)
        if self.mode == "nothing":
            return []
        if self.mode == "garbage":
            dst.write_bytes(b"this is not a drawing")
            return []
        if fmt == "dxf":
            shutil.copyfile(self._dxf, dst)
        else:
            dst.write_bytes(DWG_BYTES)
        if self.mode == "stale":
            old = time.time() - 3600
            os.utime(dst, (old, old))
        return [f"{self.name} note"]


def run(src: Path, dst: Path, ctx: RunContext, pool: list[Any], fmt: str = "dxf", **kw: Any) -> Any:
    return cv.convert(src, dst, fmt, ctx=ctx, converters=pool, **kw)


# -- policy ---------------------------------------------------------------------------------


def test_first_available_backend_in_priority_order_is_used(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    com, oda = Fake("com", valid_dxf), Fake("oda", valid_dxf)
    conv, warnings = run(dwg, tmp_path / "o.dxf", ctx, [com, oda])
    assert conv is com and oda.calls == 0 and warnings == ["com note"]
    assert (tmp_path / "o.dxf").is_file()


def test_unavailable_backends_are_skipped(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf, available=False), Fake("oda", valid_dxf)]
    assert run(dwg, tmp_path / "o.dxf", ctx, pool)[0].name == "oda"


def test_backend_that_cannot_write_the_format_is_skipped(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    dxf = tmp_path / "in.dxf"
    shutil.copyfile(valid_dxf, dxf)
    pool = [Fake("com", valid_dxf, formats=("dxf",)), Fake("oda", valid_dxf)]
    conv, _ = run(dxf, tmp_path / "o.dwg", ctx, pool, fmt="dwg")
    assert conv.name == "oda"


def test_failure_falls_back_and_says_so(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf, mode="boom"), Fake("oda", valid_dxf)]
    conv, warnings = run(dwg, tmp_path / "o.dxf", ctx, pool)
    assert conv.name == "oda"
    assert "com failed (application crashed); used oda" in warnings[0]


@pytest.mark.parametrize("mode", ["nothing", "garbage", "stale"])
def test_silent_failures_are_caught_by_verification(
    mode: str, dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    bad, good = Fake("com", valid_dxf, mode=mode), Fake("oda", valid_dxf)
    conv, warnings = run(dwg, tmp_path / "o.dxf", ctx, [bad, good])
    assert conv is good and "com failed" in warnings[0]


def test_a_single_verification_failure_is_reported_with_its_code(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    with pytest.raises(CadError) as err:
        run(dwg, tmp_path / "o.dxf", ctx, [Fake("oda", valid_dxf, mode="garbage")])
    assert err.value.code == "OUTPUT_INVALID" and not (tmp_path / "o.dxf").exists()


def test_dwg_output_must_look_like_a_dwg(ctx: RunContext, valid_dxf: Path, tmp_path: Path) -> None:
    dxf = tmp_path / "in.dxf"
    shutil.copyfile(valid_dxf, dxf)
    assert run(dxf, tmp_path / "o.dwg", ctx, [Fake("oda", valid_dxf)], fmt="dwg")[0].name == "oda"
    with pytest.raises(CadError) as err:
        run(dxf, tmp_path / "p.dwg", ctx, [Fake("oda", valid_dxf, mode="garbage")], fmt="dwg")
    assert err.value.code == "OUTPUT_INVALID"


def test_all_backends_failing_keeps_the_common_exit_code(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf, mode="timeout"), Fake("oda", valid_dxf, mode="timeout")]
    with pytest.raises(CadError) as err:
        run(dwg, tmp_path / "o.dxf", ctx, pool)
    assert err.value.code == "CONVERT_FAILED" and err.value.exit_code == ExitCode.TIMEOUT
    assert "com:" in err.value.message and "oda:" in err.value.message


def test_no_backend_is_exit_3_with_install_hint(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf, available=False), Fake("oda", valid_dxf, available=False)]
    with pytest.raises(CadError) as err:
        run(dwg, tmp_path / "o.dxf", ctx, pool)
    assert err.value.code == "NO_BACKEND" and err.value.exit_code == ExitCode.MISSING_DEPENDENCY
    assert "ODA" in (err.value.hint or "") and "com: not installed" in err.value.message


def test_explicit_backend_is_exclusive(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf), Fake("oda", valid_dxf, available=False)]
    with pytest.raises(CadError) as err:
        run(dwg, tmp_path / "o.dxf", ctx, pool, prefer="oda")
    assert err.value.code == "NO_BACKEND" and pool[0].calls == 0
    assert run(dwg, tmp_path / "o.dxf", ctx, pool, prefer="com")[0].name == "com"
    with pytest.raises(CadError) as err:
        run(dwg, tmp_path / "o.dxf", ctx, pool, prefer="nope")
    assert err.value.code == "BAD_ARGS"


def test_approximate_backend_adds_a_warning(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    conv, warnings = run(
        dwg, tmp_path / "o.dxf", ctx, [Fake("libredwg", valid_dxf, approximate=True)]
    )
    assert conv.approximate and cv.APPROX_WARNING in warnings


def test_bad_inputs(ctx: RunContext, valid_dxf: Path, tmp_path: Path) -> None:
    with pytest.raises(CadError) as err:
        run(tmp_path / "missing.dwg", tmp_path / "o.dxf", ctx, [Fake("oda", valid_dxf)])
    assert err.value.code == "FILE_NOT_FOUND"
    with pytest.raises(CadError) as err:
        run(valid_dxf, tmp_path / "o.pdf", ctx, [Fake("oda", valid_dxf)], fmt="pdf")
    assert err.value.code == "BAD_ARGS"


# -- COM backend (never real) ---------------------------------------------------------------


def test_com_converter_uses_the_injected_exporter(
    dwg: Path, ctx: RunContext, valid_dxf: Path
) -> None:
    seen: list[tuple[Path, Path]] = []

    def exporter(src: Path, dst: Path) -> None:
        seen.append((src, dst))
        shutil.copyfile(valid_dxf, dst)

    com = cv.ComConverter(exporter=exporter)
    assert com.available() == (True, "injected") and com.formats == ("dxf",)
    stage = ctx.path("x.dxf")
    assert com.convert(dwg, stage, "dxf", ctx) == [] and seen == [(dwg, stage)]
    with pytest.raises(CadError):
        com.convert(dwg, stage, "dwg", ctx)


def test_com_converter_without_the_acad_module_is_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `from . import acad` prefers the attribute on the package, so drop it as well
    monkeypatch.setitem(sys.modules, "cadlib.acad", None)
    monkeypatch.delattr(cadlib, "acad", raising=False)
    ok, why = cv.ComConverter().available()
    assert ok is False
    assert why == ("acad module missing" if os.name == "nt" else "COM needs Windows")


def test_detect_converters_priority() -> None:
    assert [c.name for c in cv.detect_converters()] == ["com", "oda", "libredwg"]


# -- ODA ------------------------------------------------------------------------------------


def fake_oda_run(created: list[list[str]]) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        created.append(args)
        i = args.index("ODAFileConverter.exe") if "ODAFileConverter.exe" in args else 0
        in_dir, out_dir, _version, fmt = (
            Path(args[i + 1]),
            Path(args[i + 2]),
            args[i + 3],
            args[i + 4],
        )
        name = args[i + 7]
        assert (in_dir / name).is_file()
        if fmt == "DXF":
            shutil.copyfile(fake.dxf, out_dir / "input.dxf")  # type: ignore[attr-defined]
        else:
            (out_dir / "input.dwg").write_bytes(DWG_BYTES)
        return subprocess.CompletedProcess(args, 1, "", "")  # exit code 1 although it worked

    return fake


def test_oda_command_line_staging_and_untrusted_exit_code(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    fake = fake_oda_run(calls)
    fake.dxf = valid_dxf  # type: ignore[attr-defined]
    monkeypatch.setattr(cv.subprocess, "run", fake)
    oda = cv.OdaConverter(exe="ODAFileConverter.exe")
    conv, _ = cv.convert(dwg, tmp_path / "out" / "o.dxf", "dxf", ctx=ctx, converters=[oda])
    assert conv is oda and (tmp_path / "out" / "o.dxf").is_file()
    args = calls[0]
    assert args[0] == "ODAFileConverter.exe" and args[3:] == [
        "ACAD2018",
        "DXF",
        "0",
        "0",
        "input.dwg",
    ]
    assert not any(p in args[1] for p in ("drawing.dwg",)) and Path(args[1]).parent.name == "oda"
    # DXF -> DWG goes through the same staging
    dxf_in = tmp_path / "in.dxf"
    shutil.copyfile(valid_dxf, dxf_in)
    cv.convert(dxf_in, tmp_path / "o.dwg", "dwg", ctx=ctx, converters=[oda])
    assert calls[1][4] == "DWG" and calls[1][-1] == "input.dxf"


def test_oda_success_means_output_exists_not_exit_code_zero(
    dwg: Path, ctx: RunContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cv.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "done", "")
    )
    with pytest.raises(CadError) as err:
        cv.convert(dwg, tmp_path / "o.dxf", "dxf", ctx=ctx, converters=[cv.OdaConverter(exe="x")])
    assert err.value.code == "CONVERT_FAILED" and "no output" in err.value.message


def test_oda_timeout(
    dwg: Path, ctx: RunContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(args: list[str], **kw: Any) -> None:
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(cv.subprocess, "run", hang)
    with pytest.raises(CadError) as err:
        cv.convert(
            dwg,
            tmp_path / "o.dxf",
            "dxf",
            ctx=ctx,
            converters=[cv.OdaConverter(exe="x", timeout=3)],
        )
    assert err.value.code == "TIMEOUT" and err.value.exit_code == ExitCode.TIMEOUT


def test_oda_availability_from_a_detected_exe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = tmp_path / "ODAFileConverter.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cv._doctor, "find_oda", lambda: str(exe))
    monkeypatch.setattr(cv.sys, "platform", "win32")
    assert cv.OdaConverter().available() == (True, str(exe))
    monkeypatch.setattr(cv._doctor, "find_oda", lambda: None)
    assert cv.OdaConverter().available() == (False, "ODAFileConverter not found")


def test_oda_on_linux_needs_a_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv._doctor, "find_oda", lambda: "/usr/bin/ODAFileConverter")
    monkeypatch.setattr(cv.sys, "platform", "linux")
    monkeypatch.setattr(cv._doctor, "has_display", lambda: False)
    monkeypatch.setattr(cv.shutil, "which", lambda name: None)
    ok, why = cv.OdaConverter().available()
    assert not ok and "Xvfb" in why


# -- LibreDWG -------------------------------------------------------------------------------


def test_libredwg_formats_follow_the_tools_present() -> None:
    assert cv.LibreDwgConverter(tools={"dwg2dxf": "a"}).formats == ("dxf",)
    assert cv.LibreDwgConverter(tools={"dwg2dxf": "a", "dxf2dwg": "b"}).formats == ("dxf", "dwg")
    assert cv.LibreDwgConverter(tools={}).available()[0] is False


def test_libredwg_command_lines_and_warnings(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = Path(args[args.index("-o") + 1])
        if args[0] == "dwg2dxf":
            shutil.copyfile(valid_dxf, out)
        else:
            out.write_bytes(DWG_BYTES)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cv.subprocess, "run", fake)
    libre = cv.LibreDwgConverter(tools={"dwg2dxf": "dwg2dxf", "dxf2dwg": "dxf2dwg"})
    conv, warnings = cv.convert(dwg, tmp_path / "o.dxf", "dxf", ctx=ctx, converters=[libre])
    assert conv.approximate and cv.APPROX_WARNING in warnings
    assert calls[0][:2] == ["dwg2dxf", "-y"] and calls[0][-1] == str(dwg)
    dxf_in = tmp_path / "in.dxf"
    shutil.copyfile(valid_dxf, dxf_in)
    _, warnings = cv.convert(dxf_in, tmp_path / "o.dwg", "dwg", ctx=ctx, converters=[libre])
    assert calls[1][:4] == ["dxf2dwg", "-y", "--as", "r2004"] and any(
        "r2004" in w for w in warnings
    )


# -- cache ----------------------------------------------------------------------------------


def test_ensure_dxf_returns_a_dxf_source_untouched(ctx: RunContext, valid_dxf: Path) -> None:
    found = cv.ensure_dxf(valid_dxf, ctx, converters=[])
    assert found.path == valid_dxf and found.backend is None


def test_ensure_dxf_converts_once_and_then_hits_the_cache(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    oda = Fake("oda", valid_dxf)
    base = tmp_path / "base"
    first = cv.ensure_dxf(dwg, ctx, base=base, converters=[oda])
    assert not first.cached and oda.calls == 1 and first.backend == "oda"
    second = cv.ensure_dxf(dwg, ctx, base=base, converters=[oda])
    assert second.cached and oda.calls == 1 and second.path.parent == base / "cache"
    dwg.write_bytes(DWG_BYTES + b"edited")  # new content, new hash: no stale hit
    third = cv.ensure_dxf(dwg, ctx, base=base, converters=[oda])
    assert not third.cached and oda.calls == 2


def test_cache_is_keyed_by_converter(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    cv.ensure_dxf(dwg, ctx, base=base, converters=[Fake("oda", valid_dxf)])
    libre = Fake("libredwg", valid_dxf, approximate=True)
    found = cv.ensure_dxf(
        dwg, ctx, base=base, prefer="libredwg", converters=[Fake("oda", valid_dxf), libre]
    )
    assert not found.cached and libre.calls == 1 and file_sha1(dwg)  # the oda entry was not reused


# -- command --------------------------------------------------------------------------------


def run_cmd(*argv: str) -> tuple[Result, dict[str, Any]]:
    parser = argparse.ArgumentParser()
    p = parser.add_subparsers(dest="command").add_parser("convert")
    cv.COMMANDS["convert"].add_arguments(p)
    args = parser.parse_args(["convert", *argv])
    try:
        res = cv.COMMANDS["convert"].run(args)
    except CadError as err:
        res = Result.from_error("convert", err)
    data = json.loads(res.to_json())
    SCHEMA.validate(data)
    return res, data


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch, valid_dxf: Path, tmp_path: Path) -> list[Fake]:
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(tmp_path / "runs"))
    pool = [Fake("com", valid_dxf, available=False), Fake("oda", valid_dxf)]
    monkeypatch.setattr(cv, "detect_converters", lambda timeout=240.0: pool)
    return pool


def test_command_dwg_to_dxf_lands_in_the_run_dir(
    faked: list[Fake], dwg: Path, tmp_path: Path
) -> None:
    _res, data = run_cmd(str(dwg), "--to", "dxf")
    assert (
        data["status"] == "ok" and data["backend"] == "oda" and data["summary"]["cached"] is False
    )
    out = Path(data["outputs"]["output"]["path"])
    assert out.parent.parent == tmp_path / "runs" and out.name == "drawing.dxf"
    assert data["outputs"]["output"]["source_mtime"] and Path(data["run_dir"]).is_dir()
    assert not (dwg.parent / "drawing.dxf").exists()  # never next to the source
    again, data2 = run_cmd(str(dwg), "--to", "dxf")
    assert data2["summary"]["cached"] is True and faked[1].calls == 1 and again.exit_code == 0


def test_command_dxf_to_dwg_with_out(faked: list[Fake], valid_dxf: Path, tmp_path: Path) -> None:
    out = tmp_path / "result" / "plan.dwg"
    _, data = run_cmd(str(valid_dxf), "--to", "dwg", "--out", str(out))
    assert data["status"] == "ok" and out.read_bytes().startswith(b"AC")


def test_command_dxf_to_dxf_is_a_noop_without_out(
    faked: list[Fake], valid_dxf: Path, tmp_path: Path
) -> None:
    _, data = run_cmd(str(valid_dxf), "--to", "dxf")
    assert data["summary"]["noop"] is True and data["outputs"]["output"]["path"] == str(
        valid_dxf.absolute()
    )
    assert "run_dir" not in data and not (tmp_path / "runs").exists()
    assert all(f.calls == 0 for f in faked)


def test_command_dxf_to_dxf_copies_only_when_out_is_given(
    faked: list[Fake], valid_dxf: Path, tmp_path: Path
) -> None:
    out = tmp_path / "copy.dxf"
    _, data = run_cmd(str(valid_dxf), "--to", "dxf", "--out", str(out))
    assert (
        data["status"] == "ok"
        and out.read_bytes() == valid_dxf.read_bytes()
        and data["backend"] == "none"
    )


def test_command_never_overwrites_without_the_flag(
    faked: list[Fake], dwg: Path, tmp_path: Path
) -> None:
    out = tmp_path / "o.dxf"
    out.write_text("precious", encoding="utf-8")
    res, data = run_cmd(str(dwg), "--to", "dxf", "--out", str(out))
    assert res.exit_code == ExitCode.PRECONDITION_FAILED and data["errors"][0]["code"] == "EXISTS"
    assert out.read_text("utf-8") == "precious"
    res, data = run_cmd(str(dwg), "--to", "dxf", "--out", str(out), "--overwrite")
    assert data["status"] == "ok" and out.read_text("utf-8") != "precious"


def test_command_no_backend_is_exit_3(
    monkeypatch: pytest.MonkeyPatch, dwg: Path, tmp_path: Path, valid_dxf: Path
) -> None:
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(tmp_path / "runs"))
    pool = [Fake("com", valid_dxf, available=False), Fake("oda", valid_dxf, available=False)]
    monkeypatch.setattr(cv, "detect_converters", lambda timeout=240.0: pool)
    res, data = run_cmd(str(dwg), "--to", "dxf")
    assert res.exit_code == ExitCode.MISSING_DEPENDENCY and data["status"] == "refused"
    assert data["errors"][0]["code"] == "NO_BACKEND" and data["errors"][0]["hint"]


def test_command_dry_run_writes_nothing(faked: list[Fake], dwg: Path, tmp_path: Path) -> None:
    _, data = run_cmd(str(dwg), "--to", "dxf", "--dry-run")
    assert (
        data["summary"]["would_use"] == "oda" and "com: not installed" in data["summary"]["skipped"]
    )
    assert not (tmp_path / "runs").exists() and faked[1].calls == 0


def test_command_argument_errors(faked: list[Fake], dwg: Path, tmp_path: Path) -> None:
    res, data = run_cmd(str(tmp_path / "missing.dwg"), "--to", "dxf")
    assert res.exit_code == ExitCode.BAD_ARGS and data["errors"][0]["code"] == "FILE_NOT_FOUND"
    res, _ = run_cmd(str(dwg), "--to", "dwg")
    assert res.exit_code == ExitCode.BAD_ARGS
    other = tmp_path / "x.txt"
    other.write_text("x", encoding="utf-8")
    res, _ = run_cmd(str(other), "--to", "dxf")
    assert res.exit_code == ExitCode.BAD_ARGS


def test_command_with_non_ascii_paths(faked: list[Fake], tmp_path: Path) -> None:
    src = tmp_path / "rysunki zażółć" / "plan łąka.dwg"
    src.parent.mkdir()
    src.write_bytes(DWG_BYTES)
    out = tmp_path / "wyniki ąę" / "wynik żółw.dxf"
    _, data = run_cmd(
        str(src), "--to", "dxf", "--out", str(out), "--run-dir", str(tmp_path / "przebiegi ć")
    )
    assert data["status"] == "ok" and out.is_file() and "przebiegi ć" in data["run_dir"]


def test_command_failed_run_marks_the_status_file(
    monkeypatch: pytest.MonkeyPatch, dwg: Path, tmp_path: Path, valid_dxf: Path
) -> None:
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(tmp_path / "runs"))
    pool = [Fake("oda", valid_dxf, mode="garbage")]
    monkeypatch.setattr(cv, "detect_converters", lambda timeout=240.0: pool)
    res, _ = run_cmd(str(dwg), "--to", "dxf")
    assert res.exit_code == ExitCode.ERROR
    status = next((tmp_path / "runs").glob("*-convert-*/status.json"))
    assert json.loads(status.read_text("utf-8"))["state"] == "failed"
