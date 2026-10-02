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

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL.parents[1] / "evals"))

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
    kw.setdefault("allow_com", True)
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
    assert err.value.code == "TIMEOUT" and err.value.exit_code == ExitCode.TIMEOUT
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


class FakeDoc:
    def __init__(self, path: Path) -> None:
        self.path = path


class FakeSession:
    """Stands in for cadlib.acad.AcadSession; records what it was asked to do."""

    def __init__(self, dxf: Path, *, warnings: list[str] | None = None) -> None:
        self._dxf, self._warnings = dxf, warnings or []
        self.exports: list[tuple[Path, Path]] = []
        self.opened: list[Path] = []
        self.saved: list[tuple[FakeDoc, Path]] = []
        self.quit_calls = 0

    def export_dxf(self, src: Path, dst: Path, version: str = "2013") -> list[str]:
        self.exports.append((src, dst))
        shutil.copyfile(self._dxf, dst)
        return list(self._warnings)

    def open(self, path: Path, *, readonly: bool = True) -> FakeDoc:
        self.opened.append(path)
        return FakeDoc(path)

    def save_dwg(self, doc: FakeDoc, dst: Path, version: str = "2013") -> None:
        self.saved.append((doc, dst))
        dst.write_bytes(DWG_BYTES)

    def __enter__(self) -> FakeSession:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.quit_calls += 1


def test_com_converter_exports_a_staged_copy_and_returns_the_warnings(
    dwg: Path, ctx: RunContext, valid_dxf: Path
) -> None:
    session = FakeSession(valid_dxf, warnings=["viewport 2 has status 0"])
    com = cv.ComConverter(session_factory=lambda: session)
    assert com.available() == (True, "injected") and com.formats == ("dxf", "dwg")
    out = ctx.path("x.dxf")
    assert com.convert(dwg, out, "dxf", ctx) == ["viewport 2 has status 0"]
    opened_src, _ = session.exports[0]
    assert opened_src != dwg and opened_src.parent.parent == ctx.dir  # never the original
    assert opened_src.read_bytes() == dwg.read_bytes() and session.quit_calls == 1


