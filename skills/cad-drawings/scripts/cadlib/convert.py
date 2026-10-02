"""DWG <-> DXF conversion with a fixed backend policy: COM, then ODA File Converter, then LibreDWG.

Success is never read from an exit code or from a library's silence: the output must exist, be
newer than the start of the call, and (for DXF) open with ``ezdxf.recover``. Every attempt writes
to a staging file in the run directory and is moved to the destination only after it verified.

``--backend`` other than ``auto`` is exclusive (no silent fallback: the caller asked for it).
COM starts a CAD application, so it is used only with ``allow_com`` (``--allow-com``) or when it
is requested explicitly; otherwise ODA / LibreDWG are tried and, when none exists, the error
tells the agent to ask the user first. Outputs are written to a temp file beside the target and
moved with ``os.replace``; an output that is the same file as an input is refused.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import doctor as _doctor
from .command import Command
from .result import CadError, ExitCode, Result
from .runs import (
    MTIME_TOLERANCE_S,
    RunContext,
    cache_get,
    cache_put,
    file_sha1,
    note_child,
    preflight_disk,
    stage_copy_report,
)

FORMATS = ("dxf", "dwg")
BACKENDS = ("auto", "com", "oda", "libredwg")
DEFAULT_TIMEOUT_S = 100.0  # below typical host command limits; long jobs: background + status.json
ODA_VERSION = "ACAD2018"
LIBREDWG_DWG_VERSION = "r2004"
APPROX_WARNING = (
    "LibreDWG conversion is approximate: some objects (mostly R2010+ entities) may be dropped; "
    "ODA File Converter is more faithful"
)
NON_ASCII_WARNING = (
    "the path contains non-ASCII characters and this external tool may mishandle it; if "
    "conversion fails, use --run-dir with an ASCII-only path"
)
TAIL_CHARS = 600


class Converter(Protocol):
    name: str  # "com" | "oda" | "libredwg"
    approximate: bool  # True for libredwg

    @property
    def formats(self) -> tuple[str, ...]:  # output formats this backend can write
        ...

    def available(self) -> tuple[bool, str]: ...

    def convert(self, src: Path, dst: Path, fmt: str, ctx: RunContext) -> list[str]: ...


def _run_tool(args: list[str], timeout: float, ctx: RunContext, label: str) -> None:
    """Run an external converter; the exit code is only logged (it is unreliable)."""
    ctx.log(f"{label}: {' '.join(args)}")
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CadError(
            "TIMEOUT",
            f"{label} did not finish within {timeout:.0f} s",
            exit_code=ExitCode.TIMEOUT,
            hint="retry with a larger --timeout or another --backend",
        ) from None
    except OSError as exc:
        raise CadError("CONVERT_FAILED", f"{label} could not start: {exc}") from exc
    tail = (proc.stdout + proc.stderr).strip()[-TAIL_CHARS:]
    ctx.log(f"{label}: exit code {proc.returncode} (not trusted){': ' + tail if tail else ''}")


def _non_ascii(*paths: Path) -> bool:
    return any(not str(p).isascii() for p in paths)


@dataclass
class ComConverter:
    """Native export through a CAD application (COM, Windows). Delegates to ``cadlib.acad``.

    The source is always staged into the run directory first; the original is never opened.
    ``session`` (an existing ``AcadSession``) is used as is and left running; otherwise a private
    instance is started and quit around each conversion. ``session_factory`` is a test hook.
    """

    session: Any = None
    session_factory: Callable[[], Any] | None = None
    timeout: float = DEFAULT_TIMEOUT_S
    name: str = "com"
    approximate: bool = False
    formats: tuple[str, ...] = ("dxf", "dwg")

    def available(self) -> tuple[bool, str]:
        if self.session is not None or self.session_factory is not None:
            return True, "injected"
        if os.name != "nt":
            return False, "COM needs Windows"
        try:
            from . import acad
        except ImportError:
            return False, "acad module missing"
        if not any(h.get("exe_exists") for h in _doctor.detect_cad_hosts()):
            return False, "no COM-capable CAD application registered"
        if getattr(acad, "AcadSession", None) is None:
            return False, "acad module has no session class"
        return True, "ok"

    @contextlib.contextmanager
    def _open_session(self) -> Iterator[Any]:
        if self.session is not None:
            yield self.session
            return
        if self.session_factory is not None:
            manager = self.session_factory()
        else:
            from .acad import AcadSession

            manager = AcadSession.start(timeout=self.timeout)
        with manager as started:
            pid = getattr(started, "pid", None)
            if isinstance(pid, int):
                note_child(pid, str(getattr(started, "image", "cad")))
            yield started

    def convert(self, src: Path, dst: Path, fmt: str, ctx: RunContext) -> list[str]:
        ok, why = self.available()
        if not ok:
            raise CadError("NO_BACKEND", f"COM backend unavailable: {why}")
        if fmt not in self.formats:
            raise CadError(
                "BAD_ARGS", f"the COM backend cannot write {fmt}", exit_code=ExitCode.BAD_ARGS
            )
        report = stage_copy_report(src, ctx)  # never open the original
        warnings = list(report.skipped)
        ctx.log(f"com: converting {src.name} to {fmt.upper()} through the CAD application")
        with self._open_session() as session:
            if fmt == "dxf":
                warnings += list(session.export_dxf(report.path, dst) or [])
            else:
                doc = session.open(report.path, readonly=True)
                warnings += list(session.save_dwg(doc, dst) or [])
        return warnings


@dataclass
class OdaConverter:
    """ODA File Converter. Works on folders, so the source is staged under an ASCII name."""

    exe: str | None = None
    timeout: float = DEFAULT_TIMEOUT_S
    name: str = "oda"
    approximate: bool = False
    formats: tuple[str, ...] = ("dxf", "dwg")

    def _exe(self) -> str | None:
        return self.exe or _doctor.find_oda()

    def available(self) -> tuple[bool, str]:
        exe = self._exe()
        if not exe:
            return False, "ODAFileConverter not found"
        if sys.platform.startswith("linux") and not _doctor.has_display():
            if not (shutil.which("xvfb-run") or shutil.which("Xvfb")):
                return False, "ODAFileConverter needs a display on Linux: install Xvfb"
        return True, exe

    def convert(self, src: Path, dst: Path, fmt: str, ctx: RunContext) -> list[str]:
        exe = self._exe()
        if not exe:
            raise CadError("NO_BACKEND", "ODAFileConverter not found")
        warnings: list[str] = []
        stem = "input"
        in_dir = ctx.subdir("oda/in")
        out_dir = ctx.subdir("oda/out")
        staged_in = ctx.path(f"oda/in/{stem}{src.suffix.lower()}")
        staged_out = out_dir / f"{stem}.{fmt}"
        for leftover in (staged_in, staged_out):
            if leftover.exists():
                leftover.unlink()
        shutil.copyfile(src, staged_in)
        prefix: list[str] = []
        if sys.platform.startswith("linux") and not _doctor.has_display():
            xvfb = shutil.which("xvfb-run")
            if xvfb:
                prefix = [xvfb, "-a"]
        elif sys.platform == "darwin":
            warnings.append("ODA File Converter opens a window on macOS; do not close it")
        if _non_ascii(in_dir, out_dir):
            warnings.append(NON_ASCII_WARNING)
        args = [
            *prefix,
            exe,
            str(in_dir),
            str(out_dir),
            ODA_VERSION,
            fmt.upper(),
            "0",  # no recursion
            "0",  # no audit: keep the content as it is
            staged_in.name,
        ]
        _run_tool(args, self.timeout, ctx, "ODA File Converter")
        if not staged_out.is_file():
            raise CadError(
                "CONVERT_FAILED",
                "ODA File Converter produced no output file",
                hint="see the run log; the file may be damaged or a newer DWG version than the converter knows",
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_out), dst)
        return warnings


@dataclass
class LibreDwgConverter:
    """LibreDWG command line tools; always approximate."""

    tools: dict[str, str] | None = None  # tool name -> path; None = look on PATH
    timeout: float = DEFAULT_TIMEOUT_S
    name: str = "libredwg"
    approximate: bool = True

    def _tools(self) -> dict[str, str]:
        return self.tools if self.tools is not None else _doctor.find_libredwg()

    @property
    def formats(self) -> tuple[str, ...]:
        tools = self._tools()
        out = []
        if "dwg2dxf" in tools:
            out.append("dxf")
        if "dxf2dwg" in tools:
            out.append("dwg")
        return tuple(out)

    def available(self) -> tuple[bool, str]:
        found = self._tools()
        if not found:
            return False, "dwg2dxf / dxf2dwg not found on PATH"
        return True, ", ".join(sorted(found))

    def convert(self, src: Path, dst: Path, fmt: str, ctx: RunContext) -> list[str]:
        tool = "dwg2dxf" if fmt == "dxf" else "dxf2dwg"
        exe = self._tools().get(tool)
        if not exe:
            raise CadError("NO_BACKEND", f"{tool} not found on PATH")
        warnings = [APPROX_WARNING]
        if fmt == "dwg":
            warnings.append(f"dxf2dwg writes DWG {LIBREDWG_DWG_VERSION} only")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if _non_ascii(src, dst):
            warnings.append(NON_ASCII_WARNING)
        args = [exe, "-y"]
        if fmt == "dwg":
            args += ["--as", LIBREDWG_DWG_VERSION]
        args += ["-o", str(dst), str(src)]
        _run_tool(args, self.timeout, ctx, tool)
        return warnings


def detect_converters(timeout: float = DEFAULT_TIMEOUT_S) -> list[Converter]:
    """All known backends in priority order: com, oda, libredwg (availability is checked later)."""
    return [
        ComConverter(timeout=timeout),
        OdaConverter(timeout=timeout),
        LibreDwgConverter(timeout=timeout),
    ]


# -- verification ---------------------------------------------------------------------------


def verify_output(path: Path, fmt: str, started: float) -> list[str]:
    """Raise CadError(OUTPUT_INVALID) unless ``path`` is a fresh, readable file of ``fmt``."""
    try:
        stat = path.stat()
    except OSError:
        raise CadError("OUTPUT_INVALID", "converter reported success but wrote no file") from None
    if stat.st_size == 0:
        raise CadError("OUTPUT_INVALID", "converter wrote an empty file")
    if stat.st_mtime + MTIME_TOLERANCE_S < started:
        raise CadError("OUTPUT_INVALID", "output file is older than the conversion call (stale)")
    if fmt == "dwg":
        with path.open("rb") as fh:
            if fh.read(2) != b"AC":
                raise CadError("OUTPUT_INVALID", "output does not look like a DWG file")
        return []
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError:
        _check_dxf_structure(path)
        return ["ezdxf is not installed: the converted DXF was only checked structurally"]
    try:
        recover.readfile(str(path))
    except (ezdxf.DXFError, OSError) as exc:
        raise CadError("OUTPUT_INVALID", f"converted DXF does not open: {exc}") from exc
    return []


def _check_dxf_structure(path: Path) -> None:
    """Without ezdxf: a DXF starts with a SECTION (or the binary sentinel) and ends with EOF."""
    with path.open("rb") as fh:
        head = fh.read(64)
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 512))
        tail = fh.read()
    if head.startswith(b"AutoCAD Binary DXF"):
        return
    if not re.match(rb"\s*0\r?\n\s*SECTION", head) or b"EOF" not in tail:
        raise CadError("OUTPUT_INVALID", "output does not look like a DXF file")


def _same_path(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists() and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def reject_overlap(dst: Path, *inputs: Path) -> None:
    """Refuse an output path that is a directory or the same file as any input."""
    dst = Path(dst)
    if dst.is_dir():
        raise CadError(
            "BAD_ARGS",
            f"output path is a directory: {dst}",
            exit_code=ExitCode.BAD_ARGS,
            hint="give a file name",
        )
    for item in inputs:
        if _same_path(dst, Path(item)):
            raise CadError(
                "BAD_ARGS",
                f"output path is the same file as an input: {dst}",
                exit_code=ExitCode.BAD_ARGS,
                hint="choose a different --out; inputs are never overwritten",
            )


def _place(stage: Path, dst: Path) -> None:
    """Move a verified file to ``dst`` atomically (a temp beside the target across volumes)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(stage, dst)
        return
    except OSError:
        pass  # another volume (or a transient lock): copy beside the target, then replace
    _copy_atomic(stage, dst)
    with contextlib.suppress(OSError):
        stage.unlink()


