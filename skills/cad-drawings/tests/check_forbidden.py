#!/usr/bin/env python3
"""Scan the repository for strings and files that must never be published.

Standard library only. Exit code 0: clean, 1: at least one hit, 2: bad arguments.

    python skills/cad-drawings/tests/check_forbidden.py [--root <repo root>]

The rules come from AGENTS.md ("Forbidden in the repo"). Content checks are skipped for the
policy documents that have to name the forbidden things (see CONTENT_ALLOWLIST).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# (needle, ignore_case). Short or common-looking needles stay case-sensitive to avoid
# false hits inside ordinary words.
FORBIDDEN_STRINGS: tuple[tuple[str, bool], ...] = (
    ("C:\\Users\\", True),
    ("C:/Users/", True),
    ("CLIENT", False),
    ("user", True),
    ("Folder", False),
    ("XXX", False),
    ("xxxx", True),
    ("xxxx", True),
    ("xxxxxx", True),
)

# Vendor files that must not be committed (matched on the file suffix, case-insensitive).
FORBIDDEN_EXTENSIONS: tuple[str, ...] = (
    ".pc3",
    ".pmp",
    ".ctb",
    ".stb",
    ".shx",
    ".lin",
    ".pat",
    ".dwt",
    ".tlb",
)

# Drawings are only allowed where they are generated: evals/fixtures (excluded from the scan).
CAD_EXTENSIONS: tuple[str, ...] = (".dwg", ".dxf")

# Names of external programs whose binaries must not be bundled (matched on the file stem).
FORBIDDEN_BINARY_STEMS: tuple[str, ...] = (
    "odafileconverter",
    "pdftoppm",
    "pdftocairo",
    "dwg2dxf",
    "dwgread",
)
FORBIDDEN_DIR_NAMES: tuple[str, ...] = ("gen_py",)

# Skipped entirely, by directory name anywhere in the tree.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"_local", ".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
# Skipped entirely, by path relative to the root (POSIX separators).
EXCLUDED_REL_PATHS: tuple[str, ...] = ("skills/cad-drawings/evals/fixtures",)

# Policy documents that describe the forbidden things; file names/extensions are still checked.
CONTENT_ALLOWLIST: frozenset[str] = frozenset(
    {"AGENTS.md", "skills/cad-drawings/tests/check_forbidden.py"}
)

BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class Violation:
    path: str
    line: int  # 0 for file-level findings
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.message}"


def _is_excluded(rel: Path) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
        return True
    posix = rel.as_posix()
    return any(posix == ex or posix.startswith(ex + "/") for ex in EXCLUDED_REL_PATHS)


def _check_name(rel: Path) -> list[Violation]:
    found: list[Violation] = []
    suffix = rel.suffix.lower()
    if suffix in FORBIDDEN_EXTENSIONS:
        found.append(Violation(rel.as_posix(), 0, f"vendor file extension {suffix}"))
    if suffix in CAD_EXTENSIONS:
        found.append(Violation(rel.as_posix(), 0, f"CAD drawing outside evals/fixtures ({suffix})"))
    if rel.stem.lower() in FORBIDDEN_BINARY_STEMS:
        found.append(Violation(rel.as_posix(), 0, "bundled binary of an external program"))
    for part in rel.parts[:-1]:
        if part in FORBIDDEN_DIR_NAMES:
            found.append(Violation(rel.as_posix(), 0, f"forbidden directory {part}/"))
    return found


def _check_content(path: Path, rel: Path) -> list[Violation]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [Violation(rel.as_posix(), 0, f"unreadable: {exc}")]
    if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        return []
    found: list[Violation] = []
    for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        lowered = line.lower()
        for needle, ignore_case in FORBIDDEN_STRINGS:
            hit = needle.lower() in lowered if ignore_case else needle in line
            if hit:
                found.append(Violation(rel.as_posix(), number, f"forbidden string {needle!r}"))
    return found


def find_violations(root: Path) -> list[Violation]:
    """Return every violation below ``root`` (sorted by path, then line)."""
    root = root.resolve()
    found: list[Violation] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _is_excluded(rel):
            continue
        if path.is_dir():
            continue  # files inside a forbidden directory are reported individually
        found.extend(_check_name(rel))
        if rel.as_posix() not in CONTENT_ALLOWLIST:
            found.extend(_check_content(path, rel))
    return found


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Fail on forbidden strings and vendor files.")
    parser.add_argument("--root", type=Path, default=default_root, help="repository root")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2
    violations = find_violations(args.root)
    for violation in violations:
        print(violation)
    if violations:
        print(f"{len(violations)} forbidden item(s) found", file=sys.stderr)
        return 1
    print("check_forbidden: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
