"""``doctor`` command: wraps the standalone ``scripts/doctor.py`` (stdlib only).

The standalone script is the single source of truth for environment detection; it is loaded by
file path so this works however ``sys.path`` is set up. The detection helpers are re-exported
for ``cadlib.convert`` (converter discovery and install hints use the same data as ``doctor``).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .command import Command
from .result import ExitCode, Result

_MODULE_NAME = "_cad_drawings_doctor_standalone"
_SCRIPT = Path(__file__).resolve().parents[1] / "doctor.py"


def _load_standalone() -> ModuleType:
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module  # dataclasses need the module registered before exec
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


_std = _load_standalone()

find_oda = _std.find_oda
find_libredwg = _std.find_libredwg
has_display = _std.has_display
detect_cad_hosts = _std.detect_cad_hosts
running_cad_processes = _std.running_cad_processes
install_options = _std.install_options
converter_install_hint = _std.converter_install_hint
build_report = _std.build_report


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    _std.add_arguments(parser)


def _run(args: argparse.Namespace) -> Result:
    report = build_report(probe_com=bool(args.probe_com))
    result = Result(
        command="doctor",
        exit_code=ExitCode(report["exit_code"]),
        backend=report.get("backend"),
        summary=report.get("summary", {}),
        warnings=list(report.get("warnings", [])),
    )
    for item in report.get("errors", []):
        result.errors.append(dict(item))
    return result


COMMANDS = {
    "doctor": Command(
        help="report what this machine can do (capability matrix, install commands); installs nothing",
        add_arguments=_add_arguments,
        run=_run,
        epilog=(
            "Examples:\n  cad.py doctor\n  cad.py doctor --probe-com   (starts a CAD instance; "
            "ask the user first)\nExit codes: 0 report printed, 3 Python too old."
        ),
    )
}