def _copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(3)}.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# -- policy ---------------------------------------------------------------------------------


def _select_pool(
    converters: list[Converter] | None, prefer: str | None, timeout: float
) -> list[Converter]:
    pool = list(converters) if converters is not None else detect_converters(timeout)
    if prefer and prefer != "auto":
        pool = [c for c in pool if c.name == prefer]
        if not pool:
            raise CadError(
                "BAD_ARGS",
                f"unknown backend {prefer!r}",
                exit_code=ExitCode.BAD_ARGS,
                hint=f"choose one of: {', '.join(BACKENDS)}",
            )
    return pool


def _no_backend(fmt: str, skipped: list[str], com_blocked: bool = False) -> CadError:
    detail = "; ".join(skipped) if skipped else "none configured"
    hint = _doctor.converter_install_hint()
    if com_blocked:
        hint = (
            "a CAD application is installed and can convert, but it starts an instance: ask the "
            "user, then rerun with --allow-com. Alternatively: " + hint
        )
    return CadError(
        "NO_BACKEND",
        f"no converter can write {fmt.upper()} here ({detail})",
        exit_code=ExitCode.MISSING_DEPENDENCY,
        hint=hint,
    )


def _candidates(
    pool: list[Converter], fmt: str, com_ok: bool
) -> tuple[list[Converter], list[str], bool]:
    """Usable backends in priority order, the reasons others were skipped, and whether a
    working COM host was left out only because nobody allowed it."""
    candidates: list[Converter] = []
    skipped: list[str] = []
    com_blocked = False
    for conv in pool:
        if fmt not in conv.formats:
            skipped.append(f"{conv.name}: cannot write {fmt}")
            continue
        ok, why = conv.available()
        if not ok:
            skipped.append(f"{conv.name}: {why}")
        elif conv.name == "com" and not com_ok:
            com_blocked = True
            skipped.append("com: not used without --allow-com (it starts a CAD instance)")
        else:
            candidates.append(conv)
    return candidates, skipped, com_blocked


