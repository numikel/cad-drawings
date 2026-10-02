"""DXF commands: info, find, dump, fingerprint, diff (wiring; logic lives in the modules)."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .command import Command
from .diffing import DIFF_JSON_CAP, SUMMARY_CHANGES, diff_fingerprints
from .drawing import Loaded, _resolve_source, open_drawing
from .drawing_info import build_info
from .entities import Loc, describe, expect_for, iter_locations
from .fingerprint import _fingerprint_of, build_fingerprint
from .printing import DocModel
from .result import CadError, Result
from .search import _WHERE, MAX_PATTERN_CHARS, MAX_SUBJECT_CHARS, compile_pattern, find_hits
from .util import (
    Deadline,
    _add_common,
    _finish,
    _jsonable,
    _new_run,
    _write_json,
    atomic_write,
    conversion_options,
    parse_floats,
    refuse_input_as_output,
)

DEFAULT_DUMP_LIMIT = 5000
DEFAULT_FIND_LIMIT = 20000


def _open(path: Path, args: argparse.Namespace, ctx: Any, result: Result) -> Loaded:
    """Open a drawing with the shared conversion flags; every warning reaches the result."""
    loaded = open_drawing(path, ctx, **conversion_options(args))
    for warning in loaded.warnings:
        result.warn(warning)
    if loaded.approximate:
        result.approximate = True
    return loaded


def _run_info(args: argparse.Namespace) -> Result:
    ctx = _new_run("info", args)
    result = Result("info", backend="ezdxf")
    loaded = _open(args.file, args, ctx, result)
    info = build_info(loaded, args.conventions)
    path = ctx.path("info.json")
    _write_json(path, info)
    ctx.add_output(result, "info", path, source=loaded.source)
    vps = [vp for lay in info["layouts"] for vp in lay.get("viewports", []) if not vp["overall"]]
    result.summary = {
        "file": loaded.source.name,
        "dxf_version": info["dxf_version"],
        "insunits": info["units"]["insunits_name"],
        "measurement": info["units"]["measurement_name"],
        "layouts": [lay["name"] for lay in info["layouts"] if lay["name"].lower() != "model"],
        "viewports": len(vps),
        "viewports_status_unreliable": sum(1 for vp in vps if vp["status"] <= 0),
        "layers": {
            "count": len(info["layers"]),
            "off": sum(1 for x in info["layers"] if x["off"]),
            "frozen": sum(1 for x in info["layers"] if x["frozen"]),
        },
        "blocks": {
            "count": len(info["blocks"]),
            "anonymous": sum(1 for b in info["blocks"] if b["anonymous"]),
        },
        "xrefs": {
            "count": len(info["xrefs"]),
            "unresolved": sum(1 for x in info["xrefs"] if not x["resolved_on_disk"]),
            "unchecked": sum(1 for x in info["xrefs"] if not x["checked"]),
            "bound": len(info["bound_xref_prefixes"]),
        },
        "lock_files": info["lock_files"],
    }
    for warning in info["warnings"]:
        result.warn(warning)
    for x in info["xrefs"]:
        if not x["checked"]:
            result.warn(f"xref {x['block']!r} not checked: network or non-local path {x['path']}")
        elif not x["resolved_on_disk"]:
            result.warn(f"xref {x['block']!r} not found on disk: {x['path']}")
    if info["lock_files"]:
        result.warn("lock files beside the source: the drawing may be open in a CAD application")
    return _finish(result, ctx)


def _add_info_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path, help="DXF or DWG file")
    p.add_argument("--conventions", action="store_true", help="add text styles, title block guess")
    _add_common(p)


def _run_find(args: argparse.Namespace) -> Result:
    ctx = _new_run("find", args)
    where = {w.strip() for w in args.where.split(",") if w.strip()}
    if not where or not where <= set(_WHERE):
        raise CadError("BAD_ARGS", f"--where takes a subset of {','.join(_WHERE)}")
    rx = compile_pattern(args.pattern, args.ignore_case)
    result = Result("find", backend="ezdxf")
    deadline = Deadline(args.timeout)
    path = ctx.path("find.jsonl")
    by_type: Counter[str] = Counter()
    by_space: Counter[str] = Counter()
    by_file: Counter[str] = Counter()
    stats: dict[str, int] = {}
    total = hidden_n = 0
    truncated = False
    with path.open("w", encoding="utf-8") as out:
        for file in args.files:
            loaded = _open(file, args, ctx, result)
            for w in DocModel(loaded.doc).warnings:
                result.warn(f"{loaded.source.name}: {w}")
            for hit in find_hits(loaded, rx, where, args.hidden, deadline, stats):
                if total >= args.limit:
                    truncated = True
                    break
                total += 1
                by_type[hit["type"]] += 1
                by_space[hit["space"]] += 1
                by_file[Path(hit["file"]).name] += 1
                hidden_n += hit["visible_in_space"] is False
                out.write(json.dumps(_jsonable(hit), ensure_ascii=False) + "\n")
            if truncated:
                break
    if truncated:
        result.warn(f"stopped at --limit {args.limit}; narrow the pattern or raise the limit")
    cut = stats.get("truncated_subjects", 0)
    if cut:
        result.warn(f"{cut} text(s) longer than {MAX_SUBJECT_CHARS} characters were searched cut")
    result.summary = {
        "hits": total,
        "hidden": hidden_n,
        "by_type": dict(by_type),
        "by_space": dict(by_space),
        "by_file": dict(by_file),
        "truncated": truncated,
        "truncated_subjects": cut,
        "prints_on_basis": "viewport geometry, not a plot",
    }
    ctx.add_output(result, "hits", path)
    if not total:
        result.next.append("no hits: try -i, a looser pattern or --where text,attrib,layer,block")
    return _finish(result, ctx)


def _add_find_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("files", nargs="+", type=Path, help="DXF or DWG files")
    p.add_argument(
        "--pattern",
        required=True,
        help=(
            f"regular expression (Python syntax, at most {MAX_PATTERN_CHARS} characters); "
            f"each text is cut to {MAX_SUBJECT_CHARS} characters before matching "
            "(counted as truncated_subjects)"
        ),
    )
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("--where", default=",".join(_WHERE), help="subset of text,attrib,layer,block")
    p.add_argument("--hidden", choices=("include", "only", "exclude"), default="include")
    p.add_argument("--limit", type=int, default=DEFAULT_FIND_LIMIT, help="maximum hits written")
    _add_common(p)


def _overlaps(bbox: list[float] | None, anchor: list[float] | None, win: list[float]) -> bool:
    x1, y1, x2, y2 = (
        min(win[0], win[2]),
        min(win[1], win[3]),
        max(win[0], win[2]),
        max(win[1], win[3]),
    )
    if bbox is not None:
        return not (bbox[2] < x1 or bbox[0] > x2 or bbox[3] < y1 or bbox[1] > y2)
    return anchor is not None and x1 <= anchor[0] <= x2 and y1 <= anchor[1] <= y2


def _space_matches(loc: Loc, space: str) -> bool:
    if space == "all":
        return True
    if space == "model":
        return loc.space == "model"
    if space == "paper":
        return loc.space == "paper"
    return (loc.layout or "").lower() == space.lower() or (loc.block or "").lower() == space.lower()


def _run_dump(args: argparse.Namespace) -> Result:
    ctx = _new_run("dump", args)
    result = Result("dump", backend="ezdxf")
    deadline = Deadline(args.timeout)
    types = {t.upper() for t in args.type or []}
    layers = {x.lower() for x in args.layer or []}
    handles = {h.upper() for h in args.handle or []}
    window = parse_floats(args.window, 4, "--window") if args.window else None
    loaded = _open(args.file, args, ctx, result)
    path = ctx.path("dump.jsonl")
    written = 0
    truncated = False
    by_type: Counter[str] = Counter()
    model = DocModel(loaded.doc)
    with path.open("w", encoding="utf-8") as out:
        for i, loc in enumerate(iter_locations(loaded.doc)):
            if i % 2000 == 0:
                deadline.check()
            e = loc.entity
            if not _space_matches(loc, args.space):
                continue
            if types and e.dxftype() not in types:
                continue
            if layers and e.dxf.layer.lower() not in layers:
                continue
            if handles and str(e.dxf.handle).upper() not in handles:
                continue
            rec = describe(loc, with_bbox=True)
            if window is not None and not _overlaps(rec.get("bbox"), rec["anchor"], window):
                continue
            if written >= args.limit:
                truncated = True
                break
            rec.update(model.locate(loc, rec.get("bbox")))
            rec["expect"] = expect_for(loc)
            out.write(json.dumps(_jsonable(rec), ensure_ascii=False) + "\n")
            written += 1
            by_type[rec["type"]] += 1
    if truncated:
        result.warn(f"output cut at --limit {args.limit}; add filters or raise the limit")
    result.summary = {"entities": written, "by_type": dict(by_type), "truncated": truncated}
    ctx.add_output(result, "entities", path, source=loaded.source)
    return _finish(result, ctx)


def _add_dump_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path)
    p.add_argument("--space", default="all", help="model | paper | all | layout or block name")
    p.add_argument("--type", action="append", help="entity type, repeatable (e.g. TEXT)")
    p.add_argument("--layer", action="append", help="layer name, repeatable")
    p.add_argument("--window", help="X1,Y1,X2,Y2 in drawing units (bbox overlap)")
    p.add_argument("--handle", action="append", help="entity handle, repeatable")
    p.add_argument("--limit", type=int, default=DEFAULT_DUMP_LIMIT)
    _add_common(p)


def _run_fingerprint(args: argparse.Namespace) -> Result:
    ctx = _new_run("fingerprint", args)
    result = Result("fingerprint", backend="ezdxf")
    deadline = Deadline(args.timeout)
    src = _resolve_source(args.file)
    dest = args.out.expanduser() if args.out is not None else None
    if dest is not None:
        refuse_input_as_output(dest, [src])
        if dest.exists() and not args.overwrite:
            raise CadError(
                "EXISTS", f"{dest} exists", hint="pass --overwrite or choose another --out"
            )
    loaded = _open(src, args, ctx, result)
    fp = build_fingerprint(loaded, ctx, deadline)
    out = ctx.path("fingerprint.json")
    _write_json(out, fp)
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = out.read_bytes()
        atomic_write(dest, lambda tmp: tmp.write_bytes(data))
        out = dest
    for w in fp["warnings"]:
        result.warn(w)
    result.summary = {
        "entities": fp["total"],
        "by_type": fp["counts"],
        "layouts": [x["name"] for x in fp["layouts"]],
        "viewports": sum(1 for v in fp["viewports"] if not v["overall"]),
    }
    ctx.add_output(result, "fingerprint", out, source=loaded.source)
    return _finish(result, ctx)


def _add_fingerprint_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", type=Path)
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also write the fingerprint here (never an input file; written atomically)",
    )
    p.add_argument("--overwrite", action="store_true")
    _add_common(p)


def _run_diff(args: argparse.Namespace) -> Result:
    ctx = _new_run("diff", args)
    result = Result("diff", backend="ezdxf")
    deadline = Deadline(args.timeout)
    options = conversion_options(args)
    fp_a, approx_a = _fingerprint_of(args.a, ctx, deadline, **options)
    fp_b, approx_b = _fingerprint_of(args.b, ctx, deadline, **options)
    if approx_a or approx_b:
        result.approximate = True
        result.warn(
            "an input came from an approximate DWG conversion; differences may be artefacts"
        )
    for fp, label in ((fp_a, "A"), (fp_b, "B")):
        for w in fp.get("warnings", []):
            result.warn(f"{label}: {w}")
    report = diff_fingerprints(fp_a, fp_b, full=args.full)
    report["a"], report["b"] = fp_a["source"], fp_b["source"]
    path = ctx.path("diff.json")
    _write_json(path, report)
    ctx.add_output(result, "diff", path)
    all_changes = report["structural"] + report["changes"]
    first = [c["description"] for c in all_changes[:SUMMARY_CHANGES]]
    if report["truncated"]:
        result.warn(f"diff.json lists the first {DIFF_JSON_CAP} changes; use --full for all")
    result.summary = {
        **report["counts"],
        "structural": len(report["structural"]),
        "identical": report["total_changes"] == 0,
        "noise": report["noise"],
        "first_changes": first,
    }
    if report["counts"]["removed"] or report["counts"]["added"]:
        result.next.append(
            "added/removed items are unmatched, not explained: confirm with the author"
        )
    return _finish(result, ctx)


def _add_diff_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("a", type=Path, help="base: drawing or fingerprint .json")
    p.add_argument("b", type=Path, help="changed: drawing or fingerprint .json")
    p.add_argument("--full", action="store_true", help="no cap on the changes listed in diff.json")
    _add_common(p)


COMMANDS = {
    "info": Command(
        help="units, layouts, viewports, layers, blocks, xrefs and lock files of a drawing",
        add_arguments=_add_info_args,
        run=_run_info,
        epilog="example: cad.py info plan.dwg --conventions",
    ),
    "find": Command(
        help="regex search in text, attributes, layer and block names, all spaces",
        add_arguments=_add_find_args,
        run=_run_find,
        epilog="example: cad.py find a.dxf b.dxf --pattern 'fire|exit' -i --hidden include",
    ),
    "dump": Command(
        help="entities as JSONL with filters (space, type, layer, window, handle)",
        add_arguments=_add_dump_args,
        run=_run_dump,
        epilog="example: cad.py dump plan.dxf --space Sheet-A --type TEXT --type MTEXT",
    ),
    "fingerprint": Command(
        help="compact JSON fingerprint of the graphic entities (for diff)",
        add_arguments=_add_fingerprint_args,
        run=_run_fingerprint,
        epilog="example: cad.py fingerprint plan.dwg --out plan.fp.json",
    ),
    "diff": Command(
        help="semantic diff of two drawings or fingerprints, ignoring re-save noise",
        add_arguments=_add_diff_args,
        run=_run_diff,
        epilog="example: cad.py diff v1.fp.json v2.dwg",
    ),
}
