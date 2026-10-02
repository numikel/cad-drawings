"""Shared output contract: exit codes, errors and the JSON summary printed on stdout.

Every command returns a ``Result``; ``cad.py`` prints it and exits with ``Result.exit_code``.
The JSON follows ``assets/output.schema.json`` and is capped at ``MAX_JSON_BYTES``.
"""

from __future__ import annotations

import enum
import json
import sys
from dataclasses import dataclass, field
from typing import Any

MAX_JSON_BYTES = 4096
MAX_WARNINGS = 20
MAX_LIST_ITEMS = 20


class ExitCode(enum.IntEnum):
    OK = 0
    ERROR = 1
    BAD_ARGS = 2
    MISSING_DEPENDENCY = 3
    BUSY = 4
    TIMEOUT = 5
    PRECONDITION_FAILED = 6
    PARTIAL = 7


_STATUS = {
    ExitCode.OK: "ok",
    ExitCode.PARTIAL: "partial",
    ExitCode.MISSING_DEPENDENCY: "refused",
    ExitCode.BUSY: "refused",
    ExitCode.PRECONDITION_FAILED: "refused",
}

# Stable machine codes -> the exit code every raise of that code must carry. A contract test
# (tests/test_error_codes.py) fails on a code that is missing here or raised with another exit
# code. Add new codes here, never invent them silently in a command.
ERROR_CODES: dict[str, ExitCode] = {
    # bad input
    "BAD_ARGS": ExitCode.BAD_ARGS,
    "BAD_FINGERPRINT": ExitCode.BAD_ARGS,
    "FILE_NOT_FOUND": ExitCode.BAD_ARGS,
    "LAYOUT_NOT_FOUND": ExitCode.BAD_ARGS,
    "UNSUPPORTED": ExitCode.BAD_ARGS,
    "SPEC_INVALID": ExitCode.BAD_ARGS,
    # missing dependency or backend
    "MISSING_DEPENDENCY": ExitCode.MISSING_DEPENDENCY,
    "NO_BACKEND": ExitCode.MISSING_DEPENDENCY,
    # resource busy
    "BUSY": ExitCode.BUSY,
    "COM_TRANSIENT": ExitCode.BUSY,
    "DISK_LOW": ExitCode.BUSY,
    "DOC_ALREADY_OPEN": ExitCode.BUSY,
    "DOC_OPEN_BY_USER": ExitCode.BUSY,
    "LOCKED": ExitCode.BUSY,
    # time
    "TIMEOUT": ExitCode.TIMEOUT,
    # precondition not met (target exists, empty layout, stale base, ...)
    "CONFIRM_REQUIRED": ExitCode.PRECONDITION_FAILED,
    "DEST_EXISTS": ExitCode.PRECONDITION_FAILED,
    "EMPTY_LAYOUT": ExitCode.PRECONDITION_FAILED,
    "EXISTS": ExitCode.PRECONDITION_FAILED,
    "BASE_CHANGED": ExitCode.PRECONDITION_FAILED,
    "EXPECT_FAILED": ExitCode.PRECONDITION_FAILED,
    "NOT_FOUND": ExitCode.PRECONDITION_FAILED,
    "NO_INSTANCE": ExitCode.PRECONDITION_FAILED,
    "NO_PAPER_SIZE": ExitCode.PRECONDITION_FAILED,
    "PRECONDITION_FAILED": ExitCode.PRECONDITION_FAILED,
    # generic failures
    "COM_ERROR": ExitCode.ERROR,
    "CONVERT_FAILED": ExitCode.ERROR,  # may carry the shared exit code of failed backends
    "INTERRUPTED": ExitCode.ERROR,
    "OUTPUT_INVALID": ExitCode.ERROR,
    "OUTPUT_MISSING": ExitCode.ERROR,
    "PID_UNAVAILABLE": ExitCode.ERROR,
    "PLOT_BAD_OUTPUT": ExitCode.ERROR,
    "PLOT_FAILED": ExitCode.ERROR,
    "RUN_DIR": ExitCode.ERROR,
    "RUNS_DIR_UNSAFE": ExitCode.ERROR,
    "STALE_OUTPUT": ExitCode.ERROR,
    "UNEXPECTED": ExitCode.ERROR,
    "UNINTENDED_CHANGE": ExitCode.ERROR,
    "UNREADABLE": ExitCode.ERROR,
}

# Codes whose exit code is chosen at run time on purpose (the test skips non-literal values).
DYNAMIC_EXIT_CODES = frozenset({"CONVERT_FAILED"})


