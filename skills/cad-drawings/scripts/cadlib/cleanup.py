"""``cleanup``: list and delete run directories and cache entries the tool itself created.

Safety rules:
- only files named in a run's ``manifest.json`` are deleted, one file at a time; a file that is
  not in the manifest is never touched (the run directory then stays, with a note);
- directories are removed with ``rmdir`` (empty ones only), never recursively;
- nothing is deleted without ``--yes``; ``--dry-run`` shows the plan;
- runs that look active (fresh heartbeat of a live process) are skipped;
- ``--orphans DIR`` only reports ``.dwl`` / ``.dwl2`` lock files, it never deletes them.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import doctor as _doctor
from .command import Command
from .result import CadError, ExitCode, Result
from .runs import (
    CACHE_NAME_RE,
    MANIFEST_NAME,
    RUN_ID_RE,
    STALE_RUNNING_S,
    pid_alive,
    runs_base,
)

MAX_LISTED = 20
MAX_ORPHAN_SCAN = 2000
DAY_S = 86400.0


@dataclass
class RunInfo:
    run_id: str
    path: Path
    started: float
    size: int
    files: list[str]
    active: bool
    has_manifest: bool
    state: str = ""

    def brief(self, now: float) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.run_id,
            "mb": round(self.size / 1024**2, 2),
            "age_days": round((now - self.started) / DAY_S, 1),
        }
        if self.state:
            item["state"] = self.state
        if self.active:
            item["active"] = True
        if not self.has_manifest:
            item["no_manifest"] = True
        return item


@dataclass
class Outcome:
    deleted_files: int = 0
    deleted_bytes: int = 0
    removed_runs: int = 0
    kept: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).stat().st_size
    return total


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(text: Any, fallback: float) -> float:
    try:
        return dt.datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return fallback


def scan_runs(base: Path, now: float | None = None) -> list[RunInfo]:
    """Run directories under ``base``, newest first."""
    now = time.time() if now is None else now
    runs: list[RunInfo] = []
    if not base.is_dir():
        return runs
    for entry in base.iterdir():
        if not (entry.is_dir() and RUN_ID_RE.match(entry.name)):
            continue
        manifest = _read_json(entry / MANIFEST_NAME)
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        status = _read_json(entry / "status.json") or {}
        updated = _parse_iso(status.get("updated"), 0.0)
        pid = status.get("pid")
        running = status.get("state") == "running"
        # a live owner protects its run; "running" with a dead owner is a crashed (failed) run
        active = (
            running and now - updated < STALE_RUNNING_S and isinstance(pid, int) and pid_alive(pid)
        )
        state = "failed" if running and not active else str(status.get("state", ""))
        files = [str(f) for f in (manifest or {}).get("files", []) if isinstance(f, str)]
        runs.append(
            RunInfo(
                run_id=entry.name,
                path=entry,
                started=_parse_iso((manifest or {}).get("started"), mtime),
                size=_tree_size(entry),
                files=files,
                active=active,
                has_manifest=manifest is not None,
                state=state,
            )
        )
    runs.sort(key=lambda r: r.started, reverse=True)
    return runs


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def delete_run(run: RunInfo, out: Outcome) -> None:
    """Delete the manifest-tracked files of one run, then its emptied directories."""
    root = run.path
    for rel in sorted(set(run.files), key=lambda r: (-r.count("/"), r)):
        target = root / rel
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            out.failures.append(f"{run.run_id}: refused unsafe manifest entry {rel!r}")
            continue
        if target.is_symlink():
            size, link = 0, True
        else:
            link = False
            if not target.is_file():
                continue
            if not _inside(root, target):
                out.failures.append(f"{run.run_id}: refused {rel!r} (outside the run)")
                continue
            size = target.stat().st_size
        try:
            target.unlink()
        except OSError as exc:
            out.failures.append(f"{run.run_id}: cannot delete {rel}: {exc}")
            continue
        out.deleted_files += 1
        out.deleted_bytes += 0 if link else size
    # emptied sub-directories, deepest first; rmdir refuses non-empty ones
    dirs = sorted(
        {p for rel in run.files for p in Path(rel).parents if str(p) != "."},
        key=lambda p: -len(p.parts),
    )
    for rel_dir in dirs:
        with contextlib.suppress(OSError):
            (root / rel_dir).rmdir()
    leftovers = [p.name for p in root.iterdir() if p.name != MANIFEST_NAME]
    if leftovers:
        out.kept.append(f"{run.run_id}: kept {len(leftovers)} file(s) not created by the tool")
        if run.has_manifest:  # keep the run recognisable for a later cleanup
            with contextlib.suppress(OSError):
                (root / MANIFEST_NAME).write_text(
                    json.dumps({"version": 1, "files": []}), encoding="utf-8"
                )
        return
    with contextlib.suppress(FileNotFoundError):
        (root / MANIFEST_NAME).unlink()
    try:
        root.rmdir()
    except OSError as exc:
        out.failures.append(f"{run.run_id}: cannot remove the run directory: {exc}")
        return
    out.removed_runs += 1


def _cache_entries(base: Path, older_than_s: float | None, now: float) -> list[Path]:
    cache = base / "cache"
    if not cache.is_dir():
        return []
    found = []
    for entry in cache.iterdir():
        if not (entry.is_file() and CACHE_NAME_RE.match(entry.name)):
            continue
        try:
            if older_than_s is None or now - entry.stat().st_mtime >= older_than_s:
                found.append(entry)
        except OSError:
            continue
    return found


def _owner_of_dwl(path: Path) -> dict[str, str]:
    """Best-effort user and computer name from a ``.dwl`` file (plain text lines)."""
    try:
        raw = path.read_bytes()[:512]
    except OSError:
        return {}
    text = (
        raw.decode("utf-16-le", "ignore") if raw[1:2] == b"\x00" else raw.decode("utf-8", "ignore")
    )
    lines = [line.strip() for line in text.replace("\x00", "\n").splitlines() if line.strip()]
    return {"user": lines[0], "host": lines[1]} if len(lines) >= 2 else {}


def report_orphans(folder: Path) -> list[dict[str, Any]]:
    """Describe ``.dwl`` / ``.dwl2`` files under ``folder``. Report only; nothing is deleted."""
    if not folder.is_dir():
        raise CadError("FILE_NOT_FOUND", f"not a directory: {folder}", exit_code=ExitCode.BAD_ARGS)
    running = _doctor.running_cad_processes()
    local = platform.node().lower()
    items: list[dict[str, Any]] = []
    scanned = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if Path(name).suffix.lower() not in (".dwl", ".dwl2"):
                continue
            scanned += 1
            if scanned > MAX_ORPHAN_SCAN:
                return items
            path = Path(root) / name
            owner = _owner_of_dwl(path)
            host = owner.get("host", "").lower()
            if running:
                verdict, note = "unknown", "a CAD process is running; the file may be in use"
            elif host and host != local:
                verdict, note = "unknown", "created on another computer"
            else:
                verdict, note = "probably_orphaned", "no CAD process is running on this computer"
            drawing = path.with_suffix(".dwg")
            items.append(
                {
                    "path": str(path),
                    "verdict": verdict,
                    "note": note,
                    "owner": owner or None,
                    "drawing_exists": drawing.exists(),
                }
            )
    return items


def _positive_days(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of days")
    return value


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--list", action="store_true", help="list runs with sizes (default action)")
    parser.add_argument(
        "--older-than",
        type=_positive_days,
        metavar="DAYS",
        help="select runs/cache older than DAYS (a positive number)",
    )
    parser.add_argument(
        "--run", nargs="+", action="extend", default=[], metavar="RUN_ID", help="select runs by id"
    )
    parser.add_argument("--cache", action="store_true", help="also select cached DXF conversions")
    parser.add_argument(
        "--orphans", metavar="DIR", help="report .dwl/.dwl2 lock files under DIR (never deletes)"
    )
    parser.add_argument("--yes", action="store_true", help="confirm deletion")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted")
    parser.add_argument("--run-dir", help="base directory of run directories")


def _resolve_ids(wanted: list[str], runs: list[RunInfo]) -> list[RunInfo]:
    by_id = {r.run_id: r for r in runs}
    chosen: list[RunInfo] = []
    for item in wanted:
        exact = by_id.get(item)
        matches = [exact] if exact else [r for r in runs if r.run_id.startswith(item)]
        if len(matches) != 1:
            raise CadError(
                "BAD_ARGS",
                f"run id {item!r} matches {len(matches)} runs",
                exit_code=ExitCode.BAD_ARGS,
                hint="use `cleanup --list` and pass the full id",
            )
        chosen.append(matches[0])
    return chosen


def _run(args: argparse.Namespace) -> Result:
    base = runs_base(Path(args.run_dir) if args.run_dir else None)
    now = time.time()
    if args.older_than is not None and not (math.isfinite(args.older_than) and args.older_than > 0):
        raise CadError(
            "BAD_ARGS",
            "--older-than must be a positive number of days",
            exit_code=ExitCode.BAD_ARGS,
        )
    older_s = args.older_than * DAY_S if args.older_than is not None else None
    result = Result(command="cleanup", backend="none")
    runs = scan_runs(base, now)

    selected: dict[str, RunInfo] = {}
    for run in _resolve_ids(list(args.run), runs):
        selected[run.run_id] = run
    if older_s is not None:
        for run in runs:
            if now - run.started >= older_s:
                selected[run.run_id] = run
    skipped_active = [r.run_id for r in selected.values() if r.active]
    for run_id in skipped_active:
        del selected[run_id]
        result.warn(f"skipped {run_id}: still running")
    cache_files = _cache_entries(base, older_s, now) if args.cache else []
    wants_delete = bool(selected or cache_files or args.run or args.cache or older_s is not None)

    summary: dict[str, Any] = {
        "base": str(base),
        "runs": len(runs),
        "total_mb": round(sum(r.size for r in runs) / 1024**2, 2),
    }
    if args.list or not (wants_delete or args.orphans):
        summary["listed"] = [r.brief(now) for r in runs[:MAX_LISTED]]
    if args.orphans:
        orphans = report_orphans(Path(args.orphans))
        summary["orphans"] = {
            "count": len(orphans),
            "probably_orphaned": sum(o["verdict"] == "probably_orphaned" for o in orphans),
            "items": orphans[:MAX_LISTED],
        }
        result.warn("lock files are reported only; the tool never deletes .dwl/.dwl2 files")
    result.summary = summary

    if not wants_delete:
        return result
    plan = {
        "runs": len(selected),
        "run_ids": sorted(selected)[:MAX_LISTED],
        "cache_files": len(cache_files),
        "mb": round(
            (sum(r.size for r in selected.values()) + sum(_safe_size(f) for f in cache_files))
            / 1024**2,
            2,
        ),
    }
    summary["plan"] = plan
    if args.dry_run:
        summary["dry_run"] = True
        return result
    if not args.yes:
        result.add_error(
            CadError(
                "CONFIRM_REQUIRED",
                "deletion needs confirmation",
                exit_code=ExitCode.PRECONDITION_FAILED,
                hint="re-run with --yes (or --dry-run to preview)",
            )
        )
        return result

    out = Outcome()
    for run in selected.values():
        delete_run(run, out)
    for entry in cache_files:
        try:
            size = entry.stat().st_size
            entry.unlink()
        except OSError as exc:
            out.failures.append(f"cache {entry.name}: {exc}")
            continue
        out.deleted_files += 1
        out.deleted_bytes += size
    with contextlib.suppress(OSError):
        (base / "cache").rmdir()
    summary["deleted"] = {
        "runs": out.removed_runs,
        "files": out.deleted_files,
        "mb": round(out.deleted_bytes / 1024**2, 2),
    }
    for note in out.kept:
        result.warn(note)
    if out.failures:
        for failure in out.failures[:10]:
            result.warn(failure)
        result.exit_code = ExitCode.PARTIAL
    return result


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


COMMANDS = {
    "cleanup": Command(
        help="list and delete run directories and cache entries created by this tool",
        add_arguments=_add_arguments,
        run=_run,
        epilog=(
            "Examples:\n  cad.py cleanup --list\n  cad.py cleanup --older-than 7 --dry-run\n"
            "  cad.py cleanup --older-than 7 --cache --yes\n  cad.py cleanup --orphans path/to/drawings\n"
            "Only files recorded in a run manifest are deleted. Exit codes: 0 ok, 6 --yes missing, "
            "7 some deletions failed."
        ),
    )
}
