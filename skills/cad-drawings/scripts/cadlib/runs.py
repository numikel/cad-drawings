"""Run directories, disk preflight, working copies, locks and the DXF cache.

Layout: ``<base>/<YYYYmmdd-HHMMSS>-<command>-<4 hex>/`` holding ``log.txt``, ``status.json``,
``progress.log``, ``manifest.json`` and the outputs. ``manifest.json`` lists every file the tool
created inside the run (relative POSIX names); ``cleanup`` deletes only what the manifest names.

Base directory: ``--run-dir`` / the ``base`` argument, else env ``CAD_DRAWINGS_RUNS``, else
``<user temp>/cad-drawings-runs`` (POSIX: ``cad-drawings-runs-<uid>``, mode 0700, ownership
checked). Never next to the source file.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from .result import CadError, ExitCode, Result

ENV_RUNS = "CAD_DRAWINGS_RUNS"
MANIFEST_NAME = "manifest.json"
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9_-]+-[0-9a-f]{4}$")
CACHE_NAME_RE = re.compile(r"^[0-9a-f]{40}-[A-Za-z0-9_.]+\.dxf$")
# File systems with coarse timestamps (FAT: 2 s) must not turn a fresh artifact into a stale one.
MTIME_TOLERANCE_S = 2.0
# A lock file that cannot be parsed is treated as "being written" for this long.
LOCK_GRACE_S = 5.0
MAX_PLOT_STYLE_FILES = 50
PLOT_STYLE_SUFFIXES = (".ctb", ".stb")
# A "running" run whose heartbeat is older than this is not protected (PID reuse safety net).
STALE_RUNNING_S = 6 * 3600.0
_POSIX = os.name == "posix"


def _current_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else -1


def _default_base() -> Path:
    tmp = Path(tempfile.gettempdir()).absolute()
    return tmp / (f"cad-drawings-runs-{_current_uid()}" if _POSIX else "cad-drawings-runs")


def runs_base(base: Path | None = None) -> Path:
    """Resolve the directory that holds run directories, locks and the cache."""
    if base is not None:
        return Path(base).expanduser().absolute()
    env = os.environ.get(ENV_RUNS)
    if env:
        return Path(env).expanduser().absolute()
    return _default_base()


def _prepare_base(root: Path) -> None:
    """Create the base. On POSIX the per-user default must be a private directory that is ours."""
    if not (_POSIX and root == _default_base()):
        root.mkdir(parents=True, exist_ok=True)
        return
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _current_uid()
    ):
        raise CadError(
            "RUNS_DIR_UNSAFE",
            f"{root} exists but is not a private directory owned by this user",
            hint="remove it, or point CAD_DRAWINGS_RUNS / --run-dir at a directory of your own",
        )
    if info.st_mode & 0o077:
        os.chmod(root, 0o700)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> None:
    """Atomic JSON write (readers never see a half-written file)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    last: OSError | None = None
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # Windows: a reader holds the target open
            last = exc
            time.sleep(0.02 * (attempt + 1))
    with contextlib.suppress(OSError):
        tmp.unlink()
    if last is not None:
        raise last


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_ancestor(path: Path) -> Path:
    path = Path(path).absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def free_gb(path: Path) -> float:
    """Free space (GiB) on the volume that holds ``path`` (or its nearest existing parent)."""
    return shutil.disk_usage(_existing_ancestor(path)).free / (1024**3)


def preflight_disk(path: Path, min_gb: float = 5.0) -> None:
    free = free_gb(path)
    if free < min_gb:
        raise CadError(
            "DISK_LOW",
            f"only {free:.1f} GB free where {path} lives; at least {min_gb:.1f} GB are needed",
            exit_code=ExitCode.BUSY,
            hint=(
                f"free space on that drive or pick another one with --run-dir "
                f"(now {free:.1f} GB free)"
            ),
        )


