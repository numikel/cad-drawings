"""Small shared helpers for the DXF commands: rounding, hashing, JSON, run plumbing."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ezdxf.math import Vec3

from .result import CadError, Result

if TYPE_CHECKING:
    from .runs import RunContext


_EPS = 1e-6


_ANON_NAME = re.compile(r"^\*[A-Za-z]\d*$")


_LAYOUT_BLOCKS = ("*model_space", "*paper_space")


class Deadline:
    """Soft time limit checked from long loops; ``None`` disables it."""

    def __init__(self, seconds: float | None) -> None:
        self._end = None if not seconds else time.monotonic() + seconds

    def check(self) -> None:
        if self._end is not None and time.monotonic() > self._end:
            raise CadError(
                "TIMEOUT",
                "time limit reached while reading the drawing",
                hint="raise --timeout or narrow the request",
            )


def r4(value: float) -> float:
    """Round for signatures and output; folds -0.0 into 0.0."""
    out = round(float(value), 4)
    return 0.0 if out == 0 else out


def _pt(point: Any, dims: int = 2) -> list[float]:
    v = Vec3(point)
    return [r4(v.x), r4(v.y)] if dims == 2 else [r4(v.x), r4(v.y), r4(v.z)]


def _hash(obj: Any, size: int = 16) -> str:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:size]


def _is_anonymous(name: str) -> bool:
    return bool(_ANON_NAME.match(name))


def _norm_block_name(name: str) -> str:
    """Anonymous block names (``*U7``, ``*D12``) are renumbered on every save."""
    return "*" + name[1].upper() if _is_anonymous(name) else name


def _brief(value: Any, limit: int = 120) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= limit else text[: limit - 3] + "..."


def atomic_write(dest: Path, write: Callable[[Path], None]) -> None:
    """Write ``dest`` through a temporary sibling and ``os.replace``: never a half-written file."""
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    atomic_write(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))


def refuse_input_as_output(dest: Path, inputs: list[Path]) -> None:
    """Reject an output path that is (or resolves to) one of the input files or a directory."""
    resolved = dest.expanduser().resolve()
    if resolved.is_dir():
        raise CadError("BAD_ARGS", f"output path is a directory: {dest}")
    for src in inputs:
        try:
            same = resolved == Path(src).expanduser().resolve() or (
                resolved.exists() and os.path.samefile(resolved, src)
            )
        except OSError:
            same = False
        if same:
            raise CadError(
                "BAD_ARGS",
                f"output path {dest} is an input file; inputs are never overwritten",
                hint="choose another --out",
            )


def _drive_is_fixed(drive: str) -> bool:
    """True for a local fixed disk (Windows). Anything else - network, removable, unknown - is not."""
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 3)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ImportError):
        return False


def path_is_local(raw: str | os.PathLike[str]) -> bool:
    """Whether touching ``raw`` cannot reach the network: no UNC path, no non-fixed drive.

    Paths stored inside drawings (xrefs, fonts) are untrusted: probing a UNC path makes the
    machine contact that host. Relative paths are local by construction.
    """
    text = os.fspath(raw).replace("\\", "/")
    if text.startswith("//"):
        return False
    if sys.platform == "win32":
        drive = re.match(r"^([A-Za-z]):", text)
        if drive:
            return _drive_is_fixed(drive.group(1).upper() + ":")
        if text.startswith("/"):  # root of the current drive
            return _drive_is_fixed(os.path.splitdrive(os.getcwd())[0].upper())
    return True


def safe_is_file(path: str | os.PathLike[str]) -> bool | None:
    """``is_file`` that never touches a non-local path; ``None`` means "not checked"."""
    if not path_is_local(path):
        return None
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _jsonable(value: Any) -> Any:
    if isinstance(value, float):
        return r4(value)
    if isinstance(value, str | int | bool) or value is None:
        return value
    if hasattr(value, "x") and hasattr(value, "y"):
        return _pt(value, 3 if hasattr(value, "z") else 2)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    return str(value)


def parse_floats(text: str, count: int, what: str) -> list[float]:
    try:
        values = [float(p) for p in text.split(",")]
    except ValueError:
        values = []
    if len(values) != count:
        raise CadError(
            "BAD_ARGS",
            f"{what} must be {count} comma-separated numbers, got {text!r}",
        )
    return values


DEFAULT_TIMEOUT_S = 100.0
BACKENDS = ("auto", "com", "oda", "libredwg")


def _add_common(parser: argparse.ArgumentParser, *, conversion: bool = True) -> None:
    parser.add_argument("--run-dir", type=Path, default=None, help="base directory for run folders")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"soft time limit in seconds (default {DEFAULT_TIMEOUT_S:g})",
    )
    if conversion:
        parser.add_argument(
            "--allow-com",
            action="store_true",
            help="let a DWG be converted with the user's CAD application (only with their consent)",
        )
        parser.add_argument(
            "--backend",
            choices=BACKENDS,
            default="auto",
            help="DWG converter: auto = oda, libredwg (COM only with --allow-com or --backend com)",
        )


def conversion_options(args: argparse.Namespace) -> dict[str, Any]:
    """Keyword arguments for ``open_drawing`` taken from the shared flags."""
    backend = getattr(args, "backend", "auto")
    return {
        "allow_com": bool(getattr(args, "allow_com", False)),
        "prefer": None if backend in (None, "auto") else backend,
    }


def _new_run(command: str, args: argparse.Namespace) -> RunContext:
    from .runs import RunContext

    return RunContext.create(command, getattr(args, "run_dir", None))


def _finish(result: Result, ctx: RunContext) -> Result:
    ctx.bind(result)
    ctx.finish()
    return result
