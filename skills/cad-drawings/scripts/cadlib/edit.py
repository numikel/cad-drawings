"""The ``edit`` command: a safe executor for edit plans (``assets/edit-spec.schema.json``).

Pass 1 checks everything against the unchanged drawing and writes nothing; pass 2 applies the
edits to a working copy; a verification step then diffs the result against the original and
compares it with the plan. See ``edit_dxf`` (ezdxf backend) and ``acad_edit`` (COM backend).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from . import diffing, edit_dxf, runs
from .command import Command
from .drawing import Loaded, _read_dxf, open_drawing
from .edit_dxf import ApplyError, Inspection, Outcome, PlannedEdit, Problem
from .fingerprint import build_fingerprint
from .result import CadError, ExitCode, Result
from .runs import RunContext, file_sha1
from .util import Deadline, _add_common, _new_run, atomic_write, refuse_input_as_output

OPS = ("replace-text", "set-props", "delete", "move", "clone", "pan-viewport")
_REQUIRED_ARGS = {
    "replace-text": ("old", "new"),
    "set-props": ("props",),
    "move": ("vector",),
    "clone": ("vector",),
    "pan-viewport": ("view_center",),
}
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_HANDLE = re.compile(r"^[0-9A-Fa-f]+$")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _unknown_keys(obj: dict[str, Any], allowed: tuple[str, ...], where: str) -> list[str]:
    return [f"{where}: unexpected property {key!r}" for key in obj if key not in allowed]


def _missing_keys(obj: dict[str, Any], required: tuple[str, ...], where: str) -> list[str]:
    return [f"{where}: missing required property {key!r}" for key in required if key not in obj]


def _check_string(obj: dict[str, Any], key: str, where: str, out: list[str]) -> None:
    if key in obj and not isinstance(obj[key], str):
        out.append(f"{where}.{key}: must be a string")


def _check_point(obj: dict[str, Any], key: str, where: str, out: list[str]) -> None:
    if key not in obj:
        return
    value = obj[key]
    if not isinstance(value, list):
        out.append(f"{where}.{key}: must be an array of 2 or 3 numbers")
        return
    if not 2 <= len(value) <= 3:
        out.append(f"{where}.{key}: must have 2 or 3 items")
    if not all(_is_number(v) for v in value):
        out.append(f"{where}.{key}: items must be numbers")


def _check_expect(expect: Any, where: str) -> list[str]:
    if not _is_object(expect):
        return [f"{where}: must be an object"]
    out = _missing_keys(expect, ("type",), where)
    out += _unknown_keys(
        expect,
        ("type", "layer", "space", "text", "text_is_plain", "attrib", "insert", "tolerance"),
        where,
    )
    for key in ("type", "layer", "space", "text"):
        _check_string(expect, key, where, out)
    if "text_is_plain" in expect and not isinstance(expect["text_is_plain"], bool):
        out.append(f"{where}.text_is_plain: must be a boolean")
    if "attrib" in expect:
        attrib = expect["attrib"]
        if not _is_object(attrib):
            out.append(f"{where}.attrib: must be an object")
        else:
            out += [
                f"{where}.attrib.{tag}: must be a string"
                for tag, value in attrib.items()
                if not isinstance(value, str)
            ]
    _check_point(expect, "insert", where, out)
    if "tolerance" in expect and not _is_number(expect["tolerance"]):
        out.append(f"{where}.tolerance: must be a number")
    return out


def _check_args(args: Any, where: str) -> list[str]:
    if not _is_object(args):
        return [f"{where}: must be an object"]
    out = _unknown_keys(
        args,
        ("old", "new", "count", "props", "vector", "target_layer", "target_space", "view_center"),
        where,
    )
    for key in ("old", "new", "target_layer", "target_space"):
        _check_string(args, key, where, out)
    if "count" in args:
        count = args["count"]
        if not _is_integer(count):
            out.append(f"{where}.count: must be an integer")
        elif count < 1:
            out.append(f"{where}.count: must be at least 1")
    if "props" in args and not _is_object(args["props"]):
        out.append(f"{where}.props: must be an object")
    _check_point(args, "vector", where, out)
    _check_point(args, "view_center", where, out)
    return out


def _check_edit(edit: Any, where: str) -> list[str]:
    if not _is_object(edit):
        return [f"{where}: must be an object"]
    out = _missing_keys(edit, ("id", "op", "handle", "expect"), where)
    out += _unknown_keys(edit, ("id", "op", "handle", "expect", "args"), where)
    _check_string(edit, "id", where, out)
    op = edit.get("op")
    if "op" in edit and (not isinstance(op, str) or op not in OPS):
        out.append(f"{where}.op: must be one of {', '.join(OPS)}")
    if "handle" in edit:
        handle = edit["handle"]
        if not isinstance(handle, str):
            out.append(f"{where}.handle: must be a string")
        elif not _HANDLE.search(handle):
            out.append(f"{where}.handle: must be a hexadecimal handle, got {handle!r}")
    if "expect" in edit:
        out += _check_expect(edit["expect"], f"{where}.expect")
    if "args" in edit:
        out += _check_args(edit["args"], f"{where}.args")
    if isinstance(op, str) and op in _REQUIRED_ARGS:
        if "args" not in edit:
            out.append(f"{where}: op {op!r} requires 'args'")
        elif _is_object(edit["args"]):
            out += _missing_keys(edit["args"], _REQUIRED_ARGS[op], f"{where}.args")
    return out


def validate_spec(spec: Any) -> list[str]:
    """Structural problems of a plan; the same verdict as the JSON schema."""
    if not _is_object(spec):
        return ["plan: must be an object"]
    out = _missing_keys(spec, ("version", "base", "edits"), "plan")
    out += _unknown_keys(spec, ("version", "base", "output", "note", "edits"), "plan")
    if "version" in spec:
        version = spec["version"]
        if not (_is_number(version) and version == 1):
            out.append("plan.version: must be 1")
    if "base" in spec:
        base = spec["base"]
        if not _is_object(base):
            out.append("plan.base: must be an object")
        else:
            out += _missing_keys(base, ("path", "sha1"), "plan.base")
            out += _unknown_keys(base, ("path", "sha1"), "plan.base")
            _check_string(base, "path", "plan.base", out)
            if "sha1" in base:
                sha1 = base["sha1"]
                if not isinstance(sha1, str):
                    out.append("plan.base.sha1: must be a string")
                elif not _SHA1.search(sha1):
                    out.append("plan.base.sha1: must be 40 lowercase hex digits")
    if "output" in spec:
        output = spec["output"]
        if not _is_object(output):
            out.append("plan.output: must be an object")
        else:
            out += _unknown_keys(output, ("path", "overwrite"), "plan.output")
            _check_string(output, "path", "plan.output", out)
            if "overwrite" in output and not isinstance(output["overwrite"], bool):
                out.append("plan.output.overwrite: must be a boolean")
    _check_string(spec, "note", "plan", out)
    if "edits" in spec:
        edits = spec["edits"]
        if not isinstance(edits, list):
            out.append("plan.edits: must be an array")
        else:
            if not edits:
                out.append("plan.edits: needs at least one edit")
            for i, item in enumerate(edits):
                out += _check_edit(item, f"edits[{i}]")
    return out


def validate_semantics(spec: dict[str, Any]) -> list[str]:
    """Rules beyond the schema: unique ids, a non-empty ``old``. Call after ``validate_spec``."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(spec["edits"]):
        key = item["id"]
        if key in seen:
            out.append(f"edits[{i}].id: duplicate id {key!r} (first used by edits[{seen[key]}])")
        else:
            seen[key] = i
        args = item.get("args", {})
        if item["op"] == "replace-text":
            if args["old"] == "":
                out.append(f"edits[{i}].args.old: must not be empty")
            if args["old"] == args["new"]:
                out.append(f"edits[{i}].args: 'old' and 'new' are identical, nothing to replace")
        if item["op"] == "set-props" and not args["props"]:
            out.append(f"edits[{i}].args.props: must name at least one property")
    return out


