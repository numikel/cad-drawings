"""cleanup: only manifest-tracked files are deleted, --yes is required, orphans are report-only."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import cleanup as cl
from cadlib.result import CadError, ExitCode, Result
from cadlib.runs import RunContext

SCHEMA = Draft202012Validator(json.loads((SKILL / "assets/output.schema.json").read_text("utf-8")))


def make_run(
    base: Path, age_days: float = 0.0, files: dict[str, bytes] | None = None
) -> RunContext:
    ctx = RunContext.create("info", base)
    ctx.finish()
    for name, data in (files or {"out.json": b"{}", "sub/deep.txt": b"abc"}).items():
        ctx.path(name).write_bytes(data)
    if age_days:
        manifest = ctx.dir / "manifest.json"
        data = json.loads(manifest.read_text("utf-8"))
        stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
        data["started"] = stamp.isoformat(timespec="seconds")
        manifest.write_text(json.dumps(data), encoding="utf-8")
    return ctx


def run_cmd(*argv: str) -> tuple[Result, dict[str, Any]]:
    parser = argparse.ArgumentParser()
    p = parser.add_subparsers(dest="command").add_parser("cleanup")
    cl.COMMANDS["cleanup"].add_arguments(p)
    args = parser.parse_args(["cleanup", *argv])
    try:
        res = cl.COMMANDS["cleanup"].run(args)
    except CadError as err:
        res = Result.from_error("cleanup", err)
    data = json.loads(res.to_json())
    SCHEMA.validate(data)
    return res, data


def test_list_is_the_default_and_deletes_nothing(tmp_path: Path) -> None:
    ctx = make_run(tmp_path)
    res, data = run_cmd("--run-dir", str(tmp_path))
    assert res.exit_code == 0 and data["summary"]["runs"] == 1
    assert data["summary"]["listed"][0]["id"] == ctx.dir.name
    assert (ctx.dir / "out.json").exists()


def test_list_on_an_empty_or_missing_base(tmp_path: Path) -> None:
    _, data = run_cmd("--list", "--run-dir", str(tmp_path / "nothing"))
    assert data["summary"]["runs"] == 0


def test_deleting_needs_yes(tmp_path: Path) -> None:
    ctx = make_run(tmp_path)
    res, data = run_cmd("--run", ctx.dir.name, "--run-dir", str(tmp_path))
    assert (
        res.exit_code == ExitCode.PRECONDITION_FAILED
        and data["errors"][0]["code"] == "CONFIRM_REQUIRED"
    )
    assert (ctx.dir / "out.json").exists()


def test_dry_run_shows_the_plan_and_keeps_files(tmp_path: Path) -> None:
    ctx = make_run(tmp_path)
    res, data = run_cmd("--run", ctx.dir.name, "--dry-run", "--yes", "--run-dir", str(tmp_path))
    assert (
        res.exit_code == 0
        and data["summary"]["dry_run"] is True
        and data["summary"]["plan"]["runs"] == 1
    )
    assert (ctx.dir / "out.json").exists()


def test_yes_deletes_tracked_files_and_the_run_directory(tmp_path: Path) -> None:
    keep = make_run(tmp_path)
    gone = make_run(tmp_path)
    res, data = run_cmd("--run", gone.dir.name, "--yes", "--run-dir", str(tmp_path))
    assert res.exit_code == 0 and data["summary"]["deleted"]["runs"] == 1
    assert not gone.dir.exists() and keep.dir.exists()


def test_an_untracked_file_is_never_deleted(tmp_path: Path) -> None:
    ctx = make_run(tmp_path)
    foreign = ctx.dir / "user_notes.txt"
    foreign.write_text("mine", encoding="utf-8")
    nested = ctx.dir / "sub" / "foreign.dxf"
    nested.write_text("mine too", encoding="utf-8")
    _res, data = run_cmd("--run", ctx.dir.name, "--yes", "--run-dir", str(tmp_path))
    assert foreign.read_text("utf-8") == "mine" and nested.read_text("utf-8") == "mine too"
    assert not (ctx.dir / "out.json").exists() and not (ctx.dir / "log.txt").exists()
    assert any("not created by the tool" in w for w in data["warnings"])
    # the run stays recognisable, so a later cleanup does not lose track of it
    assert [r.run_id for r in cl.scan_runs(tmp_path)] == [ctx.dir.name]


def test_a_directory_without_manifest_is_listed_but_not_deleted(tmp_path: Path) -> None:
    ctx = make_run(tmp_path, age_days=30)
    (ctx.dir / "manifest.json").unlink()
    _, listing = run_cmd("--list", "--run-dir", str(tmp_path))
    assert listing["summary"]["listed"][0]["no_manifest"] is True
    run_cmd("--older-than", "1", "--yes", "--run-dir", str(tmp_path))
    assert (ctx.dir / "out.json").exists() and not (ctx.dir / "manifest.json").exists()


def test_unsafe_manifest_entries_are_refused(tmp_path: Path) -> None:
    ctx = make_run(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch", encoding="utf-8")
    manifest = ctx.dir / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["files"] += ["../outside.txt", str(outside)]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    res, out = run_cmd("--run", ctx.dir.name, "--yes", "--run-dir", str(tmp_path))
    assert outside.read_text("utf-8") == "do not touch"
    assert res.exit_code == ExitCode.PARTIAL and any("unsafe" in w for w in out["warnings"])


def test_older_than_selects_only_old_runs(tmp_path: Path) -> None:
    old = make_run(tmp_path, age_days=10)
    new = make_run(tmp_path, age_days=1)
    run_cmd("--older-than", "7", "--yes", "--run-dir", str(tmp_path))
    assert not old.dir.exists() and new.dir.exists()


def test_a_running_run_is_skipped(tmp_path: Path) -> None:
    ctx = RunContext.create("render", tmp_path)
    ctx.progress(1, 5, "busy")  # fresh heartbeat of this (live) process
    ctx.path("partial.png").write_bytes(b"x")
    res, data = run_cmd("--run", ctx.dir.name, "--yes", "--run-dir", str(tmp_path))
    assert (ctx.dir / "partial.png").exists() and any(
        "still running" in w for w in data["warnings"]
    )
    assert res.exit_code == 0


def test_run_ids_may_be_unique_prefixes_and_unknown_ids_fail(tmp_path: Path) -> None:
    ctx = make_run(tmp_path)
    res, _ = run_cmd("--run", ctx.dir.name[:19], "--yes", "--run-dir", str(tmp_path))
    assert res.exit_code == 0 and not ctx.dir.exists()
    res, data = run_cmd("--run", "20000101-000000-none", "--yes", "--run-dir", str(tmp_path))
    assert res.exit_code == ExitCode.BAD_ARGS and data["errors"][0]["code"] == "BAD_ARGS"


def test_foreign_directories_in_the_base_are_ignored(tmp_path: Path) -> None:
    other = tmp_path / "my-project"
    other.mkdir()
    (other / "file.txt").write_text("x", encoding="utf-8")
    make_run(tmp_path, age_days=30)
    run_cmd("--older-than", "1", "--yes", "--run-dir", str(tmp_path))
    assert (other / "file.txt").exists()


def test_cache_cleanup_only_removes_cache_entries(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    entry = cache / f"{'a' * 40}-oda.dxf"
    entry.write_text("dxf", encoding="utf-8")
    foreign = cache / "notes.txt"
    foreign.write_text("mine", encoding="utf-8")
    res, data = run_cmd("--cache", "--yes", "--run-dir", str(tmp_path))
    assert res.exit_code == 0 and not entry.exists() and foreign.exists()
    assert data["summary"]["deleted"]["files"] == 1


def test_cache_older_than_keeps_fresh_entries(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    old, fresh = cache / f"{'a' * 40}-oda.dxf", cache / f"{'b' * 40}-oda.dxf"
    for p in (old, fresh):
        p.write_text("dxf", encoding="utf-8")
    stamp = time.time() - 30 * 86400
    os.utime(old, (stamp, stamp))
    run_cmd("--cache", "--older-than", "7", "--yes", "--run-dir", str(tmp_path))
    assert not old.exists() and fresh.exists()


def test_orphans_are_reported_and_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cl._doctor, "running_cad_processes", list)
    drawings = tmp_path / "rysunki zażółć"
    drawings.mkdir()
    (drawings / "a.dwg").write_bytes(b"AC")
    lock = drawings / "a.dwl"
    lock.write_text(f"someone\n{cl.platform.node()}\n", encoding="utf-8")
    other = drawings / "b.dwl2"
    other.write_text("someone\nsome-other-computer\n", encoding="utf-8")
    res, data = run_cmd("--orphans", str(drawings), "--yes", "--run-dir", str(tmp_path / "runs"))
    info = data["summary"]["orphans"]
    assert res.exit_code == 0 and info["count"] == 2 and info["probably_orphaned"] == 1
    verdicts = {Path(i["path"]).name: i["verdict"] for i in info["items"]}
    assert verdicts == {"a.dwl": "probably_orphaned", "b.dwl2": "unknown"}
    assert lock.exists() and other.exists()


def test_orphans_with_a_running_cad_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cl._doctor, "running_cad_processes", lambda: ["acad.exe"])
    (tmp_path / "a.dwl").write_text(f"u\n{cl.platform.node()}\n", encoding="utf-8")
    _, data = run_cmd("--orphans", str(tmp_path), "--run-dir", str(tmp_path / "runs"))
    assert data["summary"]["orphans"]["probably_orphaned"] == 0


def test_orphans_dir_must_exist(tmp_path: Path) -> None:
    res, _ = run_cmd("--orphans", str(tmp_path / "nope"), "--run-dir", str(tmp_path))
    assert res.exit_code == ExitCode.BAD_ARGS


def test_non_ascii_base_and_files(tmp_path: Path) -> None:
    base = tmp_path / "przebiegi zażółć"
    ctx = make_run(base, files={"wyniki/ąęć.txt": "zażółć".encode()})
    _, listing = run_cmd("--list", "--run-dir", str(base))
    assert listing["summary"]["listed"][0]["id"] == ctx.dir.name
    res, _ = run_cmd("--run", ctx.dir.name, "--yes", "--run-dir", str(base))
    assert res.exit_code == 0 and not ctx.dir.exists()
