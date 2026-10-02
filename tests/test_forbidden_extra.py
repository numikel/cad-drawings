"""Client-identifying terms are loaded at run time and never echoed (the repo must not contain them)."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCANNER = Path(__file__).resolve().parent / "check_forbidden.py"
spec = importlib.util.spec_from_file_location("cad_check_forbidden_extra", SCANNER)
assert spec is not None and spec.loader is not None
scanner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scanner  # dataclasses need the module registered
spec.loader.exec_module(scanner)

TERM = "Zorblax"  # an invented term; the real ones live outside the repository


def _plant(root: Path, text: str) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "note.md").write_text(text, encoding="utf-8")


def test_term_from_the_environment_is_reported_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(tmp_path, f"fine\nsee {TERM} here\n")
    monkeypatch.setenv(scanner.EXTRA_ENV, f"unused,{TERM}")
    found = scanner.find_violations(tmp_path)
    assert [(v.path, v.line) for v in found] == [("docs/note.md", 2)]
    assert TERM not in found[0].message and "local rule" in found[0].message


def test_term_from_the_untracked_file_supports_ignore_case_and_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(scanner.EXTRA_ENV, raising=False)
    _plant(tmp_path, f"fine\nsee {TERM.upper()} here\n")
    (tmp_path / "_local").mkdir()
    (tmp_path / "_local" / "forbidden_extra.txt").write_text(
        f"# comment\n\ni:{TERM}\n", encoding="utf-8"
    )
    # _local is excluded from the scan itself, so only the planted note is reported
    assert [(v.path, v.line) for v in scanner.find_violations(tmp_path)] == [("docs/note.md", 2)]


def test_without_extra_rules_only_generic_rules_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(scanner.EXTRA_ENV, raising=False)
    _plant(tmp_path, f"see {TERM} here\n")
    assert scanner.find_violations(tmp_path) == []
