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


class CadError(Exception):
    """Raise from any command to end it with a structured error.

    ``code`` is a stable machine string (NO_BACKEND, DOC_OPEN_BY_USER, COM_TRANSIENT,
    PRECONDITION_FAILED, DISK_LOW, TIMEOUT, ...); ``hint`` says what to do next.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: ExitCode = ExitCode.ERROR,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
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
            data["warnings"] = self.warnings[:MAX_WARNINGS]
            if len(self.warnings) > MAX_WARNINGS:
                data["warnings"].append(f"... {len(self.warnings) - MAX_WARNINGS} more (see log)")
        if self.errors:
            data["errors"] = self.errors
        if self.next:
            data["next"] = self.next[:MAX_LIST_ITEMS]
        return data

    def to_json(self) -> str:
        """Compact JSON under MAX_JSON_BYTES; shrinks warnings, then summary lists, if needed."""
        data = self.to_dict()
        text = json.dumps(data, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            data["warnings"] = data.get("warnings", [])[:3] + ["output truncated, see log"]
            data["summary"] = _shrink(data.get("summary", {}))
            text = json.dumps(data, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            # last resort: keep the fields an agent cannot do without
            keep = {
                k: data[k]
                for k in ("status", "command", "exit_code", "errors", "run_dir", "log")
                if k in data
            }
            keep["warnings"] = ["output truncated, see log"]
            text = json.dumps(keep, ensure_ascii=False)
        return text


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
