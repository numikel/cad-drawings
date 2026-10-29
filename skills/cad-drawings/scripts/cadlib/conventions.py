"""conventions: what a set of recently issued drawings has in common.

``info FILE --conventions`` describes one drawing. A project's ``CAD_CONVENTIONS.md`` is built
from several (the majority pattern becomes the rule), so this command reads many files and counts
how many of them follow each pattern. Nothing is extracted here: every fact comes from
``drawing_info.build_info`` and is only tallied, so ``count`` and ``of`` can go straight into the
``Source`` column of the conventions file (``inferred: 41 of 44 layouts use A1``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .command import Command
from .drawing import open_drawing
from .drawing_info import build_info
from .result import MAX_JSON_BYTES, CadError, ExitCode, Result
from .runs import note_child
from .util import DEFAULT_TIMEOUT_S, _add_common, _finish, _new_run, _write_json, conversion_options

DEFAULT_MAX_FILES = 20
SUFFIXES = (".dxf", ".dwg")
SUMMARY_TOP = 5  # entries per aggregate in the printed summary, before it is fitted to the cap
MAX_FIELD_CHARS = 48
MAX_WARNING_CHARS = 160
MAX_SKIP_WARNINGS = 3
MAX_FILE_WARNINGS = 4

# ISO 216 sheets (long side, short side) in millimetres; matched within PAPER_TOLERANCE_MM.
ISO_SHEETS = {
    "A0": (1189, 841),
    "A1": (841, 594),
    "A2": (594, 420),
    "A3": (420, 297),
    "A4": (297, 210),
}
PAPER_TOLERANCE_MM = 2

AGGREGATES = (
    "units",
    "paper_sizes",
    "text_styles",
    "text_heights",
    "layers",
    "layer_prefixes",
    "block_prefixes",
    "title_block_blocks",
    "layout_name_patterns",
)

BASIS = {
    "units": "files: $INSUNITS and $MEASUREMENT",
    "paper_sizes": "paper layouts with a page setup size; orientation ignored",
    "text_styles": "files containing a text style of that name (case-insensitive)",
    "text_heights": (
        "files where the height is among the 10 most used TEXT/MTEXT heights; 0 (unset) ignored"
    ),
    "layers": "files containing a layer of that name (case-insensitive)",
    "layer_prefixes": "files where the prefix is among the 10 most common layer-name prefixes",
    "block_prefixes": "files where the prefix is among the 10 most common block-name prefixes",
    "title_block_blocks": "files where the block repeats on most paper layouts (title block guess)",
    "layout_name_patterns": "paper layouts; digit runs replaced by #",
}


@dataclass
class FileFacts:
    """What one drawing contributes to the tallies."""

    units: tuple[str, str]
    paper_sizes: list[tuple[int, int]] = field(default_factory=list)
    layouts_without_size: int = 0
    text_styles: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )  # lower name -> (name, font)
    text_heights: list[float] = field(default_factory=list)
    layers: dict[str, str] = field(default_factory=dict)  # lower-case name -> name
    layer_prefixes: list[str] = field(default_factory=list)
    block_prefixes: list[str] = field(default_factory=list)
    title_blocks: list[str] = field(default_factory=list)
    layout_names: list[str] = field(default_factory=list)


# -- reading one file -----------------------------------------------------------------------


def _paper_size(width: float, height: float) -> tuple[int, int] | None:
    if width <= 0 or height <= 0:
        return None
    return round(max(width, height)), round(min(width, height))


def _iso_name(size: tuple[int, int]) -> str:
    for name, (long_side, short_side) in ISO_SHEETS.items():
        if (
            abs(size[0] - long_side) <= PAPER_TOLERANCE_MM
            and abs(size[1] - short_side) <= PAPER_TOLERANCE_MM
        ):
            return name
    return "custom"


def facts_from_info(info: dict[str, Any]) -> FileFacts:
    """Reduce one ``build_info(..., conventions=True)`` report to what is tallied."""
    conv = info["conventions"]
    facts = FileFacts(units=(info["units"]["insunits_name"], info["units"]["measurement_name"]))
    for layout in info["layouts"]:
        setup = layout.get("page_setup")
        if setup is None:  # model space
            continue
        size = _paper_size(*setup["paper_size_mm"])
        if size is None:
            facts.layouts_without_size += 1
        else:
            facts.paper_sizes.append(size)
    for style in conv["text_styles"]:
        facts.text_styles.setdefault(style["name"].lower(), (style["name"], style["font"]))
    facts.text_heights = [h["height"] for h in conv["text_heights"] if h["height"] > 0]
    for layer in info["layers"]:
        facts.layers.setdefault(layer["name"].lower(), layer["name"])
    facts.layer_prefixes = [p["prefix"] for p in conv["layer_prefixes"]]
    facts.block_prefixes = [p["prefix"] for p in conv["block_prefixes"]]
    facts.title_blocks = [
        b["block"] for b in conv["title_block_guess"]["repeated_paper_space_blocks"]
    ]
    facts.layout_names = list(conv["layout_names"])
    return facts


def _start_cad(timeout: float) -> Any:
    """Start the CAD instance (a context manager). Separate so that tests can replace it."""
    from .acad import AcadSession

    return AcadSession.start(timeout=timeout)


class _SharedCad:
    """One CAD instance for the whole run, started on the first DWG that needs converting.

    A cached conversion, ODA or LibreDWG never touch it, so CAD may not start at all. It stands
    in for an ``AcadSession`` in ``convert.ensure_dxf(session=...)``, which leaves it running.
    """

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._manager: Any = None
        self._session: Any = None

    def _get(self) -> Any:
        if self._session is None:
            self._manager = _start_cad(self._timeout)
            self._session = self._manager.__enter__()
            pid = getattr(self._session, "pid", None)
            if isinstance(pid, int):
                note_child(pid, str(getattr(self._session, "image", "cad")))
        return self._session

    def export_dxf(self, src: Path, dst: Path, *args: Any, **kwargs: Any) -> Any:
        return self._get().export_dxf(src, dst, *args, **kwargs)

    def open(self, path: Path, *args: Any, **kwargs: Any) -> Any:
        return self._get().open(path, *args, **kwargs)

    def save_dwg(self, doc: Any, dst: Path, *args: Any, **kwargs: Any) -> Any:
        return self._get().save_dwg(doc, dst, *args, **kwargs)

    @property
    def warnings(self) -> list[str]:
        return list(getattr(self._session, "warnings", None) or [])

    def close(self) -> None:
        """Quit the instance if it was started (always, also after an error)."""
        if self._manager is not None:
            manager, self._manager = self._manager, None
            manager.__exit__(None, None, None)


def _read(
    path: Path, args: argparse.Namespace, ctx: Any, session: Any = None
) -> tuple[FileFacts, list[str], bool]:
    """Open ``path`` and extract its facts: (facts, warnings, approximate)."""
    loaded = open_drawing(path, ctx, **conversion_options(args), session=session)
    info = build_info(loaded, True)
    return facts_from_info(info), list(info["warnings"]), loaded.approximate


# -- tallies --------------------------------------------------------------------------------


class Tally:
    """Counts how many files (or layouts) show each key; remembers a label for the report."""

    def __init__(self) -> None:
        self._rows: dict[Any, dict[str, Any]] = {}

    def add(self, key: Any, payload: dict[str, Any]) -> None:
        row = self._rows.get(key)
        if row is None:
            self._rows[key] = {**payload, "count": 1}
        else:
            row["count"] += 1

    def add_once_per_file(self, keys: dict[Any, dict[str, Any]]) -> None:
        for key, payload in keys.items():
            self.add(key, payload)

    def ranked(self, of: int) -> list[dict[str, Any]]:
        """Entries with ``count`` and ``of``, most common first, ties in a stable order."""
        ordered = sorted(self._rows.items(), key=lambda kv: (-kv[1]["count"], str(kv[0])))
        return [{**row, "of": of} for _, row in ordered]


def _pattern(name: str) -> str:
    return re.sub(r"\d+", "#", name)


def aggregate(all_facts: list[FileFacts]) -> dict[str, list[dict[str, Any]]]:
    """Tally the facts of all sampled files into the ``AGGREGATES`` tables."""
    units, sizes, styles, heights, layers = Tally(), Tally(), Tally(), Tally(), Tally()
    layer_prefixes, block_prefixes, title_blocks, patterns = Tally(), Tally(), Tally(), Tally()
    sized_layouts = named_layouts = 0
    for f in all_facts:
        units.add(f.units, {"insunits": f.units[0], "measurement": f.units[1]})
        for size in f.paper_sizes:
            sizes.add(size, {"name": _iso_name(size), "size_mm": list(size)})
        sized_layouts += len(f.paper_sizes)
        styles.add_once_per_file(
            {key: {"name": name, "font": font} for key, (name, font) in f.text_styles.items()}
        )
        heights.add_once_per_file({h: {"height": h} for h in dict.fromkeys(f.text_heights)})
        layers.add_once_per_file({key: {"name": name} for key, name in f.layers.items()})
        layer_prefixes.add_once_per_file(
            {p.lower(): {"prefix": p} for p in reversed(dict.fromkeys(f.layer_prefixes))}
        )
        block_prefixes.add_once_per_file(
            {p.lower(): {"prefix": p} for p in reversed(dict.fromkeys(f.block_prefixes))}
        )
        title_blocks.add_once_per_file({b.lower(): {"block": b} for b in f.title_blocks})
        for name in f.layout_names:
            patterns.add(_pattern(name), {"pattern": _pattern(name), "example": name})
        named_layouts += len(f.layout_names)
    n = len(all_facts)
    return {
        "units": units.ranked(n),
        "paper_sizes": sizes.ranked(sized_layouts),
        "text_styles": styles.ranked(n),
        "text_heights": heights.ranked(n),
        "layers": layers.ranked(n),
        "layer_prefixes": layer_prefixes.ranked(n),
        "block_prefixes": block_prefixes.ranked(n),
        "title_block_blocks": title_blocks.ranked(n),
        "layout_name_patterns": patterns.ranked(named_layouts),
    }


# -- picking the files ----------------------------------------------------------------------


def collect_files(paths: list[Path], recursive: bool) -> list[Path]:
    """DXF/DWG files named by ``paths`` (files, or folders searched for them), each once."""
    found: dict[str, Path] = {}
    for raw in paths:
        path = raw.expanduser()
        if path.is_dir():
            try:
                walker = path.rglob("*") if recursive else path.iterdir()
                hits = [p for p in walker if p.suffix.lower() in SUFFIXES and p.is_file()]
            except OSError as exc:
                raise CadError("UNREADABLE", f"cannot list {path}: {exc}") from exc
            if not hits:
                raise CadError(
                    "FILE_NOT_FOUND",
                    f"no DXF or DWG files in {path}",
                    hint="check the folder, or pass --recursive to search subfolders",
                )
        elif path.is_file():
            if path.suffix.lower() not in SUFFIXES:
                raise CadError(
                    "UNSUPPORTED", f"unsupported file type {path.suffix!r} (expected .dxf or .dwg)"
                )
            hits = [path]
        else:
            raise CadError("FILE_NOT_FOUND", f"path not found: {path}", hint="check the path")
        for hit in hits:
            resolved = hit.resolve()
            found.setdefault(os.path.normcase(str(resolved)), resolved)
    return sorted(found.values(), key=_newest_first)


def _newest_first(path: Path) -> tuple[float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return -mtime, path.name.lower()


# -- the command ----------------------------------------------------------------------------


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _error_of(exc: Exception) -> CadError:
    if isinstance(exc, CadError):
        return exc
    return CadError("UNREADABLE", f"{type(exc).__name__}: {exc}")


def _compact(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: _short(v, MAX_FIELD_CHARS) if isinstance(v, str) else v for k, v in entry.items()}


def _fit(result: Result, base: dict[str, Any], tables: dict[str, list[dict[str, Any]]]) -> None:
    """Show the top entries of every table, as many as fit under the JSON cap."""
    for top in range(SUMMARY_TOP, -1, -1):
        result.summary = {
            **base,
            "top": {name: [_compact(e) for e in tables[name][:top]] for name in AGGREGATES},
        }
        if top == 0:
            result.summary.pop("top")
            result.summary["top_omitted"] = "see conventions.json"
        size = len(json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8"))
        if size <= MAX_JSON_BYTES:
            return


def _run_conventions(args: argparse.Namespace) -> Result:
    if args.max_files < 1:
        raise CadError("BAD_ARGS", "--max-files must be at least 1")
    files = collect_files(args.paths, args.recursive)
    chosen, rest = files[: args.max_files], files[args.max_files :]

    ctx = _new_run("conventions", args)
    result = Result("conventions", backend="ezdxf")
    started = time.monotonic()
    limit = args.timeout if args.timeout else None

    all_facts: list[FileFacts] = []
    records: list[dict[str, Any]] = []
    failures: list[CadError] = []
    file_warnings: list[str] = []
    unvisited = 0
    # with consent, one CAD instance serves every DWG of the run (started only if one needs it)
    shared = _SharedCad(DEFAULT_TIMEOUT_S) if conversion_options(args).get("allow_com") else None
    try:
        for done, path in enumerate(chosen):
            if limit is not None and done and time.monotonic() - started > limit:
                unvisited = len(chosen) - done
                break
            record: dict[str, Any] = {
                "name": path.name,
                "path": str(path),
                "ok": False,
                "error": None,
            }
            try:
                facts, warnings, approximate = _read(path, args, ctx, shared)
            except Exception as exc:  # noqa: BLE001 - one bad file must not end the whole run
                err = _error_of(exc)
                failures.append(err)
                record["error"] = {"code": err.code, "message": err.message}
                if len(failures) <= MAX_SKIP_WARNINGS:
                    result.warn(
                        _short(
                            f"{path.name}: skipped ({err.code}: {err.message})", MAX_WARNING_CHARS
                        )
                    )
            else:
                all_facts.append(facts)
                record.update(
                    ok=True,
                    warnings=warnings,
                    paper_layouts=len(facts.layout_names),
                    layers=len(facts.layers),
                )
                if approximate:
                    result.approximate = True
                file_warnings.extend(f"{path.name}: {w}" for w in warnings)
            records.append(record)
            ctx.progress(done + 1, len(chosen), path.name)
    finally:
        if shared is not None:
            shared.close()
            for warning in shared.warnings:
                result.warn(_short(warning, MAX_WARNING_CHARS))

    tables = aggregate(all_facts)
    counts = {
        "sampled": len(all_facts),
        "skipped": len(failures),
        "not_sampled": len(rest) + unvisited,
        # layouts whose paper size is unknown are not in the `of` of paper_sizes
        "layouts_without_paper_size": sum(f.layouts_without_size for f in all_facts),
    }
    report = {
        **counts,
        "files": records,
        "not_sampled_files": [str(p) for p in (*rest, *chosen[len(chosen) - unvisited :])],
        "aggregates": tables,
        "basis": BASIS,
    }
    out = ctx.path("conventions.json")
    _write_json(out, report)
    ctx.add_output(result, "conventions", out)

    if not all_facts:
        first = failures[0]
        same = len({e.code for e in failures}) == 1
        message = f"none of the {len(failures)} file(s) could be read: {first.message}"
        hint = first.hint if same else "see conventions.json in the run directory for each file"
        if same:
            raise CadError(first.code, _short(message, 300), exit_code=first.exit_code, hint=hint)
        raise CadError("UNREADABLE", _short(message, 300), hint=hint)

    if len(failures) > MAX_SKIP_WARNINGS:
        result.warn(
            f"{len(failures) - MAX_SKIP_WARNINGS} more file(s) skipped, see conventions.json"
        )
    for text in file_warnings[:MAX_FILE_WARNINGS]:
        result.warn(_short(text, MAX_WARNING_CHARS))
    if len(file_warnings) > MAX_FILE_WARNINGS:
        result.warn(
            f"{len(file_warnings) - MAX_FILE_WARNINGS} more file warning(s), see conventions.json"
        )
    if unvisited:
        result.warn(
            f"--timeout reached: {unvisited} file(s) not read; raise --timeout or lower --max-files"
        )
    if rest:
        result.next.append(
            f"{len(rest)} older file(s) beyond --max-files were not read; "
            "raise --max-files to include them"
        )
    result.next.append(
        "use count/of as the Source column of CAD_CONVENTIONS.md; confirm minority patterns with the user"
    )

    if failures or unvisited:
        result.exit_code = ExitCode.PARTIAL
    _finish(result, ctx)
    _fit(result, counts, tables)
    return result


def _add_conventions_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "paths",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="DXF or DWG files, or folders holding them (top level unless --recursive)",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"read at most this many files, newest first (default {DEFAULT_MAX_FILES})",
    )
    p.add_argument("--recursive", action="store_true", help="search subfolders of a folder PATH")
    _add_common(p)


COMMANDS = {
    "conventions": Command(
        help="what recent drawings have in common: units, paper sizes, text, layers (counts)",
        add_arguments=_add_conventions_args,
        run=_run_conventions,
        epilog=(
            "example: cad.py conventions issued/ --max-files 20\n"
            "exit 0 all files read, 7 some skipped (DWG without converter, unreadable file)"
        ),
    ),
}
