"""Run directories: layout, stale-output detection, disk preflight, staging, locks, cache."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL.parents[1] / "evals"))

import make_fixtures
from cadlib import runs
from cadlib.result import CadError, ExitCode, Result
from cadlib.runs import (
    RUN_ID_RE,
    RunContext,
    acquire_lock,
    cache_get,
    cache_put,
    file_sha1,
    free_gb,
    pid_alive,
    preflight_disk,
    stage_copy,
)


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("fixtures")
    make_fixtures.generate(out)
    return out


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# -- layout ---------------------------------------------------------------------------------


def test_create_makes_the_documented_layout(tmp_path: Path) -> None:
    ctx = RunContext.create("info", tmp_path)
    assert ctx.dir.parent == tmp_path
    assert RUN_ID_RE.match(ctx.dir.name) and "-info-" in ctx.dir.name
    for name in ("log.txt", "status.json", "progress.log", "manifest.json"):
        assert (ctx.dir / name).exists() or name == "log.txt" or name == "progress.log"
    assert json.loads((ctx.dir / "status.json").read_text("utf-8"))["state"] == "running"
    manifest = json.loads((ctx.dir / "manifest.json").read_text("utf-8"))
    assert {"log.txt", "status.json", "progress.log"} <= set(manifest["files"])


def test_two_runs_never_share_a_directory(tmp_path: Path) -> None:
    dirs = {RunContext.create("x", tmp_path).dir for _ in range(5)}
    assert len(dirs) == 5


def test_base_comes_from_env_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(runs.ENV_RUNS, str(tmp_path / "from-env"))
    assert RunContext.create("x").dir.parent == tmp_path / "from-env"
    assert runs.runs_base(tmp_path / "explicit") == tmp_path / "explicit"


def test_default_base_is_in_the_temp_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(runs.ENV_RUNS, raising=False)
    assert runs.runs_base().name == "cad-drawings-runs"


def test_path_is_inside_the_run_tracked_and_cannot_escape(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    target = ctx.path("sub/deep/out.txt")
    assert target.parent.is_dir() and target.parent.parent.parent == ctx.dir
    assert "sub/deep/out.txt" in json.loads((ctx.dir / "manifest.json").read_text("utf-8"))["files"]
    for bad in ("../x.txt", str(tmp_path / "abs.txt"), ""):
        with pytest.raises(ValueError):
            ctx.path(bad)


def test_log_goes_to_file_and_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ctx = RunContext.create("x", tmp_path)
    ctx.log("hello zażółć")
    assert "hello zażółć" in (ctx.dir / "log.txt").read_text("utf-8")
    captured = capsys.readouterr()
    assert "hello" in captured.err and captured.out == ""


def test_progress_updates_a_readable_heartbeat(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    ctx.progress(2, 10, "layout A")
    status = json.loads((ctx.dir / "status.json").read_text("utf-8"))
    assert (status["done"], status["total"], status["note"], status["state"]) == (
        2,
        10,
        "layout A",
        "running",
    )
    assert status["pid"] == os.getpid()
    assert "2/10 layout A" in (ctx.dir / "progress.log").read_text("utf-8")
    ctx.finish()
    assert json.loads((ctx.dir / "status.json").read_text("utf-8"))["state"] == "done"


# -- outputs --------------------------------------------------------------------------------


def test_add_output_records_metadata(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    src = tmp_path / "source.bin"
    src.write_bytes(b"abc")
    out = ctx.path("out.bin")
    out.write_bytes(b"result")
    res = Result("x")
    ctx.add_output(res, "main", out, source=src)
    entry = res.outputs["main"]
    assert entry["bytes"] == 6 and entry["sha1"] == hashlib.sha1(b"result").hexdigest()
    assert entry["path"] == str(out) and "source_mtime" in entry and "mtime" in entry
    assert res.run_dir == str(ctx.dir) and res.log.endswith("log.txt")


def test_add_output_refuses_an_artifact_older_than_its_source(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    src = tmp_path / "source.bin"
    src.write_bytes(b"abc")
    out = ctx.path("out.bin")
    out.write_bytes(b"old")
    old = time.time() - 3600
    os.utime(out, (old, old))
    with pytest.raises(CadError) as err:
        ctx.add_output(Result("x"), "main", out, source=src)
    assert err.value.code == "STALE_OUTPUT"


def test_add_output_tolerates_coarse_file_system_timestamps(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    src = tmp_path / "source.bin"
    src.write_bytes(b"abc")
    out = ctx.path("out.bin")
    out.write_bytes(b"new")
    stamp = src.stat().st_mtime - 1.0
    os.utime(out, (stamp, stamp))
    ctx.add_output(Result("x"), "main", out, source=src)


def test_add_output_missing_file(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    with pytest.raises(CadError) as err:
        ctx.add_output(Result("x"), "main", tmp_path / "nope.bin")
    assert err.value.code == "OUTPUT_MISSING"


def test_sha1_of_a_non_ascii_path(tmp_path: Path) -> None:
    path = tmp_path / "zażółć gęślą" / "plik ąę.bin"
    path.parent.mkdir()
    path.write_bytes(b"data")
    assert file_sha1(path) == hashlib.sha1(b"data").hexdigest()


def test_non_ascii_run_base_and_names(tmp_path: Path) -> None:
    ctx = RunContext.create("info", tmp_path / "zażółć")
    out = ctx.path("wyniki/ąę.txt")
    out.write_text("x", encoding="utf-8")
    res = Result("info")
    ctx.add_output(res, "o", out)
    assert "ąę.txt" in res.outputs["o"]["path"]


# -- disk -----------------------------------------------------------------------------------


def test_free_gb_works_for_a_path_that_does_not_exist_yet(tmp_path: Path) -> None:
    assert free_gb(tmp_path / "not" / "yet") > 0


def test_preflight_disk_refuses_when_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "free_gb", lambda path: 1.5)
    with pytest.raises(CadError) as err:
        preflight_disk(tmp_path, min_gb=5.0)
    assert err.value.code == "DISK_LOW" and err.value.exit_code == ExitCode.BUSY
    assert "1.5" in (err.value.hint or "") and "--run-dir" in (err.value.hint or "")


def test_preflight_disk_passes_when_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "free_gb", lambda path: 50.0)
    preflight_disk(tmp_path, min_gb=5.0)


# -- staging --------------------------------------------------------------------------------


def test_stage_copy_with_a_missing_xref_does_not_touch_the_original(
    fixtures: Path, tmp_path: Path
) -> None:
    src = fixtures / "xref_missing.dxf"
    before = (file_sha1(src), src.stat().st_mtime)
    ctx = RunContext.create("x", tmp_path / "runs")
    staged = stage_copy(src, ctx)
    assert staged.parent.parent == ctx.dir and file_sha1(staged) == before[0]
    assert (file_sha1(src), src.stat().st_mtime) == before
    assert "missing_ref.dxf" in (ctx.dir / "log.txt").read_text("utf-8")
    assert not (staged.parent / "missing_ref.dxf").exists()


def test_stage_copy_brings_referenced_files_and_plot_styles_along(
    fixtures: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "projekt zażółć"
    folder.mkdir()
    (folder / "host.dxf").write_bytes((fixtures / "xref_missing.dxf").read_bytes())
    (folder / "missing_ref.dxf").write_bytes((fixtures / "plan_mm_v1.dxf").read_bytes())
    (folder / "style.ctb").write_bytes(b"table")
    (folder / "unrelated.dxf").write_bytes(b"x")
    ctx = RunContext.create("x", tmp_path / "runs")
    staged = stage_copy(folder / "host.dxf", ctx)
    assert (staged.parent / "missing_ref.dxf").is_file()
    assert (staged.parent / "style.ctb").is_file()
    assert not (staged.parent / "unrelated.dxf").exists()
    manifest = json.loads((ctx.dir / "manifest.json").read_text("utf-8"))["files"]
    assert "staged/missing_ref.dxf" in manifest and "staged/host.dxf" in manifest


def test_stage_copy_without_dependencies_copies_one_file(fixtures: Path, tmp_path: Path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.dxf").write_bytes((fixtures / "plan_mm_v1.dxf").read_bytes())
    (folder / "style.ctb").write_bytes(b"table")
    ctx = RunContext.create("x", tmp_path / "runs")
    staged = stage_copy(folder / "a.dxf", ctx, with_dependencies=False)
    assert sorted(p.name for p in staged.parent.iterdir()) == ["a.dxf"]


def test_stage_copy_missing_source(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    with pytest.raises(CadError) as err:
        stage_copy(tmp_path / "nope.dwg", ctx)
    assert err.value.code == "FILE_NOT_FOUND"


# -- pid and locks --------------------------------------------------------------------------


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid())
    assert not pid_alive(0) and not pid_alive(-5)


def test_lock_is_released_after_the_block(tmp_path: Path) -> None:
    with acquire_lock("com", base=tmp_path):
        assert (tmp_path / "locks" / "com.lock").exists()
    assert not (tmp_path / "locks" / "com.lock").exists()
    with acquire_lock("com", base=tmp_path):
        pass


def test_lock_is_reentrant_within_one_process(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "com.lock"
    with acquire_lock("com", base=tmp_path):
        with acquire_lock("com", base=tmp_path):
            assert lock.exists()
        assert lock.exists()  # only the outermost exit releases it
    assert not lock.exists()


def test_a_lock_file_of_our_own_pid_that_nobody_holds_is_stale(tmp_path: Path) -> None:
    import platform

    lock = tmp_path / "locks" / "com.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({"pid": os.getpid(), "command": "old", "started": "x", "host": platform.node()}),
        encoding="utf-8",
    )
    with acquire_lock("com", base=tmp_path):
        pass


def test_dead_owner_is_taken_over(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "com.lock"
    lock.parent.mkdir(parents=True)
    import platform

    lock.write_text(
        json.dumps({"pid": dead_pid(), "command": "old", "started": "x", "host": platform.node()}),
        encoding="utf-8",
    )
    with acquire_lock("com", base=tmp_path):
        assert json.loads(lock.read_text("utf-8"))["pid"] == os.getpid()
    assert not lock.exists()


def test_stale_unparseable_lock_is_taken_over(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "com.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    old = time.time() - 600
    os.utime(lock, (old, old))
    with acquire_lock("com", base=tmp_path):
        pass


HOLDER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from cadlib.runs import acquire_lock
    with acquire_lock("com", base=Path(sys.argv[2])):
        print("ready", flush=True)
        sys.stdin.read()
    """
)