class RunContext:
    """One fresh run directory with a log, a heartbeat and a manifest of created files."""

    def __init__(self, directory: Path, command: str) -> None:
        self.dir = directory
        self.command = command
        self.started = time.time()
        self._files: list[str] = []
        self._bound: Result | None = None
        self._state = "running"
        self.children: list[dict[str, Any]] = []

    # -- creation -------------------------------------------------------------------------
    @classmethod
    def create(cls, command: str, base: Path | None = None) -> RunContext:
        global _CURRENT
        root = runs_base(base)
        _prepare_base(root)
        stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^A-Za-z0-9_]+", "-", command).strip("-") or "run"
        for _ in range(20):
            candidate = root / f"{stamp}-{slug}-{secrets.token_hex(2)}"
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            ctx = cls(candidate, command)
            for name in ("log.txt", "status.json", "progress.log"):
                ctx._track(name, write=False)
            ctx._write_manifest()
            ctx._write_status("running")
            _CURRENT = ctx
            return ctx
        raise CadError("RUN_DIR", f"cannot create a run directory in {root}")

    # -- paths ----------------------------------------------------------------------------
    def _track(self, rel: str, write: bool = True) -> None:
        if rel not in self._files:
            self._files.append(rel)
            if write:
                self._write_manifest()

    def _write_manifest(self) -> None:
        _write_json(
            self.dir / MANIFEST_NAME,
            {
                "version": 1,
                "command": self.command,
                "started": _iso(self.started),
                "pid": os.getpid(),
                "files": self._files,
            },
        )

    def path(self, name: str) -> Path:
        """Path of a file to create inside the run dir (parent created, tracked in the manifest)."""
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(f"run path must be relative and stay inside the run dir: {name!r}")
        target = self.dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        self._track(rel.as_posix())
        return target

    def subdir(self, name: str) -> Path:
        """A directory inside the run dir (created); its files must be registered via ``path``."""
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"run path must be relative and stay inside the run dir: {name!r}")
        target = self.dir / rel
        target.mkdir(parents=True, exist_ok=True)
        return target

    def rel(self, path: Path) -> str | None:
        """POSIX name of ``path`` relative to the run dir, or None when it is outside."""
        try:
            return Path(path).absolute().relative_to(self.dir.absolute()).as_posix()
        except ValueError:
            return None

    # -- logging and heartbeat --------------------------------------------------------------
    def log(self, message: str) -> None:
        line = f"{_iso(time.time())} {message}"
        with (self.dir / "log.txt").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(message, file=sys.stderr)

    def _write_status(self, state: str, **extra: Any) -> None:
        data = {
            "command": self.command,
            "pid": os.getpid(),
            "state": state,
            "started": _iso(self.started),
            "updated": _iso(time.time()),
            **extra,
        }
        if self.children:
            data["children"] = self.children
        with contextlib.suppress(OSError):  # a heartbeat must never break the command
            _write_json(self.dir / "status.json", data)

    def progress(self, done: int, total: int, note: str = "") -> None:
        with (self.dir / "progress.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{_iso(time.time())} {done}/{total} {note}".rstrip() + "\n")
        self._write_status("running", done=done, total=total, note=note)

    def finish(self, state: str = "done") -> None:
        """Close the run. The first state wins, except that ``failed`` always overrides ``done``."""
        global _CURRENT
        if self._state != "running" and not (state == "failed" and self._state != "failed"):
            return
        self._state = state
        self._write_status(state)
        if state != "failed" and _CURRENT is self:
            _CURRENT = None  # a failed run stays current so cad.py can attach run_dir/log

    # -- results ----------------------------------------------------------------------------
    def bind(self, result: Result) -> None:
        """Point the result at this run (run_dir and log)."""
        result.run_dir = str(self.dir)
        result.log = str(self.dir / "log.txt")

    def add_output(self, result: Result, name: str, path: Path, source: Path | None = None) -> None:
        """Register an artifact; refuse one that is older than its source."""
        path = Path(path)
        if not path.is_file():
            raise CadError("OUTPUT_MISSING", f"expected output was not written: {path}")
        stat = path.stat()
        entry: dict[str, Any] = {
            "path": str(path),
            "bytes": stat.st_size,
            "mtime": _iso(stat.st_mtime),
        }
        if source is not None:
            src_mtime = Path(source).stat().st_mtime
            if stat.st_mtime + MTIME_TOLERANCE_S < src_mtime:
                raise CadError(
                    "STALE_OUTPUT",
                    f"{path.name} is older than its source {Path(source).name}",
                    hint="regenerate it into a fresh run directory; never reuse an older artifact",
                )
            entry["source_mtime"] = _iso(src_mtime)
        entry["sha1"] = file_sha1(path)
        rel = self.rel(path)
        if rel is not None:
            self._track(rel)
        result.outputs[name] = entry
        self.bind(result)