# -- loading ------------------------------------------------------------------------------------
def load_spec(path: Path) -> dict[str, Any]:
    """Read and fully validate a plan; ``FILE_NOT_FOUND`` or ``SPEC_INVALID`` otherwise."""
    if not path.is_file():
        raise CadError("FILE_NOT_FOUND", f"plan not found: {path}", hint="check --spec")
    try:
        spec = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CadError("SPEC_INVALID", f"{path.name} is not valid JSON: {exc}") from exc
    problems = validate_spec(spec)
    if not problems:
        problems = validate_semantics(spec)
    if problems:
        raise CadError(
            "SPEC_INVALID",
            f"{len(problems)} problem(s) in the plan: " + "; ".join(problems[:5]),
            hint="fix the plan against assets/edit-spec.schema.json; nothing was written",
        )
    return spec  # type: ignore[no-any-return]


def _resolve_base(spec_path: Path, raw: str) -> Path:
    """``base.path`` as given, or next to the plan file when it is relative."""
    path = Path(raw).expanduser()
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, spec_path.parent / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CadError(
        "FILE_NOT_FOUND",
        f"the drawing named in the plan was not found: {raw}",
        hint="base.path is looked up as given, then next to the plan file",
    )


def _plan_output(args: argparse.Namespace, spec: dict[str, Any], src: Path) -> Path | None:
    """The delivery target (``--out`` or ``output.path``), refused early when it cannot be used."""
    wanted = spec.get("output", {})
    raw = args.out or wanted.get("path")
    if not raw:
        return None
    out = Path(raw).expanduser()
    refuse_input_as_output(out, [src])
    if out.suffix.lower() != src.suffix.lower():
        raise CadError(
            "BAD_ARGS",
            f"output {out.name} must have the source's type ({src.suffix}), not {out.suffix!r}",
        )
    if out.exists() and not (args.overwrite or wanted.get("overwrite")):
        raise CadError(
            "EXISTS",
            f"{out} already exists",
            hint="choose another path or pass --overwrite; nothing was changed",
        )
    return out