def test_com_converter_writes_dwg_through_save_dwg(
    ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    session = FakeSession(valid_dxf)
    com = cv.ComConverter(session_factory=lambda: session)
    src = tmp_path / "in.dxf"
    shutil.copyfile(valid_dxf, src)
    out = ctx.path("x.dwg")
    com.convert(src, out, "dwg", ctx)
    assert out.read_bytes().startswith(b"AC") and session.opened[0] != src
    assert session.saved[0][0].path == session.opened[0]


def test_com_converter_uses_a_given_session_and_does_not_close_it(
    dwg: Path, ctx: RunContext, valid_dxf: Path
) -> None:
    session = FakeSession(valid_dxf)
    com = cv.ComConverter(session=session)
    assert com.available()[0] is True
    com.convert(dwg, ctx.path("x.dxf"), "dxf", ctx)
    assert len(session.exports) == 1 and session.quit_calls == 0


def test_com_converter_rejects_unknown_formats(dwg: Path, ctx: RunContext, valid_dxf: Path) -> None:
    com = cv.ComConverter(session_factory=lambda: FakeSession(valid_dxf))
    with pytest.raises(CadError):
        com.convert(dwg, ctx.path("x.pdf"), "pdf", ctx)


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


# -- hardening: COM only on request, same-file guards, atomic writes ------------------------


def only_com(valid_dxf: Path) -> list[Any]:
    return [Fake("com", valid_dxf)]


def test_com_is_not_used_unless_allowed(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    com, oda = Fake("com", valid_dxf), Fake("oda", valid_dxf)
    conv, _ = cv.convert(dwg, tmp_path / "o.dxf", "dxf", ctx=ctx, converters=[com, oda])
    assert conv is oda and com.calls == 0
    conv, _ = cv.convert(
        dwg, tmp_path / "p.dxf", "dxf", ctx=ctx, converters=[com, oda], allow_com=True
    )
    assert conv is com


def test_prefer_com_implies_allow_com(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    conv, _ = cv.convert(
        dwg, tmp_path / "o.dxf", "dxf", ctx=ctx, converters=only_com(valid_dxf), prefer="com"
    )
    assert conv.name == "com"


def test_only_com_available_asks_for_the_users_consent(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf), Fake("oda", valid_dxf, available=False)]
    with pytest.raises(CadError) as err:
        cv.convert(dwg, tmp_path / "o.dxf", "dxf", ctx=ctx, converters=pool)
    assert err.value.code == "NO_BACKEND" and err.value.exit_code == ExitCode.MISSING_DEPENDENCY
    assert "ask the user" in err.value.hint and "--allow-com" in err.value.hint
    assert pool[0].calls == 0


def test_no_cad_host_gives_no_consent_hint(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    pool = [Fake("com", valid_dxf, available=False), Fake("oda", valid_dxf, available=False)]
    with pytest.raises(CadError) as err:
        cv.convert(dwg, tmp_path / "o.dxf", "dxf", ctx=ctx, converters=pool)
    assert "--allow-com" not in err.value.hint


def test_ensure_dxf_obeys_the_com_policy_and_propagates_warnings(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    com = cv.ComConverter(session_factory=lambda: FakeSession(valid_dxf, warnings=["w1"]))
    with pytest.raises(CadError) as err:
        cv.ensure_dxf(dwg, ctx, base=tmp_path / "b", converters=[com])
    assert err.value.code == "NO_BACKEND" and "--allow-com" in err.value.hint
    found = cv.ensure_dxf(dwg, ctx, base=tmp_path / "b", converters=[com], allow_com=True)
    assert found.backend == "com" and "w1" in found.warnings and not found.cached


def test_ensure_dxf_with_a_session_uses_it(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    session = FakeSession(valid_dxf)
    found = cv.ensure_dxf(
        dwg, ctx, base=tmp_path / "b", session=session, converters=[cv.ComConverter()]
    )
    assert found.backend == "com" and len(session.exports) == 1 and session.quit_calls == 0


def test_ensure_dxf_default_timeout_is_below_host_limits() -> None:
    import inspect

    assert cv.DEFAULT_TIMEOUT_S == 100.0
    assert inspect.signature(cv.ensure_dxf).parameters["timeout"].default == 100.0
    assert inspect.signature(cv.ensure_dxf).parameters["allow_com"].default is False


def test_convert_refuses_an_output_that_is_the_source(
    ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    src = tmp_path / "plan.dxf"
    shutil.copyfile(valid_dxf, src)
    before = src.read_bytes()
    for target in (src, tmp_path / "." / "plan.dxf"):
        with pytest.raises(CadError) as err:
            cv.convert(src, target, "dxf", ctx=ctx, converters=[Fake("oda", valid_dxf)])
        assert err.value.code == "BAD_ARGS" and err.value.exit_code == ExitCode.BAD_ARGS
    assert src.read_bytes() == before


def test_atomic_place_uses_a_temp_beside_the_target(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere" / "o.dxf"
    seen: list[tuple[str, str]] = []
    real = os.replace

    def spy(a: Any, b: Any) -> None:
        seen.append((str(a), str(b)))
        real(a, b)

    def refuse_rename(a: Any, b: Any) -> None:  # force the cross-volume path
        if Path(b) == target and Path(a).parent != target.parent:
            raise OSError(18, "cross-device link")
        spy(a, b)

    monkeypatch.setattr(cv.os, "replace", refuse_rename)
    cv.convert(dwg, target, "dxf", ctx=ctx, converters=[Fake("oda", valid_dxf)])
    assert target.is_file()
    temp, final = seen[-1]
    assert Path(temp).parent == target.parent and Path(final) == target
    assert not [p for p in target.parent.iterdir() if p != target]


def test_failed_conversion_leaves_an_existing_target_untouched(
    dwg: Path, ctx: RunContext, valid_dxf: Path, tmp_path: Path
) -> None:
    target = tmp_path / "o.dxf"
    target.write_text("precious", encoding="utf-8")
    with pytest.raises(CadError):
        cv.convert(dwg, target, "dxf", ctx=ctx, converters=[Fake("oda", valid_dxf, mode="garbage")])
    assert target.read_text("utf-8") == "precious"


def test_verification_without_ezdxf_is_structural(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_dxf: Path
) -> None:
    monkeypatch.setitem(sys.modules, "ezdxf", None)
    warnings = cv.verify_output(valid_dxf, "dxf", time.time() - 5)
    assert any("ezdxf" in w for w in warnings)
    bad = tmp_path / "bad.dxf"
    bad.write_bytes(b"this is not a drawing")
    with pytest.raises(CadError) as err:
        cv.verify_output(bad, "dxf", time.time() - 5)
    assert err.value.code == "OUTPUT_INVALID"


def test_cadlib_modules_do_not_import_heavy_packages_at_import_time() -> None:
    import subprocess

    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import cadlib.runs, cadlib.convert, cadlib.cleanup, cadlib.doctor;"
        "bad = [m for m in ('ezdxf', 'PIL', 'matplotlib') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(SKILL / "scripts")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_command_defaults_and_allow_com_flag(faked: list[Fake]) -> None:
    parser = argparse.ArgumentParser()
    p = parser.add_subparsers(dest="command").add_parser("convert")
    cv.COMMANDS["convert"].add_arguments(p)
    args = parser.parse_args(["convert", "a.dwg", "--to", "dxf"])
    assert args.timeout == 100.0 and args.allow_com is False
    assert parser.parse_args(["convert", "a.dwg", "--to", "dxf", "--allow-com"]).allow_com is True


def test_command_does_not_start_com_without_the_flag(
    monkeypatch: pytest.MonkeyPatch, dwg: Path, tmp_path: Path, valid_dxf: Path
) -> None:
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(tmp_path / "runs"))
    pool = [Fake("com", valid_dxf), Fake("oda", valid_dxf, available=False)]
    monkeypatch.setattr(cv, "detect_converters", lambda timeout=100.0: pool)
    res, data = run_cmd(str(dwg), "--to", "dxf")
    assert (
        res.exit_code == ExitCode.MISSING_DEPENDENCY and "--allow-com" in data["errors"][0]["hint"]
    )
    assert pool[0].calls == 0
    res, data = run_cmd(str(dwg), "--to", "dxf", "--allow-com")
    assert data["status"] == "ok" and data["backend"] == "com" and pool[0].calls == 1


def test_command_prefer_com_implies_allow(
    monkeypatch: pytest.MonkeyPatch, dwg: Path, tmp_path: Path, valid_dxf: Path
) -> None:
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(tmp_path / "runs"))
    pool = [Fake("com", valid_dxf), Fake("oda", valid_dxf)]
    monkeypatch.setattr(cv, "detect_converters", lambda timeout=100.0: pool)
    _res, data = run_cmd(str(dwg), "--to", "dxf", "--backend", "com")
    assert data["backend"] == "com"


def test_command_out_equal_to_the_source_is_a_clean_error(
    faked: list[Fake], valid_dxf: Path, tmp_path: Path
) -> None:
    src = tmp_path / "plan.dxf"
    shutil.copyfile(valid_dxf, src)
    before = src.read_bytes()
    for flags in ([], ["--overwrite"]):
        res, data = run_cmd(str(src), "--to", "dxf", "--out", str(src), *flags)
        assert res.exit_code == ExitCode.BAD_ARGS and data["errors"][0]["code"] == "BAD_ARGS"
        assert src.read_bytes() == before
    res, data = run_cmd(
        str(src), "--to", "dwg", "--out", str(tmp_path / "." / "plan.dxf"), "--overwrite"
    )
    assert res.exit_code == ExitCode.BAD_ARGS and src.read_bytes() == before


def test_command_out_must_not_be_a_directory(faked: list[Fake], dwg: Path, tmp_path: Path) -> None:
    res, _ = run_cmd(str(dwg), "--to", "dxf", "--out", str(tmp_path), "--overwrite")
    assert res.exit_code == ExitCode.BAD_ARGS