class CadError(Exception):
    """Raise from any command to end it with a structured error.

    ``code`` is a stable machine string registered in ``ERROR_CODES``; when ``exit_code`` is
    omitted it comes from that registry. ``hint`` says what to do next.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: ExitCode | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        if exit_code is None:
            exit_code = ERROR_CODES.get(code, ExitCode.ERROR)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint


@dataclass
class Result:
    command: str
    exit_code: ExitCode = ExitCode.OK
    backend: str | None = None
    approximate: bool | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    run_dir: str | None = None
    log: str | None = None
    elapsed_s: float | None = None
    next: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_error(self, err: CadError) -> None:
        item = {"code": err.code, "message": err.message}
        if err.hint:
            item["hint"] = err.hint
        self.errors.append(item)
        if self.exit_code == ExitCode.OK:
            self.exit_code = err.exit_code

    @classmethod
    def from_error(cls, command: str, err: CadError) -> Result:
        res = cls(command=command)
        res.add_error(err)
        return res

    def to_dict(self) -> dict[str, Any]:
        status = _STATUS.get(self.exit_code, "error")
        data: dict[str, Any] = {
            "status": status,
            "command": self.command,
            "exit_code": int(self.exit_code),
        }
        for key in ("backend", "approximate", "run_dir", "log", "elapsed_s"):
            value = getattr(self, key)
            if value is not None:
                data[key] = round(value, 2) if key == "elapsed_s" else value
        if self.summary:
            data["summary"] = self.summary
        if self.outputs:
            data["outputs"] = self.outputs
        if self.warnings:
            ordered = _prioritise(self.warnings)
            data["warnings"] = ordered[:MAX_WARNINGS]
            if len(self.warnings) > MAX_WARNINGS:
                data["warnings"].append(f"... {len(self.warnings) - MAX_WARNINGS} more (see log)")
        if self.errors:
            data["errors"] = self.errors
        if self.next:
            data["next"] = self.next[:MAX_LIST_ITEMS]
        return data

    def to_json(self) -> str:
        """Compact JSON under MAX_JSON_BYTES.

        Shrinks in stages and never drops what changes how a result must be read: ``status``,
        ``exit_code``, ``errors``, ``backend``, ``approximate`` (with its warning first) and the
        artifact paths. Order of sacrifice: long summary values, extra warnings, artifact metadata,
        the summary itself.
        """
        data = self.to_dict()

        def size(d: dict[str, Any]) -> int:
            return len(json.dumps(d, ensure_ascii=False).encode("utf-8"))

        if size(data) > MAX_JSON_BYTES:
            data["warnings"] = _prioritise(data.get("warnings", []))[:3] + [
                "output truncated, see log"
            ]
            if "summary" in data:
                data["summary"] = _shrink(data["summary"])
        if size(data) > MAX_JSON_BYTES and "outputs" in data:
            outputs = data["outputs"]
            names = list(outputs)[:12]
            data["outputs"] = {n: {"path": outputs[n]["path"]} for n in names}
            if len(outputs) > len(names):
                data["warnings"].append(f"{len(outputs) - len(names)} more outputs, see log")
        if size(data) > MAX_JSON_BYTES and "summary" in data:
            data["summary"] = {"truncated": True}
        if size(data) > MAX_JSON_BYTES:
            keep_keys = (
                "status",
                "command",
                "exit_code",
                "backend",
                "approximate",
                "errors",
                "run_dir",
                "log",
            )
            keep = {k: data[k] for k in keep_keys if k in data}
            keep["warnings"] = _prioritise(data.get("warnings", []))[:1] + [
                "output truncated, see log"
            ]
            data = keep
        return json.dumps(data, ensure_ascii=False)


def _prioritise(warnings: list[str]) -> list[str]:
    """Warnings about approximate results first: they change how the result may be used."""
    first = [w for w in warnings if "approximate" in w.lower()]
    return first + [w for w in warnings if w not in first]


def _shrink(obj: Any) -> Any:
    if isinstance(obj, list):
        out = [_shrink(x) for x in obj[:MAX_LIST_ITEMS]]
        if len(obj) > MAX_LIST_ITEMS:
            out.append(f"... {len(obj) - MAX_LIST_ITEMS} more")
        return out
    if isinstance(obj, dict):
        return {k: _shrink(v) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > 200:
        return obj[:200] + "..."
    return obj


def force_utf8() -> None:
    """Make stdout/stderr UTF-8 regardless of console code page (no PYTHONIOENCODING needed)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def emit(result: Result) -> int:
    force_utf8()
    sys.stdout.write(result.to_json() + "\n")
    sys.stdout.flush()
    return int(result.exit_code)