# -- COM session (lazy: started only when a DWG needs it) -----------------------------------------
class _ComHolder:
    def __init__(self, timeout: float, ctx: RunContext) -> None:
        self.timeout = timeout
        self.ctx = ctx
        self.session: Any = None

    def get(self) -> Any:
        if self.session is None:
            from . import acad_edit

            self.session = acad_edit.start_session(self.timeout, self.ctx.log)
        return self.session

    def close(self, result: Result) -> None:
        session, self.session = self.session, None
        if session is None:
            return
        try:
            session.quit()
        except CadError as err:
            result.warn(f"CAD instance did not close cleanly: {err.message}")
        for message in getattr(session, "warnings", []):
            result.warn(message)


def _load_baseline(src: Path, ctx: RunContext, com: _ComHolder | None) -> Loaded:
    """The original as an ezdxf document. A DWG goes through the CAD application, the same
    pipeline that exports the edited file later, so the two exports compare like for like."""
    if com is None:
        return open_drawing(src, ctx)
    from . import convert

    cached = runs.cache_get(file_sha1(src), "com")
    made = convert.ensure_dxf(
        src,
        ctx,
        allow_com=True,
        prefer="com",
        session=None if cached is not None else com.get(),
    )
    warnings = list(made.warnings)
    doc = _read_dxf(made.path, warnings)
    return Loaded(doc, src, made.path, warnings, False, made.backend)


# -- reports --------------------------------------------------------------------------------------
HANDLES_NOTE = (
    "Handles read from the original are valid for that file version only. The edited file is a "
    "new version (a save can renumber entities): run find or dump on it again before planning "
    "further edits."
)
PRIORITY = ("unsupported", "handle_not_found", "expect", "text_match", "precondition")


