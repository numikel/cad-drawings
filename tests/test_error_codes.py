"""Contract: every ``CadError("CODE", ...)`` in the scripts uses a registered code and exit code.

``cadlib.result.ERROR_CODES`` maps each stable machine code to the ``ExitCode`` every raise of
that code must carry. The scan is static (ast), so a code on a rarely taken path is checked too.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cadlib import result


@dataclass(frozen=True)
class Raise:
    path: str
    line: int
    code: str
    exit_code: str | None  # ExitCode member name when given explicitly as ExitCode.X


def _is_cad_error(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "CadError") or (
        isinstance(func, ast.Attribute) and func.attr == "CadError"
    )


def collect_raises(source: str, path: str = "<memory>") -> tuple[list[Raise], list[str]]:
    """Return (literal CadError calls, locations of calls whose code is not a string literal)."""
    found: list[Raise] = []
    dynamic: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and _is_cad_error(node.func)):
            continue
        first = node.args[0] if node.args else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            dynamic.append(f"{path}:{node.lineno}")
            continue
        exit_code = None
        for kw in node.keywords:
            if kw.arg == "exit_code":
                value = kw.value
                if (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "ExitCode"
                ):
                    exit_code = value.attr
                else:
                    exit_code = "<expression>"
        found.append(Raise(path, node.lineno, first.value, exit_code))
    return found, dynamic


def script_files() -> list[Path]:
    return sorted([*(SCRIPTS / "cadlib").glob("*.py"), *SCRIPTS.glob("*.py")])


@pytest.fixture(scope="module")
def registry() -> dict[str, result.ExitCode]:
    codes = getattr(result, "ERROR_CODES", None)
    if codes is None:
        pytest.skip("cadlib.result.ERROR_CODES does not exist yet")
    return dict(codes)


@pytest.fixture(scope="module")
def raises() -> list[Raise]:
    found: list[Raise] = []
    for path in script_files():
        rel = str(path.relative_to(SKILL)).replace("\\", "/")
        found += collect_raises(path.read_text(encoding="utf-8"), rel)[0]
    return found


def test_registry_values_are_exit_codes(registry: dict[str, result.ExitCode]) -> None:
    assert registry
    bad = {k: v for k, v in registry.items() if not isinstance(v, result.ExitCode)}
    assert not bad, bad
    assert all(k == k.upper() and k.replace("_", "").isalnum() for k in registry)
    assert result.ExitCode.OK not in registry.values(), "an error code cannot map to exit 0"


def test_every_raised_code_is_registered(
    registry: dict[str, result.ExitCode], raises: list[Raise]
) -> None:
    assert raises, "scan found no CadError calls; the scanner is broken"
    unknown = sorted({f"{r.code} ({r.path}:{r.line})" for r in raises if r.code not in registry})
    assert not unknown, f"codes missing from cadlib.result.ERROR_CODES: {unknown}"


def test_explicit_exit_codes_match_the_registry(
    registry: dict[str, result.ExitCode], raises: list[Raise]
) -> None:
    mismatched = []
    for r in raises:
        if r.code not in registry or r.exit_code in (None, "<expression>"):
            continue  # unregistered codes are reported by another test; run-time choices are free
        if r.code in getattr(result, "DYNAMIC_EXIT_CODES", frozenset()):
            continue
        if r.exit_code != registry[r.code].name:
            mismatched.append(
                f"{r.code} at {r.path}:{r.line}: exit_code={r.exit_code}, "
                f"registry says ExitCode.{registry[r.code].name}"
            )
    assert not mismatched, mismatched


# --------------------------------------------------------------------------------------
# self-tests of the scanner
# --------------------------------------------------------------------------------------


def test_scanner_reads_literals_keywords_and_dynamic_codes() -> None:
    source = (
        "raise CadError('A_CODE', 'msg', exit_code=ExitCode.BUSY)\n"
        "raise errors.CadError('B_CODE', 'msg')\n"
        "raise CadError(code, 'msg')\n"
        "raise CadError('C_CODE', 'msg', exit_code=pick())\n"
        "other('NOT_AN_ERROR')\n"
    )
    found, dynamic = collect_raises(source)
    assert [(r.code, r.exit_code) for r in found] == [
        ("A_CODE", "BUSY"),
        ("B_CODE", None),
        ("C_CODE", "<expression>"),
    ]
    assert dynamic == ["<memory>:3"]