def convert(
    src: Path,
    dst: Path,
    fmt: str,
    *,
    prefer: str | None = None,
    ctx: RunContext,
    converters: list[Converter] | None = None,
    allow_com: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[Converter, list[str]]:
    """Convert ``src`` to ``dst`` (``fmt`` = dxf|dwg) with the first backend that works.

    Falls back com -> oda -> libredwg when a backend fails or its output does not verify. COM is
    skipped unless ``allow_com`` or ``prefer == "com"``. A non-auto ``prefer`` uses only that
    backend. ``converters`` injects backends (tests). ``dst`` must not be an input.
    """
    fmt = fmt.lower()
    src, dst = Path(src), Path(dst)
    if fmt not in FORMATS:
        raise CadError(
            "BAD_ARGS", f"unsupported target format {fmt!r}", exit_code=ExitCode.BAD_ARGS
        )
    if not src.is_file():
        raise CadError(
            "FILE_NOT_FOUND", f"source file not found: {src}", exit_code=ExitCode.BAD_ARGS
        )
    reject_overlap(dst, src)
    pool = _select_pool(converters, prefer, timeout)
    candidates, skipped, com_blocked = _candidates(pool, fmt, allow_com or prefer == "com")
    if not candidates:
        raise _no_backend(fmt, skipped, com_blocked)

    failures: list[tuple[Converter, CadError]] = []
    for conv in candidates:
        started = time.time()
        stage = ctx.path(f"convert/{conv.name}/{dst.name}")
        if stage.exists():
            stage.unlink()
        try:
            warnings = list(conv.convert(src, stage, fmt, ctx))
            warnings += verify_output(stage, fmt, started)
        except CadError as err:
            ctx.log(f"{conv.name} failed: {err.message}")
            failures.append((conv, err))
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            ctx.log(f"{conv.name} failed: {exc}")
            failures.append((conv, CadError("CONVERT_FAILED", f"{type(exc).__name__}: {exc}")))
            continue
        _place(stage, dst)
        fallbacks = [f"{c.name} failed ({e.message})" for c, e in failures]
        if fallbacks:
            warnings.insert(0, "; ".join(fallbacks) + f"; used {conv.name}")
        if conv.approximate and APPROX_WARNING not in warnings:
            warnings.append(APPROX_WARNING)
        return conv, warnings

    if len(failures) == 1:
        raise failures[0][1]
    codes = {e.code for _, e in failures}
    same = len(codes) == 1
    raise CadError(
        codes.pop() if same else "CONVERT_FAILED",
        "; ".join(f"{c.name}: {e.message}" for c, e in failures),
        exit_code=failures[0][1].exit_code if same else ExitCode.ERROR,
        hint="see the run log; try another --backend",
    )


@dataclass
class DxfResult:
    path: Path
    backend: str | None  # None when the source already was a DXF
    approximate: bool = False
    cached: bool = False
    warnings: list[str] = field(default_factory=list)


def ensure_dxf(
    src: Path,
    ctx: RunContext,
    *,
    allow_com: bool = False,
    prefer: str | None = None,
    session: Any = None,
    base: Path | None = None,
    converters: list[Converter] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> DxfResult:
    """A DXF for ``src``: the source itself when it is DXF, else a cached or fresh conversion.

    The one conversion policy for every command: ODA / LibreDWG, and COM only with
    ``allow_com`` (or ``prefer == "com"``, or an existing ``session``, which is used as is and
    left running). COM never opens the original (it is staged first); all backend warnings are
    returned. The cache key is ``sha1(source)`` plus the converter id, so an edited source never
    hits a stale entry; a cached DXF is returned without starting anything. The returned cache
    path must be treated as read-only.
    """
    src = Path(src)
    if src.suffix.lower() == ".dxf":
        return DxfResult(src, None)
    digest = file_sha1(src)
    pool = _select_pool(converters, prefer, timeout)
    if session is not None:
        pool = [
            dataclasses.replace(c, session=session) if isinstance(c, ComConverter) else c
            for c in pool
        ]
    for conv in pool:
        hit = cache_get(digest, conv.name, base=base)
        if hit is not None:
            ctx.log(f"cache hit for {src.name} ({conv.name})")
            warnings = [APPROX_WARNING] if conv.approximate else []
            return DxfResult(hit, conv.name, conv.approximate, True, warnings)
    target = ctx.path(f"converted/{src.stem}.dxf")
    conv, warnings = convert(
        src,
        target,
        "dxf",
        prefer=prefer,
        ctx=ctx,
        converters=pool,
        allow_com=allow_com or session is not None,
        timeout=timeout,
    )
    cache_put(digest, conv.name, target, base=base)
    return DxfResult(target, conv.name, conv.approximate, False, warnings)


# -- command --------------------------------------------------------------------------------


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("src", help="source .dwg or .dxf file")
    parser.add_argument("--to", required=True, choices=FORMATS, help="target format")
    parser.add_argument("--out", help="destination file (default: inside the run directory)")
    parser.add_argument(
        "--backend", choices=BACKENDS, default="auto", help="auto = com, oda, libredwg"
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing --out file")
    parser.add_argument(
        "--allow-com",
        action="store_true",
        help="allow starting a CAD application (COM); ask the user first. --backend com implies it",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=(
            "seconds per backend (default 100, below typical host command limits); for longer "
            "jobs run the command in the background and poll status.json in the run directory"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    parser.add_argument("--run-dir", help="base directory for run directories")


def _plan(
    pool: list[Converter], fmt: str, com_ok: bool
) -> tuple[Converter | None, list[str], bool]:
    candidates, skipped, com_blocked = _candidates(pool, fmt, com_ok)
    return (candidates[0] if candidates else None), skipped, com_blocked


def _run(args: argparse.Namespace) -> Result:
    src = Path(args.src)
    fmt: str = args.to
    if not src.is_file():
        raise CadError(
            "FILE_NOT_FOUND",
            f"source file not found: {src}",
            exit_code=ExitCode.BAD_ARGS,
            hint="check the path",
        )
    suffix = src.suffix.lower()
    if suffix not in (".dwg", ".dxf"):
        raise CadError(
            "BAD_ARGS", f"unsupported source type {suffix!r}", exit_code=ExitCode.BAD_ARGS
        )
    if suffix == ".dwg" and fmt == "dwg":
        raise CadError("BAD_ARGS", "the source already is a DWG", exit_code=ExitCode.BAD_ARGS)
    out = Path(args.out) if args.out else None
    if out is not None:
        reject_overlap(out, src)
    com_ok = bool(args.allow_com) or args.backend == "com"
    result = Result(command="convert")

    if suffix == ".dxf" and fmt == "dxf":  # nothing to convert
        result.backend = "none"
        result.summary = {"noop": True, "from": "dxf", "to": "dxf"}
        if out is None:
            result.outputs["output"] = {"path": str(src.absolute())}
            return result
        if out.exists() and not args.overwrite:
            raise _exists(out)
        if args.dry_run:
            result.summary["dry_run"] = True
            return result
        ctx = RunContext.create("convert", Path(args.run_dir) if args.run_dir else None)
        _copy_atomic(src, out)
        ctx.add_output(result, "output", out, source=src)
        ctx.finish()
        return result

    if out is not None and out.exists() and not args.overwrite:
        raise _exists(out)
    pool = _select_pool(None, args.backend, args.timeout)
    if args.dry_run:
        conv, skipped, com_blocked = _plan(pool, fmt, com_ok)
        if conv is None:
            raise _no_backend(fmt, skipped, com_blocked)
        result.backend = conv.name
        result.approximate = conv.approximate
        result.summary = {"dry_run": True, "would_use": conv.name, "skipped": skipped}
        return result

    ctx = RunContext.create("convert", Path(args.run_dir) if args.run_dir else None)
    ctx.bind(result)
    preflight_disk(ctx.dir, min_gb=max(1.0, 4 * src.stat().st_size / 1024**3))
    dst = out if out is not None else ctx.path(f"{src.stem}.{fmt}")
    ctx.log(f"convert {src} -> {fmt} (backend {args.backend})")
    try:
        if fmt == "dxf":
            found = ensure_dxf(
                src,
                ctx,
                allow_com=com_ok,
                prefer=args.backend,
                converters=pool,
                timeout=args.timeout,
            )
            reject_overlap(dst, src, found.path)
            _copy_atomic(found.path, dst)
            backend, approximate, cached, warnings = (
                found.backend,
                found.approximate,
                found.cached,
                found.warnings,
            )
        else:
            conv, warnings = convert(
                src,
                dst,
                fmt,
                prefer=args.backend,
                ctx=ctx,
                converters=pool,
                allow_com=com_ok,
                timeout=args.timeout,
            )
            backend, approximate, cached = conv.name, conv.approximate, False
    except CadError:
        ctx.finish("failed")
        raise
    ctx.add_output(result, "output", dst, source=src)
    result.backend = backend
    result.approximate = approximate
    for warning in warnings:
        result.warn(warning)
    result.summary = {"from": suffix[1:], "to": fmt, "cached": cached, "bytes": dst.stat().st_size}
    ctx.finish()
    return result


def _exists(path: Path) -> CadError:
    return CadError(
        "EXISTS",
        f"destination already exists: {path}",
        exit_code=ExitCode.PRECONDITION_FAILED,
        hint="pass --overwrite or choose another --out",
    )


COMMANDS = {
    "convert": Command(
        help="convert DWG <-> DXF (COM, then ODA File Converter, then LibreDWG)",
        add_arguments=_add_arguments,
        run=_run,
        epilog=(
            "Examples:\n  cad.py convert plan.dwg --to dxf\n"
            "  cad.py convert plan.dxf --to dwg --out out/plan.dwg --backend oda\n"
            "  cad.py convert plan.dwg --to dxf --allow-com   (starts a CAD instance; ask first)\n"
            "Exit codes: 0 ok, 3 no converter (see hint), 4 busy, 5 timeout, 6 --out exists."
        ),
    )
}