_CURRENT: RunContext | None = None


def current_context() -> RunContext | None:
    """The run created last and not yet finished (or finished as failed), if any."""
    return _CURRENT


def note_child(pid: int, image: str, role: str = "cad") -> None:
    """Record a child process this run started (status.json and held lock files).

    A later run that finds the owner dead can then report the child instead of leaving it
    unnoticed. Nothing is ever killed because of this record.
    """
    record = {"pid": int(pid), "image": image, "role": role, "started": _iso(time.time())}
    ctx = _CURRENT
    if ctx is not None:
        ctx.children.append(record)
        ctx._write_status(ctx._state)
    with _held_guard:
        for path, held in _HELD.items():
            held.payload.setdefault("children", []).append(record)
            with contextlib.suppress(OSError):
                _write_json(path, held.payload)


# -- working copies -------------------------------------------------------------------------


def _xref_paths(path: Path, ctx: RunContext) -> list[str]:
    """Paths of external references declared in a DXF file (best effort)."""
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError:
        ctx.log("ezdxf missing: xrefs of the source cannot be listed")
        return []
    try:
        try:
            doc = ezdxf.readfile(path)
        except (ezdxf.DXFError, OSError):
            doc, _ = recover.readfile(str(path))
    except (ezdxf.DXFError, OSError) as exc:
        ctx.log(f"cannot read xrefs from {path.name}: {exc}")
        return []
    found: list[str] = []
    for block in doc.blocks:
        ref = str(getattr(block.block.dxf, "xref_path", "") or "")
        if ref:
            found.append(ref)
    return found


def _copy_beside(src_root: Path, rel: Path, dest_root: Path, ctx: RunContext) -> bool:
    origin = src_root / rel
    if not origin.is_file():
        return False
    target = dest_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, target)
    tracked = ctx.rel(target)
    if tracked is not None:
        ctx._track(tracked)
    return True


def _safe_relative(ref: str) -> Path | None:
    """A reference as a plain relative path, or None when it is absolute, rooted, on a drive,
    UNC, an alternate data stream, or climbs out with ``..``. Pure string work, no file access."""
    if not ref or "\x00" in ref:
        return None
    win = PureWindowsPath(ref)  # understands both separators
    if win.drive or win.root or ref.startswith(("/", "\\")):
        return None
    parts = [part for part in win.parts if part not in (".", "")]
    if not parts or any(part == ".." or ":" in part for part in parts):
        return None
    return Path(*parts)


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


@dataclass
class StageResult:
    path: Path
    copied: int = 0
    skipped: list[str] = field(default_factory=list)