def test_live_owner_in_another_process_blocks_us(tmp_path: Path) -> None:
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    base = tmp_path / "base"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(SKILL / "scripts"), str(base)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        with pytest.raises(CadError) as err, acquire_lock("com", base=base):
            pass
        assert err.value.code == "LOCKED" and err.value.exit_code == ExitCode.BUSY
        # the venv launcher on Windows is a stub: the lock holds the real interpreter pid
        owner = json.loads((base / "locks" / "com.lock").read_text("utf-8"))
        assert f"owner pid {owner['pid']}" in (err.value.hint or "")
        assert pid_alive(owner["pid"])
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
    with acquire_lock("com", base=base):  # released when the owner exited
        pass


# -- cache ----------------------------------------------------------------------------------


def test_cache_roundtrip_keyed_by_hash_and_converter(tmp_path: Path) -> None:
    dxf = tmp_path / "a.dxf"
    dxf.write_text("0\nEOF\n", encoding="utf-8")
    sha = "a" * 40
    assert cache_get(sha, "com", base=tmp_path) is None
    stored = cache_put(sha, "com", dxf, base=tmp_path)
    assert stored.parent == tmp_path / "cache" and stored.read_text("utf-8") == dxf.read_text(
        "utf-8"
    )
    assert cache_get(sha, "com", base=tmp_path) == stored
    assert cache_get(sha, "oda", base=tmp_path) is None
    assert cache_get("b" * 40, "com", base=tmp_path) is None
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_cache_in_a_non_ascii_base(tmp_path: Path) -> None:
    dxf = tmp_path / "a.dxf"
    dxf.write_text("x", encoding="utf-8")
    base = tmp_path / "pamięć podręczna"
    cache_put("c" * 40, "oda", dxf, base=base)
    assert cache_get("c" * 40, "oda", base=base) is not None


