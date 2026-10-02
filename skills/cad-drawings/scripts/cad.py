"""Single entry point: python cad.py <command> [args]. Every command prints one short JSON.

Exit codes: 0 ok, 1 error, 2 bad arguments, 3 missing dependency or backend, 4 resource busy,
5 timeout, 6 precondition failed, 7 partial success. See assets/output.schema.json.
No command ever prompts; destructive steps need --yes or --overwrite.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cadlib import MODULES
from cadlib.command import Command
from cadlib.result import CadError, ExitCode, Result, emit, force_utf8


def load_commands() -> dict[str, Command]:
    commands: dict[str, Command] = {}
    for name in MODULES:
        try:
            module = importlib.import_module(f"cadlib.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"cadlib.{name}":
                continue  # module not delivered yet
            raise
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


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    commands = load_commands()
    args = build_parser(commands).parse_args(argv)
    started = time.monotonic()
    try:
        res = commands[args.command].run(args)
    except CadError as err:
        res = Result.from_error(args.command, err)
    except KeyboardInterrupt:
        res = Result.from_error(
            args.command, CadError("INTERRUPTED", "interrupted", exit_code=ExitCode.ERROR)
        )
    except Exception as exc:  # noqa: BLE001 - last resort: never a bare traceback on stdout
        print(traceback.format_exc(), file=sys.stderr)
        res = Result.from_error(
            args.command, CadError("UNEXPECTED", f"{type(exc).__name__}: {exc}", hint="see stderr")
        )
    res.elapsed_s = time.monotonic() - started
    return emit(res)


if __name__ == "__main__":
    sys.exit(main())