def stage_copy_report(src: Path, ctx: RunContext, with_dependencies: bool = True) -> StageResult:
    """Copy ``src`` into the run dir so tools can open it without touching the original.

    With dependencies, files the source references that sit beside it (or below it) are copied
    with the same relative layout: xref files (listed from DXF sources) and plot-style tables.
    A reference is only followed when it is a plain relative path that stays inside the source
    folder; absolute, rooted, drive, UNC and ``..`` references are skipped before any file
    access. Skipped and missing references are logged and listed in ``StageResult.skipped``.
    The xrefs of DWG sources are not resolved (their references are not readable without CAD).
    """
    src = Path(src).absolute()
    if not src.is_file():
        raise CadError(
            "FILE_NOT_FOUND", f"source file not found: {src}", exit_code=ExitCode.BAD_ARGS
        )
    stage_root = ctx.subdir("staged")
    staged = ctx.path(f"staged/{src.name}")
    shutil.copy2(src, staged)
    report = StageResult(staged)
    if not with_dependencies:
        return report

    def skip(message: str) -> None:
        report.skipped.append(message)
        ctx.log(message)

    suffix = src.suffix.lower()
    if suffix == ".dxf":
        for ref in _xref_paths(src, ctx):
            rel = _safe_relative(ref)
            if rel is None:
                skip(f"xref skipped (absolute, drive, UNC or '..' path): {ref}")
            elif not (
                _inside(src.parent, src.parent / rel) and _inside(stage_root, stage_root / rel)
            ):
                skip(f"xref skipped (resolves outside the source folder): {ref}")
            elif _copy_beside(src.parent, rel, stage_root, ctx):
                report.copied += 1
            else:
                skip(f"xref not found beside the source: {ref}")
    elif suffix == ".dwg":
        ctx.log("DWG source: its xrefs are not resolved (convert to DXF to list them)")
    styles = sorted(
        p for p in src.parent.iterdir() if p.is_file() and p.suffix.lower() in PLOT_STYLE_SUFFIXES
    )
    for style in styles[:MAX_PLOT_STYLE_FILES]:
        if _copy_beside(src.parent, Path(style.name), stage_root, ctx):
            report.copied += 1
    ctx.log(f"staged {src.name} with {report.copied} dependency file(s)")
    return report


def stage_copy(src: Path, ctx: RunContext, with_dependencies: bool = True) -> Path:
    """``stage_copy_report`` returning only the staged path."""
    return stage_copy_report(src, ctx, with_dependencies).path


