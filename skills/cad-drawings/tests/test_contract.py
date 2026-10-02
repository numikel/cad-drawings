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
