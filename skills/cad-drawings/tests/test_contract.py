"""Output contract: Result JSON matches the schema and stays small; cad.py honours exit codes."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib.command import Command
from cadlib.result import MAX_JSON_BYTES, CadError, ExitCode, Result

SCHEMA = Draft202012Validator(json.loads((SKILL / "assets/output.schema.json").read_text("utf-8")))


def validate(res: Result) -> dict:
    data = json.loads(res.to_json())
    SCHEMA.validate(data)
    return data


def test_ok_result_validates_and_has_status_ok() -> None:
    res = Result("info", backend="ezdxf", summary={"layers": 3}, run_dir="x", elapsed_s=1.234)
    data = validate(res)
    assert data["status"] == "ok" and data["exit_code"] == 0 and data["elapsed_s"] == 1.23


@pytest.mark.parametrize(
    ("exit_code", "status"),
    [
        (ExitCode.ERROR, "error"),
        (ExitCode.BAD_ARGS, "error"),
        (ExitCode.MISSING_DEPENDENCY, "refused"),
        (ExitCode.BUSY, "refused"),
        (ExitCode.TIMEOUT, "error"),
        (ExitCode.PRECONDITION_FAILED, "refused"),
        (ExitCode.PARTIAL, "partial"),
    ],
)
def test_every_exit_code_maps_to_a_schema_valid_status(exit_code: ExitCode, status: str) -> None:
    res = Result.from_error("x", CadError("CODE", "msg", exit_code=exit_code, hint="do y"))
    assert validate(res)["status"] == status


def test_error_keeps_hint_and_first_error_sets_exit_code() -> None:
    res = Result("x")
    res.add_error(CadError("A", "first", exit_code=ExitCode.BUSY, hint="h"))
    res.add_error(CadError("B", "second", exit_code=ExitCode.TIMEOUT))
    data = validate(res)
    assert data["exit_code"] == 4 and [e["code"] for e in data["errors"]] == ["A", "B"]
    assert data["errors"][0]["hint"] == "h" and "hint" not in data["errors"][1]


def test_huge_result_is_capped_and_still_valid() -> None:
    res = Result("find", summary={"hits": [{"text": "x" * 500, "i": i} for i in range(500)]})
    for i in range(200):
        res.warn(f"warning number {i} " + "w" * 100)
    res.run_dir, res.log = "run", "log.txt"
    text = res.to_json()
    assert len(text.encode("utf-8")) <= MAX_JSON_BYTES
    data = validate(res)
    assert data["run_dir"] == "run" and data["log"] == "log.txt"


def test_warnings_are_deduplicated() -> None:
    res = Result("x")
    for _ in range(5):
        res.warn("same")
    assert res.warnings == ["same"]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SKILL / "scripts/cad.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_cli_bad_arguments_exit_2_with_json_not_argparse_text() -> None:
    proc = run_cli("no-such-command")
    assert proc.returncode == 2
    data = json.loads(proc.stdout)
    SCHEMA.validate(data)
    assert data["errors"][0]["code"] == "BAD_ARGS"


def test_cli_without_a_command_is_a_json_bad_args_error() -> None:
    proc = run_cli()
    assert proc.returncode == 2
    SCHEMA.validate(json.loads(proc.stdout))


def test_command_dataclass_is_wired_like_cad_py_expects() -> None:
    def add(p: argparse.ArgumentParser) -> None:
        p.add_argument("--x", type=int, default=1)

    cmd = Command(help="h", add_arguments=add, run=lambda a: Result("t", summary={"x": a.x}))
    parser = argparse.ArgumentParser()
    add_parsed = parser.add_subparsers(dest="command").add_parser("t")
    cmd.add_arguments(add_parsed)
    assert cmd.run(parser.parse_args(["t", "--x", "7"])).summary == {"x": 7}


def test_cli_uses_utf8_on_stdout() -> None:
    res = Result("x", warnings=["zażółć gęślą jaźń"])
    assert "zażółć" in res.to_json()


def test_truncation_never_drops_approximate_backend_or_artifact_paths() -> None:
    res = Result(
        "render",
        backend="ezdxf",
        approximate=True,
        summary={"layouts": [{"name": f"L{i}", "note": "n" * 80} for i in range(80)]},
        outputs={
            f"png{i}": {"path": f"/runs/x/{i}.png", "bytes": i, "sha1": "0" * 40} for i in range(40)
        },
    )
    for i in range(30):
        res.warn(f"generic warning {i} " + "w" * 90)
    res.warn("approximate render (ezdxf): not a CAD plot")
    text = res.to_json()
    assert len(text.encode("utf-8")) <= MAX_JSON_BYTES
    data = validate(res)
    assert data["approximate"] is True and data["backend"] == "ezdxf"
    assert "approximate" in data["warnings"][0].lower()
    assert data["outputs"] and all("path" in v for v in data["outputs"].values())


def test_cad_error_takes_exit_code_from_the_registry() -> None:
    from cadlib.result import ERROR_CODES

    assert CadError("FILE_NOT_FOUND", "x").exit_code == ExitCode.BAD_ARGS
    assert CadError("LOCKED", "x").exit_code == ExitCode.BUSY
    assert CadError("NOT_A_REGISTERED_CODE", "x").exit_code == ExitCode.ERROR
    assert CadError("NO_BACKEND", "x", exit_code=ExitCode.ERROR).exit_code == ExitCode.ERROR
    assert all(isinstance(v, ExitCode) for v in ERROR_CODES.values())


def _load_cad():
    import importlib

    return importlib.import_module("cad")


def test_missing_dependency_gives_exit_3_json_and_other_commands_survive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cad = _load_cad()
    real_import = cad.importlib.import_module

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "cadlib.dxf":
            raise ModuleNotFoundError("No module named 'ezdxf'", name="ezdxf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(cad.importlib, "import_module", fake_import)
    commands = cad.load_commands()
    assert {"info", "find", "diff"} <= set(commands)
    assert (
        "cleanup" in commands or "doctor" in commands
    )  # modules without the dependency still load
    code = cad.main(["info", "whatever.dxf"])
    data = json.loads(capsys.readouterr().out)
    SCHEMA.validate(data)
    assert code == 3 and data["errors"][0]["code"] == "MISSING_DEPENDENCY"
    assert "ezdxf" in data["errors"][0]["message"] and "ezdxf" in data["errors"][0]["hint"]


def test_error_results_keep_run_dir_and_close_the_run_as_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from cadlib import runs

    cad = _load_cad()
    finished: list[str] = []

    class FakeCtx:
        dir = tmp_path

        def finish(self, state: str = "done") -> None:
            finished.append(state)

    def boom(_args: argparse.Namespace) -> Result:
        raise CadError("PRECONDITION_FAILED", "nope")

    cmd = Command(help="h", add_arguments=lambda p: None, run=boom)
    monkeypatch.setattr(cad, "load_commands", lambda: {"boom": cmd})
    monkeypatch.setattr(runs, "current_context", lambda: FakeCtx(), raising=False)
    code = cad.main(["boom"])
    data = json.loads(capsys.readouterr().out)
    SCHEMA.validate(data)
    assert code == 6 and finished == ["failed"]
    assert data["run_dir"] == str(tmp_path) and data["log"].endswith("log.txt")
