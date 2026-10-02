"""Command registry contract. Each cadlib module that provides CLI commands defines COMMANDS.

    COMMANDS = {
        "info": Command(help="...", add_arguments=_add_info_args, run=_run_info),
    }

``scripts/cad.py`` imports the modules listed in ``cadlib.MODULES`` and builds the argparse
tree from their COMMANDS. Tracks add commands only inside their own modules; nobody edits
``cad.py`` except the main context.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from .result import Result


@dataclass(frozen=True)
class Command:
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], Result]
    epilog: str = ""  # examples and exit codes shown by `--help`
