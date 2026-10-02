"""cadlib: library behind scripts/cad.py. See API.md for module ownership and signatures."""

# Modules that may define COMMANDS (see command.py). Missing modules are skipped, so tracks can
# land independently. Order = order in `--help`.
MODULES = ("doctor", "dxf", "convert", "render", "cleanup", "edit", "plot")

__version__ = "0.1.0"
