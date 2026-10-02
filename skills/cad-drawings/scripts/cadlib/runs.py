"""Run directories, disk preflight, working copies, locks and the DXF cache.

Layout: ``<base>/<YYYYmmdd-HHMMSS>-<command>-<4 hex>/`` holding ``log.txt``, ``status.json``,
``progress.log``, ``manifest.json`` and the outputs. ``manifest.json`` lists every file the tool
created inside the run (relative POSIX names); ``cleanup`` deletes only what the manifest names.

Base directory: ``--run-dir`` / the ``base`` argument, else env ``CAD_DRAWINGS_RUNS``, else
``<user temp>/cad-drawings-runs``. Never next to the source file.
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
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
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


def runs_base(base: Path | None = None) -> Path:
    """Resolve the directory that holds run directories, locks and the cache."""
    if base is not None:
        return Path(base).expanduser().absolute()
    env = os.environ.get(ENV_RUNS)
    if env:
        return Path(env).expanduser().absolute()
    return Path(tempfile.gettempdir()).absolute() / "cad-drawings-runs"


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

    # -- creation -------------------------------------------------------------------------
    @classmethod
    def create(cls, command: str, base: Path | None = None) -> RunContext:
        root = runs_base(base)
        root.mkdir(parents=True, exist_ok=True)
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
        with contextlib.suppress(OSError):  # a heartbeat must never break the command
            _write_json(self.dir / "status.json", data)

    def progress(self, done: int, total: int, note: str = "") -> None:
        with (self.dir / "progress.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{_iso(time.time())} {done}/{total} {note}".rstrip() + "\n")
        self._write_status("running", done=done, total=total, note=note)

    def finish(self, state: str = "done") -> None:
        self._write_status(state)

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


def stage_copy(src: Path, ctx: RunContext, with_dependencies: bool = True) -> Path:
    """Copy ``src`` into the run dir so tools can open it without touching the original.

    With dependencies, files the source references that sit beside it (or below it) are copied
    with the same relative layout: xref files (listed from DXF sources) and plot-style tables.
    Missing or unreachable references are logged, never fatal.
    """
    src = Path(src).absolute()
    if not src.is_file():
        raise CadError(
            "FILE_NOT_FOUND", f"source file not found: {src}", exit_code=ExitCode.BAD_ARGS
        )
    stage_root = ctx.subdir("staged")
    staged = ctx.path(f"staged/{src.name}")
    shutil.copy2(src, staged)
    if not with_dependencies:
        return staged
    copied = 0
    if src.suffix.lower() == ".dxf":
        for ref in _xref_paths(src, ctx):
            candidate = Path(ref.replace("\\", "/"))
            if candidate.is_absolute():
                try:
                    candidate = candidate.relative_to(src.parent)
                except ValueError:
                    ctx.log(f"xref outside the source folder, not copied: {ref}")
                    continue
            if ".." in candidate.parts:
                ctx.log(f"xref outside the source folder, not copied: {ref}")
                continue
            if _copy_beside(src.parent, candidate, stage_root, ctx):
                copied += 1
            else:
                ctx.log(f"xref not found beside the source: {ref}")
    styles = sorted(
        p for p in src.parent.iterdir() if p.is_file() and p.suffix.lower() in PLOT_STYLE_SUFFIXES
    )
    for style in styles[:MAX_PLOT_STYLE_FILES]:
        if _copy_beside(src.parent, Path(style.name), stage_root, ctx):
            copied += 1
    ctx.log(f"staged {src.name} with {copied} dependency file(s)")
    return staged


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
    """A CadError when the lock is held by a live owner, else None (stale: take it over)."""
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
    alive = not same_host or (isinstance(pid, int) and pid_alive(pid))
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


@contextlib.contextmanager
def acquire_lock(name: str, *, base: Path | None = None) -> Iterator[None]:
    """Exclusive named lock. A dead owner is taken over; a live one raises CadError LOCKED.

    The takeover re-reads the lock right before deleting it, which narrows (but cannot fully
    close) the race between two processes that both find the same stale lock.
    """
    path = _lock_path(name, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "command": " ".join([Path(sys.argv[0]).name, *sys.argv[1:2]])[:200],
        "started": _iso(time.time()),
        "host": platform.node(),
    }
    acquired = False
    for _ in range(4):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = _read_lock(path)
            busy = _lock_busy(path, owner)
            if busy is not None:
                raise busy from None
            if _read_lock(path) != owner:
                continue  # somebody else took it over first; look again
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                raise CadError(
                    "LOCKED",
                    f"{path.stem} is in use by another run",
                    exit_code=ExitCode.BUSY,
                    hint="the lock file cannot be removed; another process holds it open",
                ) from None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        acquired = True
        break
    if not acquired:
        raise CadError(
            "LOCKED", f"could not take lock {path.stem}", exit_code=ExitCode.BUSY, hint="retry"
        )
    try:
        yield
    finally:
        current = _read_lock(path)
        if current is not None and current.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                path.unlink()


# -- cache ----------------------------------------------------------------------------------


def _cache_file(sha1: str, kind: str, base: Path | None) -> Path:
    safe_kind = re.sub(r"[^A-Za-z0-9_.]+", "_", kind) or "x"
    return runs_base(base) / "cache" / f"{sha1}-{safe_kind}.dxf"


def cache_get(sha1: str, kind: str, *, base: Path | None = None) -> Path | None:
    """Cached DXF for a source hash and converter id, or None. Treat the file as read-only."""
    path = _cache_file(sha1, kind, base)
    try:
        return path if path.stat().st_size > 0 else None
    except OSError:
        return None


def cache_put(sha1: str, kind: str, src: Path, *, base: Path | None = None) -> Path:
    path = _cache_file(sha1, kind, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, path)
    return path
