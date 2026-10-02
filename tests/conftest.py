"""Shared pytest fixtures: the synthetic drawings and their ground truth, generated once.

Test modules may still define fixtures of their own with the same names (a module-level
fixture overrides the one defined here); nothing in this file is required by them.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
MAKE_FIXTURES = SKILL_DIR.parents[1] / "evals" / "make_fixtures.py"


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("cad_make_fixtures_shared", MAKE_FIXTURES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``slow`` unless CAD_DRAWINGS_RUN_SLOW=1 (same switch as test_fixtures)."""
    if os.environ.get("CAD_DRAWINGS_RUN_SLOW") == "1":
        return
    skip = pytest.mark.skip(reason="slow test: set CAD_DRAWINGS_RUN_SLOW=1")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def skill_dir() -> Path:
    return SKILL_DIR


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Directory with every synthetic fixture (without the large one), generated once."""
    directory = tmp_path_factory.mktemp("synthetic-fixtures")
    _load_generator().generate(directory)
    return directory


@pytest.fixture(scope="session")
def truth(fixtures_dir: Path) -> dict[str, Any]:
    """Parsed truth.json: ``{"schema_version", "generator", "files": {<name>: {...}}}``."""
    return json.loads((fixtures_dir / "truth.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def changes(fixtures_dir: Path) -> dict[str, Any]:
    """Parsed changes.json: ground truth of the sheet_set_v1 -> plan_v2 comparison."""
    return json.loads((fixtures_dir / "changes.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _no_real_cad_in_unit_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety net: only tests marked ``com`` may start or attach to a real CAD application.

    Every real start goes through ``acad._create_app`` / ``acad._get_active``; unit tests that
    need a fake patch those names themselves (a later monkeypatch in the test wins).
    """
    if request.node.get_closest_marker("com"):
        return
    try:
        from cadlib import acad
    except ImportError:  # scripts dir not on sys.path for this module
        return

    def refuse(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("unit tests must not start or attach to a real CAD application")

    monkeypatch.setattr(acad, "_create_app", refuse)
    monkeypatch.setattr(acad, "_get_active", refuse)


@pytest.fixture(autouse=True)
def _com_tests_wait_for_each_other(request: pytest.FixtureRequest) -> Any:
    """Tests marked ``com`` take a machine-wide lock so parallel agents never share CAD."""
    if not request.node.get_closest_marker("com"):
        yield
        return
    import time

    from cadlib import runs
    from cadlib.result import CadError

    deadline = time.monotonic() + 1500
    while True:
        try:
            cm = runs.acquire_lock("com-tests")
            cm.__enter__()
            break
        except CadError as err:
            if err.code != "LOCKED" or time.monotonic() > deadline:
                raise
            time.sleep(5)
    try:
        yield
    finally:
        cm.__exit__(None, None, None)