# -- hardening: takeover, children, current context ---------------------------------------


def _payload(pid: int, **extra: object) -> dict[str, object]:
    import platform

    return {"pid": pid, "command": "x", "started": "t", "host": platform.node(), **extra}


def test_takeover_removes_a_stale_lock_by_unique_rename(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "com.lock"
    lock.parent.mkdir(parents=True)
    dead = _payload(dead_pid())
    lock.write_text(json.dumps(dead), encoding="utf-8")
    assert runs._take_over(lock, dead) is True
    assert not lock.exists() and not list(lock.parent.glob("*stale*"))


def test_takeover_gives_a_live_lock_back_when_it_was_replaced_meanwhile(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "com.lock"
    lock.parent.mkdir(parents=True)
    live = _payload(os.getpid(), marker="fresh")
    lock.write_text(json.dumps(live), encoding="utf-8")
    assert runs._take_over(lock, _payload(dead_pid())) is False
    assert json.loads(lock.read_text("utf-8"))["marker"] == "fresh"
    assert not list(lock.parent.glob("*stale*"))


def test_current_context_is_set_by_create_and_cleared_by_finish(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    assert runs.current_context() is ctx
    ctx.finish()
    assert runs.current_context() is None


def test_finish_failed_is_idempotent_and_beats_done(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    ctx.finish("done")
    ctx.finish("failed")
    ctx.finish("failed")
    ctx.finish("done")
    assert json.loads((ctx.dir / "status.json").read_text("utf-8"))["state"] == "failed"


def test_note_child_records_the_process_in_status_and_lock(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path)
    with acquire_lock("com", base=tmp_path):
        runs.note_child(4242, "acad.exe")
        owner = json.loads((tmp_path / "locks" / "com.lock").read_text("utf-8"))
        assert owner["children"][0]["pid"] == 4242 and owner["children"][0]["image"] == "acad.exe"
    status = json.loads((ctx.dir / "status.json").read_text("utf-8"))
    child = status["children"][0]
    assert (child["pid"], child["image"], child["role"]) == (4242, "acad.exe", "cad")
    assert child["started"].endswith("+00:00")
    ctx.progress(1, 2)  # later heartbeats keep the record
    assert json.loads((ctx.dir / "status.json").read_text("utf-8"))["children"]
    ctx.finish()


def test_note_child_without_a_run_is_harmless() -> None:
    runs.note_child(1, "x.exe")


def test_takeover_warns_about_live_children_of_the_dead_owner(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "com.lock"
    lock.parent.mkdir(parents=True)
    child = {"pid": os.getpid(), "image": "python.exe", "role": "cad", "started": "t"}
    lock.write_text(json.dumps(_payload(dead_pid(), children=[child])), encoding="utf-8")
    with acquire_lock("com", base=tmp_path) as info:
        assert any(str(os.getpid()) in w for w in info.warnings)
    assert pid_alive(os.getpid())  # nothing was killed


def test_default_base_is_per_user_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(runs.ENV_RUNS, raising=False)
    monkeypatch.setattr(runs, "_POSIX", True)
    monkeypatch.setattr(runs, "_current_uid", lambda: 4711)
    monkeypatch.setattr(runs.tempfile, "gettempdir", lambda: str(tmp_path))
    assert runs.runs_base().name == "cad-drawings-runs-4711"


def test_posix_base_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(runs.ENV_RUNS, raising=False)
    monkeypatch.setattr(runs, "_POSIX", True)
    monkeypatch.setattr(runs, "_current_uid", lambda: 987654)  # never the owner of tmp_path
    monkeypatch.setattr(runs.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "cad-drawings-runs-987654").mkdir()
    with pytest.raises(CadError) as err:
        RunContext.create("x")
    assert err.value.code == "RUNS_DIR_UNSAFE"


def test_cache_get_ignores_a_file_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dxf = tmp_path / "a.dxf"
    dxf.write_text("x", encoding="utf-8")
    cache_put("d" * 40, "oda", dxf, base=tmp_path)
    monkeypatch.setattr(runs, "_POSIX", True)
    monkeypatch.setattr(runs, "_current_uid", lambda: 987654)
    assert cache_get("d" * 40, "oda", base=tmp_path) is None
    monkeypatch.setattr(runs, "_current_uid", lambda: (tmp_path / "cache").stat().st_uid)
    assert cache_get("d" * 40, "oda", base=tmp_path) is not None


def test_an_empty_cache_file_is_a_miss(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / f"{'e' * 40}-oda.dxf").write_bytes(b"")
    assert cache_get("e" * 40, "oda", base=tmp_path) is None


# -- hardening: stage_copy path safety ------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "\\xrefs\\a.dxf",
        "C:a.dxf",
        "C:\\x\\a.dxf",
        "\\\\server\\share\\a.dxf",
        "/etc/a.dxf",
        "../a.dxf",
        "sub/../../a.dxf",
        "a.dxf:stream",
        "",
        "//host/share/a.dxf",
    ],
)
def test_unsafe_xref_references_are_rejected(ref: str) -> None:
    assert runs._safe_relative(ref) is None


@pytest.mark.parametrize("ref", ["a.dxf", "sub\\a.dxf", "sub/a.dxf", "./a.dxf"])
def test_safe_xref_references_are_kept_relative(ref: str) -> None:
    rel = runs._safe_relative(ref)
    assert rel is not None and not rel.is_absolute() and ".." not in rel.parts


def _host_with_xrefs(path: Path, refs: list[str]) -> None:
    import ezdxf
    from ezdxf import xref

    doc = ezdxf.new("R2018")
    for i, ref in enumerate(refs):
        xref.define(doc, f"REF{i}", filename=ref)
    doc.saveas(path)


def test_stage_copy_skips_unsafe_references_and_reports_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "proj"
    (folder / "ok").mkdir(parents=True)
    (folder / "ok" / "sub.dxf").write_bytes(b"x")
    (tmp_path / "outside.dxf").write_bytes(b"secret")
    refs = ["ok\\sub.dxf", "..\\outside.dxf", "\\\\server\\share\\a.dxf", "C:a.dxf", "\\root.dxf"]
    _host_with_xrefs(folder / "host.dxf", refs)
    touched: list[str] = []
    real = runs._copy_beside

    def spy(src_root: Path, rel: Path, dest_root: Path, ctx: RunContext) -> bool:
        touched.append(rel.as_posix())
        return real(src_root, rel, dest_root, ctx)

    monkeypatch.setattr(runs, "_copy_beside", spy)
    ctx = RunContext.create("x", tmp_path / "runs")
    report = runs.stage_copy_report(folder / "host.dxf", ctx)
    assert touched == ["ok/sub.dxf"]  # unsafe references never reach the file system
    assert (report.path.parent / "ok" / "sub.dxf").is_file()
    assert not (tmp_path / "runs" / "outside.dxf").exists()
    assert len(report.skipped) == 4 and report.copied == 1
    log = (ctx.dir / "log.txt").read_text("utf-8")
    assert "server" in log and "outside.dxf" in log
    other = RunContext.create("y", tmp_path / "runs")
    assert runs.stage_copy(folder / "host.dxf", other).is_file()  # old API still returns a Path


def test_stage_copy_refuses_a_target_that_escapes_the_stage_dir(tmp_path: Path) -> None:
    ctx = RunContext.create("x", tmp_path / "runs")
    root = ctx.subdir("staged")
    assert runs._inside(root, root / "a" / "b.dxf") is True
    assert runs._inside(root, root.parent / "evil.dxf") is False


def test_dwg_sources_say_their_xrefs_are_not_resolved(tmp_path: Path) -> None:
    src = tmp_path / "a.dwg"
    src.write_bytes(b"AC1032")
    ctx = RunContext.create("x", tmp_path / "runs")
    stage_copy(src, ctx)
    assert "not resolved" in (ctx.dir / "log.txt").read_text("utf-8")
