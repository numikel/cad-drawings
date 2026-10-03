"""cadlib: library behind scripts/cad.py. See API.md for module ownership and signatures."""

# Modules that may define COMMANDS (see command.py). Missing modules are skipped, so tracks can
# land independently. Order = order in `--help`. Helper modules are NOT listed here.
MODULES = ("doctor", "dxf", "convert", "render", "cleanup", "edit", "plot", "qa")

# Which module provides which command. Used only to build "missing dependency" stand-ins when a
# module cannot be imported (e.g. ezdxf is not installed), so the other commands keep working.
COMMAND_MODULES = {
    "doctor": "doctor",
    "info": "dxf",
    "find": "dxf",
    "dump": "dxf",
    "fingerprint": "dxf",
    "diff": "dxf",
    "convert": "convert",
    "render": "render",
    "cleanup": "cleanup",
    "edit": "edit",
    "plot": "plot",
    "qa": "qa",
}

__version__ = "0.1.0"