def _write_text(path: Path, text: str) -> None:
    def write(tmp: Path) -> None:
        tmp.write_text(text, encoding="utf-8")

    atomic_write(path, write)


def _write_changes(ctx: RunContext, outcomes: list[Outcome]) -> Path:
    path = ctx.path("changes.jsonl")
    lines = [json.dumps(o.as_dict(), ensure_ascii=False) for o in outcomes]
    _write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return path


def _write_report(ctx: RunContext, report: dict[str, Any]) -> Path:
    path = ctx.path("edit-report.json")
    _write_text(path, json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    return path


def _base_report(
    spec: dict[str, Any], src: Path, digest: str, backend: str, args: argparse.Namespace
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": 1,
        "base": {"path": str(src), "sha1": digest},
        "backend": backend,
        "dry_run": bool(args.dry_run),
        "handles": HANDLES_NOTE,
    }
    if "note" in spec:
        report["note"] = spec["note"]
    return report


def _refuse(
    result: Result,
    ctx: RunContext,
    report: dict[str, Any],
    problems: list[Problem],
) -> Result:
    """Pass 1 found problems: report every one, write nothing else."""
    report["problems"] = [p.as_dict() for p in problems]
    kinds = {p.kind for p in problems}
    kind = next(k for k in PRIORITY if k in kinds)
    code = edit_dxf.KIND_CODES[kind]
    first = [f"{p.edit_id}: {p.message}" for p in problems[:5]]
    ctx.add_output(result, "report", _write_report(ctx, report))
    result.summary = {"problems": len(problems), "applied": 0, "first": first}
    message = f"{len(problems)} problem(s) found before changing anything; first: " + "; ".join(
        first[:3]
    )
    if code == "UNSUPPORTED":
        err = CadError("UNSUPPORTED", message, hint=_unsupported_hint(problems))
    elif code == "PRECONDITION_FAILED":
        err = CadError("PRECONDITION_FAILED", message, hint=_FIX_HINT)
    else:
        err = CadError("EXPECT_FAILED", message, hint=_FIX_HINT)
    result.add_error(err)
    ctx.bind(result)
    ctx.finish("failed")
    return result


_FIX_HINT = (
    "nothing was written; re-read the entities with find or dump, rebuild the plan from the "
    "current expect blocks, and rerun (see edit-report.json for every mismatch)"
)


def _unsupported_hint(problems: list[Problem]) -> str:
    hints = [p.hint for p in problems if p.kind == "unsupported" and p.hint]
    return hints[0] if hints else "nothing was written; see edit-report.json"


# -- verification ---------------------------------------------------------------------------------
def _expected_changes(
    inspections: list[Inspection], outcomes: list[Outcome]
) -> dict[str, dict[str, Any]]:
    """What the diff must show for the applied edits, keyed by the handle the diff reports."""
    expected: dict[str, dict[str, Any]] = {}
    for item, out in zip(inspections, outcomes, strict=True):
        if out.status != "applied":
            continue
        op = item.edit.op
        if op == "clone":
            key = edit_dxf.norm_handle(out.new_handle or "0")
            kinds = {"added"}
        else:
            key = item.diff_handle
            attribute = item.target_type == "ATTRIB"
            if op == "delete":
                kinds = {"changed"} if attribute else {"removed"}
            elif op == "move":
                kinds = {"moved", "changed"} if attribute else {"moved"}
            else:
                kinds = {"changed"}
        slot = expected.setdefault(
            key, {"kinds": set(), "type": item.entity_type, "scope": item.scope, "ids": []}
        )
        slot["kinds"] |= kinds
        slot["ids"].append(item.edit.id)
    for slot in expected.values():
        slot["matched"] = False
        slot["pair"] = False
        if len(slot["ids"]) > 1:
            if "removed" in slot["kinds"]:
                slot["kinds"] = {"removed"}
            elif "added" not in slot["kinds"]:
                # an entity changed in several ways at once (say moved and re-layered) can no
                # longer be paired by the diff: it then shows as one removal plus one addition
                slot["kinds"] |= {"changed", "moved", "removed"}
                slot["pair"] = True
    return expected


def _compact(change: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "kind",
        "type",
        "space",
        "scope",
        "layer",
        "handle",
        "handle_b",
        "vector",
        "description",
    )
    return {k: change[k] for k in keys if k in change}


_FIELD_TEXT = ("text", "attribs")
_FIELD_SIDE_EFFECTS = ("anchor", "bbox")


def _field_update(change: dict[str, Any]) -> dict[str, Any] | None:
    """The report record when ``change`` is only CAD re-evaluating a field, else ``None``.

    Text of a field entity differs and nothing else but the geometry that follows from new text.
    """
    if change["kind"] != "changed" or not change.get("field"):
        return None
    fields = {c["field"]: c for c in change["changes"]}
    shown = next((fields[k] for k in _FIELD_TEXT if k in fields), None)
    if shown is None or not set(fields) <= {*_FIELD_TEXT, *_FIELD_SIDE_EFFECTS}:
        return None
    return {
        "handle": change["handle"],
        "scope": change["scope"],
        "layer": change["layer"],
        "old": shown["old"],
        "new": shown["new"],
    }


def _verify(
    base: Loaded,
    edited: Loaded,
    inspections: list[Inspection],
    outcomes: list[Outcome],
    ctx: RunContext,
    deadline: Deadline,
) -> dict[str, Any]:
    """Diff the edited file against the original and compare with the plan.

    Every change the diff shows must belong to an applied edit (matched by handle, then by type
    and scope when duplicates make handles ambiguous); every applied edit is also read back from
    the edited file and compared with the state pass 1 predicted.
    """
    fp_a = build_fingerprint(base, ctx, deadline)
    fp_b = build_fingerprint(edited, ctx, deadline)
    diff = diffing.diff_fingerprints(fp_a, fp_b, full=True)
    expected = _expected_changes(inspections, outcomes)
    leftover: list[dict[str, Any]] = []
    for change in diff["changes"]:
        slot = expected.get(edit_dxf.norm_handle(change["handle"]))
        if slot is not None and not slot["matched"] and change["kind"] in slot["kinds"]:
            slot["matched"] = True
            if change["kind"] == "removed" and slot["pair"]:
                slot["removal"] = change
        else:
            leftover.append(change)
    for change in list(leftover):
        for slot in expected.values():
            if (
                not slot["matched"]
                and change["kind"] in slot["kinds"]
                and change["type"] == slot["type"]
                and change["scope"] == slot["scope"]
            ):
                slot["matched"] = True
                leftover.remove(change)
                break
    for slot in expected.values():  # the addition that belongs to a paired removal
        removal = slot.get("removal")
        if removal is None:
            continue
        twin = next(
            (
                c
                for c in leftover
                if c["kind"] == "added"
                and c["type"] == slot["type"]
                and c["scope"] == slot["scope"]
            ),
            None,
        )
        if twin is not None:
            leftover.remove(twin)
        else:
            leftover.append(removal)
    field_updates: list[dict[str, Any]] = []
    for change in list(leftover):
        update = _field_update(change)
        if update is not None:
            field_updates.append(update)
            leftover.remove(change)
    unintended = [_compact(c) for c in leftover]
    unintended += [{**s, "kind": f"structural {s['kind']}"} for s in diff["structural"]]
    unobserved = [i for slot in expected.values() if not slot["matched"] for i in slot["ids"]]
    not_as_planned = _read_back(edited, inspections, outcomes)
    return {
        "counts": diff["counts"],
        "noise": diff["noise"],
        "unintended": unintended,
        "field_updates": field_updates,
        "unobserved": unobserved,
        "not_as_planned": not_as_planned,
        "verified": not unintended and not not_as_planned,
    }


def _read_back(
    edited: Loaded, inspections: list[Inspection], outcomes: list[Outcome]
) -> list[dict[str, Any]]:
    """Applied edits whose result, read from the edited file, differs from the prediction."""
    index = edit_dxf.build_index(edited.doc)
    per_handle = Counter(
        edit_dxf.norm_handle(i.edit.handle) for i in inspections if i.edit.op != "clone"
    )
    bad: list[dict[str, Any]] = []
    for item, out in zip(inspections, outcomes, strict=True):
        if out.status != "applied":
            continue
        op = item.edit.op
        if op == "clone":
            loc = index.get(edit_dxf.norm_handle(out.new_handle or "0"))
            handle = out.new_handle
        else:
            handle = item.edit.handle
            if per_handle[edit_dxf.norm_handle(handle)] > 1 and op != "delete":
                continue  # several edits on one entity: only the diff can judge the sum
            loc = index.get(edit_dxf.norm_handle(handle))
        if op == "delete":
            if loc is not None:
                bad.append(
                    {"edit": item.edit.id, "problem": "entity still present", "handle": handle}
                )
            continue
        if loc is None:
            bad.append(
                {
                    "edit": item.edit.id,
                    "problem": "entity missing in the edited file",
                    "handle": handle,
                }
            )
            continue
        want = item.after
        actual = edit_dxf.read_state(loc.entity, item)
        if not edit_dxf.states_match(item, want, actual):
            bad.append(
                {
                    "edit": item.edit.id,
                    "problem": "state differs from the plan",
                    "handle": handle,
                    "planned": want,
                    "found": actual,
                }
            )
    return bad


# -- command --------------------------------------------------------------------------------------
def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spec", type=Path, required=True, help="edit plan (JSON, version 1)")
    parser.add_argument("--dry-run", action="store_true", help="check the plan, change nothing")
    parser.add_argument("--out", help="also deliver the edited file here (atomic copy)")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing --out file")
    parser.add_argument(
        "--allow-com",
        action="store_true",
        help="allow starting a CAD application (COM); needed to edit a DWG; ask the user first",
    )
    _add_common(parser, conversion=False)


def _run(args: argparse.Namespace) -> Result:
    ctx = _new_run("edit", args)  # the run directory exists before anything else happens
    spec_path = Path(args.spec)
    spec = load_spec(spec_path)
    src = _resolve_base(spec_path, spec["base"]["path"])
    suffix = src.suffix.lower()
    if suffix not in (".dxf", ".dwg"):
        raise CadError("UNSUPPORTED", f"cannot edit a {src.suffix!r} file (expected .dxf or .dwg)")
    if suffix == ".dwg" and not args.allow_com:
        raise CadError(
            "NO_BACKEND",
            "editing a DWG needs the CAD application (COM), which starts an instance",
            hint="ask the user, then rerun with --allow-com",
        )
    digest = file_sha1(src)
    if digest != spec["base"]["sha1"]:
        raise CadError(
            "BASE_CHANGED",
            f"{src.name} changed since the plan was made (sha1 {digest[:10]}..., plan "
            f"{spec['base']['sha1'][:10]}...)",
            hint="handles are valid only for the exact file they were read from: re-read it "
            "with find or dump and rebuild the plan",
        )
    out = _plan_output(args, spec, src)
    result = Result(command="edit")
    com = _ComHolder(args.timeout, ctx) if suffix == ".dwg" else None
    try:
        return _execute(args, ctx, spec, src, digest, out, com, result)
    finally:
        if com is not None:
            com.close(result)


def _execute(
    args: argparse.Namespace,
    ctx: RunContext,
    spec: dict[str, Any],
    src: Path,
    digest: str,
    out: Path | None,
    com: _ComHolder | None,
    result: Result,
) -> Result:
    deadline = Deadline(args.timeout)
    backend_name = "com" if com is not None else "ezdxf"
    result.backend = backend_name
    result.approximate = False
    report = _base_report(spec, src, digest, backend_name, args)
    edits = [PlannedEdit.from_json(e) for e in spec["edits"]]

    # -- pass 1: nothing is written
    baseline = _load_baseline(src, ctx, com)
    for warning in baseline.warnings:
        result.warn(warning)
    index = edit_dxf.build_index(baseline.doc)
    inspections, problems = edit_dxf.inspect_plan(baseline.doc, index, edits)
    if com is not None:
        from . import acad_edit

        problems += acad_edit.unsupported_problems(inspections)
    if problems:
        return _refuse(result, ctx, report, problems)
    if any(i.edit.op == "clone" for i in inspections):
        result.warn(
            "clone is not idempotent: running the plan again on the edited file adds another copy"
        )
    if args.dry_run:
        return _finish_dry_run(result, ctx, report, inspections)

    # -- pass 2: apply to a working copy, save, verify
    outcomes: list[Outcome]
    try:
        edited, outcomes, target = _apply(args, ctx, src, com, inspections)
    except ApplyError as exc:
        return _failed(result, ctx, report, exc)
    verification = _verify(baseline, edited, inspections, outcomes, ctx, deadline)
    report["verification"] = verification
    report["edits"] = [o.as_dict() for o in outcomes]
    for warning in edited.warnings:
        result.warn(warning)
    ctx.add_output(result, "changes", _write_changes(ctx, outcomes))
    ctx.add_output(result, "edited", target, source=src)
    return _finish(result, ctx, report, outcomes, verification, target, out)


def _apply(
    args: argparse.Namespace,
    ctx: RunContext,
    src: Path,
    com: _ComHolder | None,
    inspections: list[Inspection],
) -> tuple[Loaded, list[Outcome], Path]:
    """Run the edits on a working copy; the edited file as a loaded document and its path."""
    staged = runs.stage_copy(src, ctx, with_dependencies=False)
    warnings: list[str] = []
    if com is None:
        backend = edit_dxf.DxfBackend(_read_dxf(staged, warnings))
        outcomes = backend.apply(inspections, ctx.log)
        target = ctx.path(f"{src.stem}_edited.dxf")
        backend.save(target)
        loaded = open_drawing(target, ctx)
        loaded.warnings = warnings + loaded.warnings
        return loaded, outcomes, target
    from . import acad_edit

    session = com.get()
    com_backend = acad_edit.ComBackend(session, staged)
    warnings.append(
        f"the edited DWG is saved as version {com_backend.version}"
        + ("" if com_backend.version_known else " (the source version was not recognised)")
    )
    outcomes = com_backend.apply(inspections, ctx.log)
    target = ctx.path(f"{src.stem}_edited.dwg")
    com_backend.save(target)
    exported = ctx.path(f"verify/{src.stem}_edited.dxf")
    warnings += list(session.export_dxf(target, exported) or [])
    doc = _read_dxf(exported, warnings)
    return Loaded(doc, target, exported, warnings, False, "com"), outcomes, target


def _finish_dry_run(
    result: Result, ctx: RunContext, report: dict[str, Any], inspections: list[Inspection]
) -> Result:
    outcomes = [
        Outcome(
            i.edit.id,
            i.edit.op,
            i.edit.handle,
            "already_applied" if i.status == "already_applied" else "planned",
            i.before,
            i.after,
        )
        for i in inspections
    ]
    report["edits"] = [o.as_dict() for o in outcomes]
    ctx.add_output(result, "changes", _write_changes(ctx, outcomes))
    ctx.add_output(result, "report", _write_report(ctx, report))
    pending = sum(1 for o in outcomes if o.status == "planned")
    result.summary = {
        "dry_run": True,
        "edits": len(outcomes),
        "would_apply": pending,
        "already_applied": len(outcomes) - pending,
    }
    result.next = ["rerun without --dry-run to apply the plan"]
    ctx.bind(result)
    ctx.finish()
    return result


def _failed(result: Result, ctx: RunContext, report: dict[str, Any], exc: ApplyError) -> Result:
    """An edit failed while applying: the working copy is discarded, nothing is delivered."""
    report["edits"] = [o.as_dict() for o in exc.outcomes]
    report["error"] = str(exc)
    ctx.add_output(result, "changes", _write_changes(ctx, exc.outcomes))
    ctx.add_output(result, "report", _write_report(ctx, report))
    done = sum(1 for o in exc.outcomes if o.status == "applied")
    result.summary = {
        "applied": 0,
        "failed": sum(1 for o in exc.outcomes if o.status == "failed"),
        "discarded": done,
        "edits": len(exc.outcomes),
    }
    hint = "no edited file was saved; the original is untouched. Fix the plan and rerun"
    if exc.code == "COM_ERROR":
        result.add_error(CadError("COM_ERROR", str(exc), hint=hint))
    else:
        result.add_error(CadError("UNEXPECTED", str(exc), hint=hint))
    ctx.bind(result)
    ctx.finish("failed")
    return result


def _finish(
    result: Result,
    ctx: RunContext,
    report: dict[str, Any],
    outcomes: list[Outcome],
    verification: dict[str, Any],
    edited: Path,
    out: Path | None,
) -> Result:
    counts = Counter(o.status for o in outcomes)
    verified = bool(verification["verified"])
    bad = len(verification["unintended"])
    off_plan = len(verification["not_as_planned"])
    summary: dict[str, Any] = {
        "edits": len(outcomes),
        "applied": counts["applied"],
        "already_applied": counts["already_applied"],
        "failed": counts["failed"],
        "verified": verified,
        "unintended": bad,
        "not_as_planned": off_plan,
        "diff": verification["counts"],
    }
    clones = {o.id: o.new_handle for o in outcomes if o.new_handle}
    if clones:
        summary["new_handles"] = dict(list(clones.items())[:10])
    fields = len(verification["field_updates"])
    if fields:
        summary["field_updates"] = fields
        result.warn(
            f"{fields} field(s) were refreshed by the CAD application when saving "
            "(for example FILENAME); not caused by the plan"
        )
    result.summary = summary
    if verification["unobserved"]:
        result.warn(
            f"{len(verification['unobserved'])} applied edit(s) are not visible in the "
            "fingerprint diff (the result was still read back and matches); see edit-report.json"
        )
    if not verified:
        shown = [u["description"] for u in verification["unintended"][:3] if "description" in u]
        result.exit_code = ExitCode.PARTIAL
        result.add_error(
            CadError(
                "UNINTENDED_CHANGE",
                f"the edited file does not match the plan: {bad} unintended change(s), "
                f"{off_plan} edit(s) not as planned" + (f"; first: {shown[0]}" if shown else ""),
                hint="inspect edited file and edit-report.json (verification); do not deliver it",
            )
        )
        result.warn("the edited file was written for inspection but is NOT verified")
        if out is not None:
            result.warn(f"--out {out.name} was not written because verification failed")
    elif out is not None:
        _atomic_copy(edited, out)
        ctx.add_output(result, "out", out)
        report["delivered"] = str(out)
    ctx.add_output(result, "report", _write_report(ctx, report))
    result.next = (
        ["inspect the edited file (render it) and fix the plan or the backend"]
        if not verified
        else ["render the edited file to check it visually", HANDLES_NOTE]
    )
    ctx.bind(result)
    ctx.finish()
    return result


COMMANDS = {
    "edit": Command(
        help="apply an edit plan safely (two passes, verified against the original)",
        add_arguments=_add_arguments,
        run=_run,
        epilog=(
            "Exit codes: 0 applied and verified, 2 invalid plan or unsupported edit, "
            "3 DWG without --allow-com, 6 base changed or expect/precondition failed (nothing "
            "written), 7 written but not verified (UNINTENDED_CHANGE)."
        ),
    ),
}