# -- locks ----------------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """True when a process with this PID is running (portable; never signals the process)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)  # signal 0 only checks existence on POSIX
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    # os.kill(pid, 0) would TERMINATE the process on Windows; query it instead.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied  # exists, but not ours to query
    try:
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _lock_path(name: str, base: Path | None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "lock"
    return runs_base(base) / "locks" / f"{safe}.lock"


def _read_lock(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lock_busy(path: Path, owner: dict[str, Any] | None) -> CadError | None:
    """A CadError when the lock is held by a live owner, else None (stale: take it over).

    A lock file that names our own PID but is not held by this process is stale (PID reuse).
    """
    if owner is None:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None  # vanished meanwhile
        if age < LOCK_GRACE_S:
            return CadError(
                "LOCKED",
                f"lock {path.name} is being created by another process",
                exit_code=ExitCode.BUSY,
                hint="wait a few seconds and retry",
            )
        return None
    pid = owner.get("pid")
    same_host = owner.get("host") in (None, platform.node())
    alive = not same_host or (isinstance(pid, int) and pid != os.getpid() and pid_alive(pid))
    if not alive:
        return None
    return CadError(
        "LOCKED",
        f"{path.stem} is in use by another run",
        exit_code=ExitCode.BUSY,
        hint=(
            f"owner pid {pid}, command {owner.get('command')!r}, started {owner.get('started')}"
            f"; wait for it to finish (a dead owner is taken over automatically)"
        ),
    )


def _take_over(path: Path, expected: dict[str, Any] | None) -> bool:
    """Remove a stale lock by renaming it to a unique name, then re-verify what was moved.

    Returns True when the moved file was the stale lock we judged dead (it is deleted). If
    somebody replaced it with a live lock in the meantime, that lock is put back and False is
    returned (the caller looks again and finds a live owner).
    """
    moved = path.with_name(f"{path.name}.stale-{os.getpid()}-{secrets.token_hex(3)}")
    try:
        os.replace(path, moved)
    except FileNotFoundError:
        return True  # already gone; the caller just retries the exclusive create
    except PermissionError:
        raise CadError(
            "LOCKED",
            f"{path.stem} is in use by another run",
            exit_code=ExitCode.BUSY,
            hint="the lock file cannot be moved; another process holds it open",
        ) from None
    if _read_lock(moved) == expected:
        with contextlib.suppress(OSError):
            moved.unlink()
        return True
    try:
        os.link(moved, path)  # give the live lock back (fails if a new one exists already)
    except OSError:
        with contextlib.suppress(OSError):
            if not path.exists():
                os.rename(moved, path)
    with contextlib.suppress(OSError):
        moved.unlink()
    return False


def _child_warnings(owner: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for child in owner.get("children") or []:
        pid = child.get("pid") if isinstance(child, dict) else None
        if isinstance(pid, int) and pid_alive(pid):
            out.append(
                f"{child.get('image', 'process')} (pid {pid}) started by a dead run is still "
                "running; it was not stopped"
            )
    return out


@dataclass
class LockInfo:
    """Yielded by ``acquire_lock``; ``warnings`` lists leftovers of a dead previous owner."""

    path: Path
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Held:
    payload: dict[str, Any]
    count: int = 1


_HELD: dict[Path, _Held] = {}
_held_guard = threading.RLock()


def _create_lock(path: Path, payload: dict[str, Any]) -> LockInfo:
    info = LockInfo(path)
    for _ in range(6):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = _read_lock(path)
            busy = _lock_busy(path, owner)
            if busy is not None:
                raise busy from None
            if owner is not None:
                info.warnings += _child_warnings(owner)
            _take_over(path, owner)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        for message in info.warnings:
            if _CURRENT is not None:
                _CURRENT.log(message)
        return info
    raise CadError(
        "LOCKED", f"could not take lock {path.stem}", exit_code=ExitCode.BUSY, hint="retry"
    )


@contextlib.contextmanager
def acquire_lock(name: str, *, base: Path | None = None) -> Iterator[LockInfo]:
    """Exclusive named lock. A dead owner is taken over; a live one raises CadError LOCKED.

    Re-entrant within one process (reference counted): only the outermost exit releases it.
    Takeover renames the stale file to a unique name and re-verifies it before deleting; if the
    dead owner had recorded child processes that still run, they are reported in
    ``LockInfo.warnings`` (and the run log), never killed.
    """
    path = _lock_path(name, base)
    with _held_guard:
        held = _HELD.get(path)
        if held is not None:
            held.count += 1
    if held is None:
        _prepare_base(runs_base(base))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "command": " ".join([Path(sys.argv[0]).name, *sys.argv[1:2]])[:200],
            "started": _iso(time.time()),
            "host": platform.node(),
        }
        info = _create_lock(path, payload)
        with _held_guard:
            held = _HELD[path] = _Held(payload)
    else:
        info = LockInfo(path)
    try:
        yield info
    finally:
        with _held_guard:
            held.count -= 1
            release = held.count == 0
            if release:
                _HELD.pop(path, None)
        if release:
            current = _read_lock(path)
            if current is not None and current.get("pid") == os.getpid():
                with contextlib.suppress(OSError):
                    path.unlink()


# -- cache ----------------------------------------------------------------------------------


def _cache_file(sha1: str, kind: str, base: Path | None) -> Path:
    safe_kind = re.sub(r"[^A-Za-z0-9_.]+", "_", kind) or "x"
    return runs_base(base) / "cache" / f"{sha1}-{safe_kind}.dxf"


def _owned_by_me(info: os.stat_result) -> bool:
    return not _POSIX or info.st_uid == _current_uid()


def cache_get(sha1: str, kind: str, *, base: Path | None = None) -> Path | None:
    """Cached DXF for a source hash and converter id, or None. Treat the file as read-only.

    On POSIX a file that is not a regular, non-empty file owned by this user is a miss.
    """
    path = _cache_file(sha1, kind, base)
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or not _owned_by_me(info):
        return None
    return path


def cache_put(sha1: str, kind: str, src: Path, *, base: Path | None = None) -> Path:
    path = _cache_file(sha1, kind, base)
    _prepare_base(runs_base(base))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, path)
    return path
