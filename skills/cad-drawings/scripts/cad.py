"""Single entry point: python cad.py <command> [args]. Every command prints one short JSON.

Exit codes: 0 ok, 1 error, 2 bad arguments, 3 missing dependency or backend, 4 resource busy,
5 timeout, 6 precondition failed, 7 partial success. See assets/output.schema.json.
No command ever prompts; destructive steps need --yes or --overwrite.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cadlib import COMMAND_MODULES, MODULES
from cadlib.command import Command
from cadlib.result import CadError, ExitCode, Result, emit, force_utf8

# import name -> what to tell the user to install (the agent must ask before installing)
_PIP_NAMES = {
    "ezdxf": "ezdxf>=1.4.4",
    "PIL": "pillow",
    "matplotlib": "matplotlib",
    "pypdfium2": "pypdfium2",
    "win32com": "pywin32>=312",
    "pythoncom": "pywin32>=312",
}


def _unavailable(command: str, module: str, exc: ImportError) -> Command:
    """Stand-in for a command whose module could not be imported (missing dependency)."""
    missing = getattr(exc, "name", None) or str(exc)
    pip_name = _PIP_NAMES.get(missing.split(".")[0], missing.split(".")[0])

    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    def run(_args: argparse.Namespace) -> Result:
        raise CadError(
            "MISSING_DEPENDENCY",
            f"command {command!r} needs the Python package {missing!r}, which is not installed",
            hint=(
                f'ask the user, then install it (pip install "{pip_name}"); '
                "python scripts/doctor.py lists everything that is missing"
            ),
        )

    return Command(
        help=f"UNAVAILABLE (missing {missing}); run it for the install hint",
        add_arguments=add_arguments,
        run=run,
    )


def load_commands() -> dict[str, Command]:
    """Collect COMMANDS from every module; a module that cannot be imported because a
    dependency is missing contributes stand-ins that fail with exit 3 and an install hint,
    so doctor, cleanup and --help keep working."""
    commands: dict[str, Command] = {}
    for name in MODULES:
        try:
            module = importlib.import_module(f"cadlib.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"cadlib.{name}":
                continue  # module not delivered yet
            for command, owner in COMMAND_MODULES.items():
                if owner == name:
                    commands[command] = _unavailable(command, name, exc)
            continue
        except ImportError as exc:
            for command, owner in COMMAND_MODULES.items():
                if owner == name:
                    commands[command] = _unavailable(command, name, exc)
            continue
        for key, command in getattr(module, "COMMANDS", {}).items():
            if key in commands:
                raise RuntimeError(f"duplicate command {key!r} (module {name})")
            commands[key] = command
    return commands


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # exit 2 with the JSON contract, not argparse text
        force_utf8()
        res = Result.from_error(
            "cad",
            CadError("BAD_ARGS", message, exit_code=ExitCode.BAD_ARGS, hint="run with --help"),
        )
        sys.stdout.write(res.to_json() + "\n")
        raise SystemExit(int(ExitCode.BAD_ARGS))


def build_parser(commands: dict[str, Command]) -> argparse.ArgumentParser:
    parser = _Parser(
        prog="cad.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name, command in commands.items():
        p = sub.add_parser(
            name,
            help=command.help,
            description=command.help,
            epilog=command.epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        command.add_arguments(p)
    return parser


def _attach_run(res: Result, *, failed: bool) -> None:
    """Keep run_dir/log on error results and close the run as failed (status.json)."""
    try:
        from cadlib import runs

        current = getattr(runs, "current_context", None)
        ctx = current() if current is not None else None
    except Exception:  # noqa: BLE001 - bookkeeping must never mask the real error
        return
    if ctx is None:
        return
    if failed:
        with contextlib.suppress(Exception):
            ctx.finish("failed")
    res.run_dir = res.run_dir or str(ctx.dir)
    res.log = res.log or str(Path(ctx.dir) / "log.txt")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    commands = load_commands()
    args = build_parser(commands).parse_args(argv)
    started = time.monotonic()
    failed = True
    try:
        res = commands[args.command].run(args)
        failed = res.exit_code not in (ExitCode.OK, ExitCode.PARTIAL)
    except CadError as err:
        res = Result.from_error(args.command, err)
    except KeyboardInterrupt:
        res = Result.from_error(args.command, CadError("INTERRUPTED", "interrupted"))
    except Exception as exc:  # noqa: BLE001 - last resort: never a bare traceback on stdout
        print(traceback.format_exc(), file=sys.stderr)
        res = Result.from_error(
            args.command, CadError("UNEXPECTED", f"{type(exc).__name__}: {exc}", hint="see stderr")
        )
    _attach_run(res, failed=failed)
    res.elapsed_s = time.monotonic() - started
    return emit(res)


if __name__ == "__main__":
    sys.exit(main())
