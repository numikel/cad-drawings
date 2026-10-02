"""COM backend of the edit command: changes a DWG through the CAD application.

Windows only at run time (the module itself imports everywhere). The edits were already checked
in pass 1 against a DXF export of the original; this backend only carries them out on a staged
copy, addressing every target by ``HandleToObject`` (never by looping over entities), and saves
a new DWG with ``AcadSession.save_dwg``. The original file is never opened.

Operations: ``replace-text``, ``set-props``, ``delete``, ``move`` and ``clone`` map to COM
members of the entity classes. ``pan-viewport`` is ``UNSUPPORTED`` here: panning the model view
of a paper-space viewport needs the viewport to be activated and the view changed through
application commands, which has not been verified on a real CAD; the DXF backend does it.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import acad
from .edit_dxf import (
    ApplyError,
    Inspection,
    Outcome,
    Problem,
    decode_escapes,
    norm_handle,
    text_problem,
)
from .result import CadError

# DXF-style canonical property -> COM member, per entity class where they differ.
COM_PROPS = {
    "layer": "Layer",
    "color": "color",
    "linetype": "Linetype",
    "lineweight": "Lineweight",
    "ltscale": "LinetypeScale",
    "height": "Height",
    "rotation": "Rotation",  # radians in COM
    "style": "StyleName",
    "xscale": "XScaleFactor",
    "yscale": "YScaleFactor",
    "zscale": "ZScaleFactor",
    "radius": "Radius",
}
TEXT_WIDTH_MEMBER = {"TEXT": "ScaleFactor", "ATTRIB": "ScaleFactor", "ATTDEF": "ScaleFactor"}
MTEXT_WIDTH_MEMBER = "Width"

# DXF entity type -> COM ObjectName (checked before anything is changed when known)
OBJECT_NAMES = {
    "TEXT": {"AcDbText"},
    "MTEXT": {"AcDbMText"},
    "ATTRIB": {"AcDbAttribute"},
    "ATTDEF": {"AcDbAttributeDefinition"},
    "INSERT": {"AcDbBlockReference", "AcDbMInsertBlock"},
    "LINE": {"AcDbLine"},
    "CIRCLE": {"AcDbCircle"},
    "ARC": {"AcDbArc"},
    "LWPOLYLINE": {"AcDbPolyline"},
    "VIEWPORT": {"AcDbViewport"},
}
DWG_HEADERS = {
    b"AC1015": "2000",
    b"AC1018": "2004",
    b"AC1021": "2007",
    b"AC1024": "2010",
    b"AC1027": "2013",
    b"AC1032": "2018",
}
PAN_HINT = (
    "pan-viewport works on DXF sources; for a DWG ask the user to pan the viewport in the CAD "
    "application, or export a DXF, edit that and let the user convert it back"
)


def dwg_save_version(path: Path) -> tuple[str, bool]:
    """``(SaveAs version, known)``: the version of the source so a save never upgrades it."""
    try:
        with Path(path).open("rb") as fh:
            header = fh.read(6)
    except OSError:
        return "2013", False
    version = DWG_HEADERS.get(header)
    return (version, True) if version else ("2013", False)


def start_session(timeout: float, log: Callable[[str], None]) -> Any:
    """A dedicated CAD instance (its PID recorded in the run); the caller quits it."""
    from . import runs

    session = acad.AcadSession.start(timeout=timeout, op_timeout=timeout, log=log)
    runs.note_child(int(session.pid or 0), str(getattr(session, "image", "cad")))
    return session


def unsupported_problems(inspections: list[Inspection]) -> list[Problem]:
    """Edits this backend cannot carry out, found before anything starts (pass 1)."""
    out: list[Problem] = []
    for item in inspections:
        edit = item.edit
        if edit.op == "pan-viewport":
            out.append(
                Problem(
                    edit.id,
                    "unsupported",
                    "pan-viewport is not supported through COM",
                    hint=PAN_HINT,
                )
            )
        elif edit.op == "clone" and _other_space(edit.args.get("target_space"), item.space):
            out.append(
                Problem(
                    edit.id,
                    "unsupported",
                    "clone into another space is not supported through COM",
                    hint="clone within the same space, or edit a DXF",
                )
            )
    return out


def _other_space(target: Any, space: str) -> bool:
    return target is not None and str(target).casefold() != space.casefold()


def _point(values: list[float]) -> Any:
    return acad._double_array(tuple(float(v) for v in values))


def com_value(name: str, value: Any) -> Any:
    """A canonical property value in COM units (rotation in radians)."""
    return math.radians(float(value)) if name == "rotation" else value


def from_com_value(name: str, value: Any) -> Any:
    return math.degrees(float(value)) if name == "rotation" else value


def com_member(kind: str, name: str) -> str:
    if name == "width":
        return MTEXT_WIDTH_MEMBER if kind == "MTEXT" else TEXT_WIDTH_MEMBER.get(kind, "Width")
    return COM_PROPS[name]


class ComBackend:
    """Applies inspected edits to a document opened in a CAD session (a staged copy)."""

    name = "com"

    def __init__(self, session: Any, staged: Path) -> None:
        self.session = session
        self.staged = Path(staged)
        self.doc: Any = None
        self.version, self.version_known = dwg_save_version(self.staged)

    # -- low-level helpers (every COM call goes through retry_expr)
    def _get(self, obj: Any, member: str) -> Any:
        return acad.retry_expr(lambda: getattr(obj, member))

    def _set(self, obj: Any, member: str, value: Any) -> None:
        acad.retry_expr(lambda: setattr(obj, member, value))

    def _object(self, handle: str) -> Any:
        return acad.retry_expr(lambda: self.doc.raw.HandleToObject(handle))

    def _discard(self) -> None:
        doc, self.doc = self.doc, None
        if doc is not None:
            with contextlib.suppress(CadError):  # the session's own quit reports an unclean close
                doc.close()

    # -- pass 2
    def apply(self, items: list[Inspection], log: Callable[[str], None]) -> list[Outcome]:
        """Open the staged copy, check every target, then apply; all or nothing."""
        self.doc = self.session.open(self.staged, readonly=False)
        outcomes: list[Outcome] = []
        try:
            targets = self._resolve(items)
            for item in items:
                outcomes.append(self._apply_one(item, targets, log))
        except ApplyError:
            self._discard()
            raise
        except (CadError, AttributeError, TypeError, ValueError, KeyError) as exc:
            self._discard()
            done = {o.id for o in outcomes}
            rest = [
                Outcome(i.edit.id, i.edit.op, i.edit.handle, "skipped")
                for i in items
                if i.edit.id not in done
            ]
            if rest:
                rest[0].status = "failed"
                rest[0].error = str(exc)
            raise ApplyError(f"CAD refused an edit: {exc}", outcomes + rest, "COM_ERROR") from exc
        return outcomes

    def _resolve(self, items: list[Inspection]) -> dict[str, Any]:
        """Every target by handle, with its COM class checked, before the first change."""
        targets: dict[str, Any] = {}
        for item in items:
            if item.status == "already_applied":
                continue
            key = norm_handle(item.edit.handle)
            if key in targets:
                continue
            obj = self._object(item.edit.handle)
            wanted = OBJECT_NAMES.get(item.target_type)
            have = str(self._get(obj, "ObjectName"))
            if wanted is not None and have not in wanted:
                raise ApplyError(
                    f"edit {item.edit.id!r}: handle {item.edit.handle} is {have} in the CAD "
                    f"application, the plan expects {item.target_type}",
                    [Outcome(item.edit.id, item.edit.op, item.edit.handle, "failed", error=have)],
                    "COM_ERROR",
                )
            targets[key] = obj
        return targets

    def _apply_one(
        self, item: Inspection, targets: dict[str, Any], log: Callable[[str], None]
    ) -> Outcome:
        edit = item.edit
        if item.status == "already_applied":
            return Outcome(
                edit.id, edit.op, edit.handle, "already_applied", item.before, item.before
            )
        obj = targets[norm_handle(edit.handle)]
        handler = {
            "replace-text": self._replace_text,
            "set-props": self._set_props,
            "delete": self._delete,
            "move": self._move,
            "clone": self._clone,
        }.get(edit.op)
        if handler is None:
            raise CadError("UNSUPPORTED", f"{edit.op} is not supported through COM")
        outcome: Outcome = handler(item, obj)
        log(f"applied {edit.id} ({edit.op} on {edit.handle}) through COM")
        return outcome

    # -- operations
    def _replace_text(self, item: Inspection, obj: Any) -> Outcome:
        args = item.edit.args
        current = str(self._get(obj, "TextString"))
        old, new = args["old"], args["new"]
        if text_problem(current, None, args) is not None:
            # COM returns decoded characters where the DXF stores \U+XXXX escapes
            old, new = decode_escapes(old), decode_escapes(new)
            why = text_problem(current, None, {**args, "old": old})
            if why is not None:
                raise ValueError(f"{why} (CAD text: {current!r})")
        updated = current.replace(old, new)
        self._set(obj, "TextString", updated)
        return Outcome(
            item.edit.id,
            item.edit.op,
            item.edit.handle,
            "applied",
            {"text": current},
            {"text": str(self._get(obj, "TextString"))},
        )

    def _set_props(self, item: Inspection, obj: Any) -> Outcome:
        kind = item.target_type
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for name, value in item.plan["props"].items():
            member = com_member(kind, name)
            before[name] = from_com_value(name, self._get(obj, member))
            self._set(obj, member, com_value(name, value))
            after[name] = from_com_value(name, self._get(obj, member))
        return Outcome(item.edit.id, item.edit.op, item.edit.handle, "applied", before, after)

    def _delete(self, item: Inspection, obj: Any) -> Outcome:
        acad.retry_expr(lambda: obj.Delete())
        return Outcome(item.edit.id, item.edit.op, item.edit.handle, "applied", item.before, None)

    def _move(self, item: Inspection, obj: Any) -> Outcome:
        vec = item.plan["vector"]
        acad.retry_expr(lambda: obj.Move(_point([0, 0, 0]), _point(vec)))
        return Outcome(
            item.edit.id, item.edit.op, item.edit.handle, "applied", item.before, item.after
        )

    def _clone(self, item: Inspection, obj: Any) -> Outcome:
        plan = item.plan
        copy = acad.retry_expr(lambda: obj.Copy())
        acad.retry_expr(lambda: copy.Move(_point([0, 0, 0]), _point(plan["vector"])))
        if plan.get("target_layer"):
            self._set(copy, "Layer", plan["target_layer"])
        new_handle = str(self._get(copy, "Handle"))
        return Outcome(
            item.edit.id,
            item.edit.op,
            item.edit.handle,
            "applied",
            None,
            {"handle": new_handle, **(item.after or {})},
            new_handle=new_handle,
        )

    # -- output
    def save(self, dst: Path) -> None:
        """SaveAs a DWG (same version as the source) and close the document."""
        if self.doc is None:
            raise CadError("UNEXPECTED", "save called before apply")
        doc, self.doc = self.doc, None
        self.session.save_dwg(doc, Path(dst), self.version)
