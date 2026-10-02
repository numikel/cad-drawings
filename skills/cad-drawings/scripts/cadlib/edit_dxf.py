"""ezdxf backend of the edit command, and the shared model of an edit.

Pass 1 (``inspect_plan``) reads the unchanged drawing and decides, for every edit, whether the
target exists, whether its ``expect`` block holds, whether the arguments make sense for that
entity and whether the edit is already in the requested state. It never changes anything and
reports every problem at once. Pass 2 (``DxfBackend.apply``) changes a working copy. The COM
backend (``acad_edit``) reuses ``PlannedEdit``, ``Inspection``, ``Outcome`` and the helpers
here; it only differs in how a change is carried out.

Rules that hold for both backends:

* ``replace-text`` works on the raw string (MTEXT formatting codes included). ``old`` must occur
  exactly once, or exactly ``count`` times when ``count`` is given.
* ``set-props`` takes DXF-style names (``layer``, ``color``, ``height``, ``rotation``, ``style``,
  ``width``, ``linetype``, ``lineweight``, ``ltscale``, ``xscale``/``yscale``/``zscale``,
  ``radius``); a name the entity type does not support is ``UNSUPPORTED``.
* ``pan-viewport`` takes the desired model-space centre; the stored centre is in display
  coordinates, ``DCS = R(+twist) * (WCS - target)``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ezdxf
from ezdxf.lldxf import const as dxf_const

from .entities import Loc, anchor_of, entity_text, expect_for, iter_locations
from .util import _pt, atomic_write, r4
from .viewports import _viewport_info

if TYPE_CHECKING:
    from ezdxf.document import Drawing

# problem kinds -> error code (priority order: the first kind present decides the exit code)
KIND_CODES = {
    "unsupported": "UNSUPPORTED",
    "handle_not_found": "EXPECT_FAILED",
    "expect": "EXPECT_FAILED",
    "text_match": "EXPECT_FAILED",
    "precondition": "PRECONDITION_FAILED",
}
TEXT_KINDS = frozenset({"TEXT", "ATTRIB", "ATTDEF", "MTEXT"})
ATTRIB_KINDS = frozenset({"ATTRIB", "ATTDEF"})
NOT_CLONABLE = frozenset({"ATTRIB", "ATTDEF", "VIEWPORT"})
DEFAULT_TOLERANCE = 1e-6
PAN_TOLERANCE = 1e-4

COMMON_PROPS = ("layer", "color", "linetype", "lineweight", "ltscale")
TEXT_PROPS = ("height", "rotation", "style", "width")
PROPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "TEXT": TEXT_PROPS,
    "ATTRIB": TEXT_PROPS,
    "ATTDEF": TEXT_PROPS,
    "MTEXT": TEXT_PROPS,
    "INSERT": ("rotation", "xscale", "yscale", "zscale"),
    "CIRCLE": ("radius",),
    "ARC": ("radius",),
}
PROP_ALIASES = {
    "char_height": "height",
    "text_height": "height",
    "width_factor": "width",
    "linetype_scale": "ltscale",
}


# -- model ----------------------------------------------------------------------------------------
def norm_handle(handle: str) -> str:
    """Handles compare as numbers: ``0x2f``-style case and leading zeros do not matter."""
    return f"{int(handle, 16):X}"


@dataclass
class PlannedEdit:
    id: str
    op: str
    handle: str
    expect: dict[str, Any]
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> PlannedEdit:
        return cls(item["id"], item["op"], item["handle"], item["expect"], item.get("args", {}))


@dataclass
class Problem:
    edit_id: str | None
    kind: str
    message: str
    field: str | None = None
    expected: Any = None
    actual: Any = None
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"edit": self.edit_id, "kind": self.kind, "message": self.message}
        for key in ("field", "expected", "actual", "hint"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class Inspection:
    """Result of pass 1 for one edit: what it will do and what the target looks like now."""

    edit: PlannedEdit
    status: str  # "pending" | "already_applied"
    entity_type: str  # type as the fingerprint knows it
    target_type: str  # type of the edited entity itself (ATTRIB for an attribute)
    diff_handle: str  # handle of the entity as the fingerprint knows it (INSERT for an ATTRIB)
    space: str  # expect-style: model | layout name | block:<name>
    scope: str  # fingerprint scope: Model, layout name or block name
    layer: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    plan: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    id: str
    op: str
    handle: str
    status: str  # applied | already_applied | failed | skipped | planned
    before: Any = None
    after: Any = None
    new_handle: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "op": self.op,
            "handle": self.handle,
            "before": self.before,
            "after": self.after,
            "status": self.status,
        }
        if self.new_handle is not None:
            out["new_handle"] = self.new_handle
        if self.error is not None:
            out["error"] = self.error
        return out


class ApplyError(Exception):
    """Pass 2 failed: nothing is saved; ``outcomes`` says how far it got."""

    def __init__(self, message: str, outcomes: list[Outcome], code: str = "UNEXPECTED") -> None:
        super().__init__(message)
        self.outcomes = outcomes
        self.code = code


# -- index ----------------------------------------------------------------------------------------
def space_name(loc: Loc) -> str:
    if loc.space == "model":
        return "model"
    if loc.space == "paper":
        return loc.layout or ""
    return f"block:{loc.block}"


def scope_name(loc: Loc) -> str:
    return (loc.block or "") if loc.space == "block" else (loc.layout or "")


def build_index(doc: Drawing) -> dict[str, Loc]:
    """Every entity of every space and block definition (ATTRIBs too) by normalised handle."""
    return {norm_handle(str(loc.entity.dxf.handle)): loc for loc in iter_locations(doc)}


def raw_text_of(entity: Any) -> str | None:
    return entity_text(entity)[1] if entity.dxftype() in TEXT_KINDS else None


def insert_of(entity: Any) -> list[float] | None:
    d = entity.dxf
    if entity.dxftype() == "VIEWPORT":
        return _pt(d.center, 3)
    if d.hasattr("insert"):
        return _pt(d.insert, 3)
    anchor = anchor_of(entity)
    return [anchor[0], anchor[1], 0.0] if anchor else None


# -- expect ---------------------------------------------------------------------------------------
def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _point_matches(given: list[float], actual: list[float], tol: float) -> bool:
    """Compare the given components; ``find`` rounds to 4 decimals, so a rounded actual passes."""
    for i, value in enumerate(given):
        if not (_close(value, actual[i], tol) or _close(value, r4(actual[i]), tol)):
            return False
    return True


def check_expect(
    loc: Loc, expect: dict[str, Any], skip: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Mismatches between an ``expect`` block and the entity as found (empty: it holds).

    Accepts exactly the shape ``find``/``dump`` emit (``entities.expect_for``). ``skip`` names
    fields an already-applied edit has legitimately changed.
    """
    entity = loc.entity
    found: dict[str, Any] = expect_for(loc) or {}
    plain, raw = entity_text(entity)
    out: list[dict[str, Any]] = []

    def bad(name: str, want: Any, got: Any) -> None:
        out.append({"field": name, "expected": want, "actual": got})

    if str(expect["type"]).upper() != found["type"].upper():
        bad("type", expect["type"], found["type"])
    for key in ("layer", "space"):
        wanted = expect.get(key)
        if wanted is None or key in skip:
            continue
        if str(wanted).casefold() != str(found[key]).casefold():
            bad(key, wanted, found[key])
    if "text" in expect and "text" not in skip:
        current = plain if expect.get("text_is_plain") else raw
        if current != expect["text"]:
            bad("text", expect["text"], current)
    if "attrib" in expect and "attrib" not in skip:
        current_attribs: dict[str, str] | None = found.get("attrib")
        for tag, value in expect["attrib"].items():
            have = None if current_attribs is None else current_attribs.get(tag)
            if have != value:
                bad(f"attrib.{tag}", value, have)
    if "insert" in expect and "insert" not in skip:
        tol = float(expect.get("tolerance", DEFAULT_TOLERANCE))
        have_point = insert_of(entity) if entity.dxf.hasattr("insert") else None
        if have_point is None or not _point_matches(expect["insert"], have_point, tol):
            bad("insert", expect["insert"], have_point)
    return out


# -- properties -----------------------------------------------------------------------------------
def canonical_prop(name: str) -> str:
    low = name.strip().lower()
    return PROP_ALIASES.get(low, low)


def supported_props(kind: str) -> tuple[str, ...]:
    return (*COMMON_PROPS, *PROPS_BY_TYPE.get(kind, ()))


def read_prop(entity: Any, name: str) -> Any:
    kind = entity.dxftype()
    d = entity.dxf
    if name == "layer":
        return str(d.layer)
    if name == "color":
        return int(d.get("color", 256))
    if name == "linetype":
        return str(d.get("linetype", "BYLAYER"))
    if name == "lineweight":
        return int(d.get("lineweight", -1))
    if name == "ltscale":
        return float(d.get("ltscale", 1.0))
    if name == "height":
        return float(d.get("char_height" if kind == "MTEXT" else "height", 0.0))
    if name == "rotation":
        return float(entity.get_rotation()) if kind == "MTEXT" else float(d.get("rotation", 0.0))
    if name == "style":
        return str(d.get("style", "Standard"))
    if name == "width":
        return float(d.get("width", 0.0 if kind == "MTEXT" else 1.0))
    if name in ("xscale", "yscale", "zscale"):
        return float(d.get(name, 1.0))
    if name == "radius":
        return float(d.radius)
    raise KeyError(name)


def _write_prop(entity: Any, name: str, value: Any) -> None:
    kind = entity.dxftype()
    if name == "rotation" and kind == "MTEXT":
        entity.set_rotation(value)
    elif name == "height" and kind == "MTEXT":
        entity.dxf.char_height = value
    else:
        entity.dxf.set(name, value)


def props_equal(name: str, a: Any, b: Any) -> bool:
    if name in ("layer", "linetype", "style"):
        return str(a).casefold() == str(b).casefold()
    if name in ("color", "lineweight"):
        return int(a) == int(b)
    if name == "rotation":
        delta = (float(a) - float(b)) % 360.0
        return min(delta, 360.0 - delta) <= DEFAULT_TOLERANCE
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=DEFAULT_TOLERANCE)


def _normalise_prop_value(doc: Drawing, name: str, value: Any) -> tuple[Any, str | None]:
    """The value as stored, or ``(None, reason)`` when it is not acceptable."""
    if name in ("layer", "linetype", "style"):
        if not isinstance(value, str) or not value:
            return None, f"{name} must be a non-empty string"
        if name == "layer" and not doc.layers.has_entry(value):
            return None, f"layer {value!r} does not exist in the drawing"
        if name == "style" and not doc.styles.has_entry(value):
            return None, f"text style {value!r} does not exist in the drawing"
        if (
            name == "linetype"
            and value.upper() not in ("BYLAYER", "BYBLOCK")
            and not doc.linetypes.has_entry(value)
        ):
            return None, f"linetype {value!r} does not exist in the drawing"
        return (
            value.upper()
            if name == "linetype" and value.upper() in ("BYLAYER", "BYBLOCK")
            else value
        ), None
    if name == "color":
        if isinstance(value, str) and value.upper() in ("BYLAYER", "BYBLOCK"):
            return (256 if value.upper() == "BYLAYER" else 0), None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 256:
            return None, "color must be an ACI number 0-256, BYLAYER or BYBLOCK"
        return value, None
    if name == "lineweight":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "lineweight must be an integer in hundredths of a millimetre"
        if value not in dxf_const.VALID_DXF_LINEWEIGHTS and value not in (-1, -2, -3):
            return None, f"{value} is not a valid lineweight"
        return value, None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None, f"{name} must be a finite number"
    if name != "rotation" and not name.endswith("scale") and value <= 0:
        return None, f"{name} must be greater than zero"
    if name.endswith("scale") and value == 0:
        return None, f"{name} must not be zero"
    return float(value), None


# -- viewports ------------------------------------------------------------------------------------
def dcs_center(
    twist_deg: float, target: tuple[float, float], wcs: tuple[float, float]
) -> tuple[float, float]:
    """Stored view centre for a desired model point: ``DCS = R(+twist) * (WCS - target)``."""
    a = math.radians(twist_deg)
    ca, sa = math.cos(a), math.sin(a)
    px, py = wcs[0] - target[0], wcs[1] - target[1]
    return (px * ca - py * sa, px * sa + py * ca)


def view_center_wcs(entity: Any) -> list[float]:
    info = _viewport_info(entity)
    wcs = info.view_center_wcs
    return [r4(wcs[0]), r4(wcs[1])]


# -- text -----------------------------------------------------------------------------------------
_UNICODE_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def decode_escapes(text: str) -> str:
    """Unicode escapes (backslash, U, plus, four hex digits) as the characters they stand for."""
    return _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def count_occurrences(raw: str, old: str) -> int:
    return raw.count(old)


def text_problem(raw: str, plain: str | None, args: dict[str, Any]) -> str | None:
    """Why ``old`` cannot be replaced in ``raw`` (None when it can)."""
    old = args["old"]
    wanted = args.get("count")
    found = count_occurrences(raw, old)
    if wanted is None and found != 1:
        if found == 0:
            if plain is not None and old in plain:
                return (
                    "'old' occurs in the plain text but not in the raw string (formatting codes "
                    "split it); use the raw text from find's 'raw' field"
                )
            return "'old' does not occur in the raw text"
        return f"'old' occurs {found} times; it must occur exactly once (or give 'count')"
    if wanted is not None and found != wanted:
        return f"'old' occurs {found} times but 'count' is {wanted}"
    return None


# -- pass 1 ---------------------------------------------------------------------------------------
def _layout_of(doc: Drawing, loc: Loc) -> Any:
    if loc.space == "model":
        return doc.modelspace()
    if loc.space == "paper":
        return doc.layouts.get(loc.layout)
    return doc.blocks.get(loc.block or "")


def resolve_layout(doc: Drawing, name: str) -> Any | None:
    """Model space, a paper layout or ``block:<name>`` by (case-insensitive) name."""
    low = name.casefold()
    if low == "model":
        return doc.modelspace()
    if low.startswith("block:"):
        wanted = name[6:]
        for blk in doc.blocks:
            if blk.name.casefold() == wanted.casefold():
                return blk
        return None
    for layout_name in doc.layouts.names():
        if layout_name.casefold() == low:
            return doc.layouts.get(layout_name)
    return None


def _can_translate(entity: Any) -> bool:
    try:
        probe = entity.copy()
        probe.translate(0.0, 0.0, 0.0)
    except (NotImplementedError, TypeError, AttributeError, ezdxf.DXFError):
        return False
    return True


def _pad3(vector: list[float]) -> list[float]:
    return [float(v) for v in vector] + [0.0] * (3 - len(vector))


class _Plan:
    """Collects problems of one edit."""

    def __init__(self, edit: PlannedEdit) -> None:
        self.edit = edit
        self.problems: list[Problem] = []

    def add(self, kind: str, message: str, **extra: Any) -> None:
        self.problems.append(Problem(self.edit.id, kind, message, **extra))


def _already_applied(loc: Loc, edit: PlannedEdit, doc: Drawing) -> bool:
    entity = loc.entity
    args = edit.args
    if edit.op == "replace-text":
        raw = raw_text_of(entity)
        if raw is None or args["old"] == args["new"]:
            return False
        if args["old"] not in raw and args["new"] in raw:
            return True
        want = edit.expect.get("text")
        if want is not None and not edit.expect.get("text_is_plain"):
            return bool(raw == want.replace(args["old"], args["new"]) and raw != want)
        return False
    if edit.op == "set-props":
        kind = entity.dxftype()
        supported = supported_props(kind)
        for name, value in args["props"].items():
            canon = canonical_prop(name)
            if canon not in supported:
                return False
            stored, why = _normalise_prop_value(doc, canon, value)
            if why is not None or not props_equal(canon, read_prop(entity, canon), stored):
                return False
        return True
    if edit.op == "move":
        want = edit.expect.get("insert")
        have = insert_of(entity) if entity.dxf.hasattr("insert") else None
        vec = _pad3(args["vector"])
        if want is None or have is None or not any(vec):
            return False
        tol = float(edit.expect.get("tolerance", DEFAULT_TOLERANCE))
        moved = [want[i] + vec[i] if i < len(want) else None for i in range(3)]
        return _point_matches([m for m in moved if m is not None], have, max(tol, 1e-4)) and not (
            _point_matches(want, have, tol)
        )
    if edit.op == "pan-viewport" and entity.dxftype() == "VIEWPORT":
        desired = args["view_center"]
        have_wcs = _viewport_info(entity).view_center_wcs
        return _close(desired[0], have_wcs[0], PAN_TOLERANCE) and _close(
            desired[1], have_wcs[1], PAN_TOLERANCE
        )
    return False


def _skips(edit: PlannedEdit) -> frozenset[str]:
    """``expect`` fields an already-applied edit has legitimately changed."""
    if edit.op == "replace-text":
        return frozenset({"text"})
    if edit.op == "move":
        return frozenset({"insert"})
    if edit.op == "set-props" and any(canonical_prop(k) == "layer" for k in edit.args["props"]):
        return frozenset({"layer"})
    return frozenset()


def _plan_replace_text(p: _Plan, loc: Loc, inspection: Inspection) -> None:
    entity = loc.entity
    if entity.dxftype() not in TEXT_KINDS:
        p.add(
            "unsupported",
            f"replace-text works on TEXT, MTEXT and ATTRIB, not {entity.dxftype()}",
        )
        return
    plain, raw = entity_text(entity)
    raw = raw or ""
    args = p.edit.args
    inspection.before = {"text": raw}
    inspection.plan = {"old": args["old"], "new": args["new"], "count": args.get("count")}
    if inspection.status == "already_applied":
        inspection.after = {"text": raw}
        return
    why = text_problem(raw, plain, args)
    if why is not None:
        p.add("text_match", why, field="old", expected=args["old"], actual=raw)
        return
    inspection.after = {"text": raw.replace(args["old"], args["new"])}


def _plan_set_props(p: _Plan, loc: Loc, doc: Drawing, inspection: Inspection) -> None:
    entity = loc.entity
    kind = entity.dxftype()
    supported = supported_props(kind)
    stored: dict[str, Any] = {}
    for name, value in p.edit.args["props"].items():
        canon = canonical_prop(name)
        if canon not in supported:
            p.add(
                "unsupported",
                f"property {name!r} is not supported for {kind}",
                hint="supported: " + ", ".join(supported),
            )
            continue
        value_ok, why = _normalise_prop_value(doc, canon, value)
        if why is not None:
            p.add("precondition", f"{name}: {why}", field=name, expected=value)
            continue
        stored[canon] = value_ok
    inspection.before = {name: read_prop(entity, name) for name in stored}
    inspection.after = dict(stored) if inspection.status == "pending" else dict(inspection.before)
    inspection.plan = {"props": stored}


def _shifted(start: list[float] | None, *shifts: list[float]) -> list[float] | None:
    if start is None:
        return None
    return [r4(start[i] + sum(s[i] for s in shifts)) for i in range(3)]


def _plan_move(p: _Plan, loc: Loc, inspection: Inspection, earlier: list[float]) -> None:
    """``earlier``: the sum of the moves earlier edits of this plan make to the same entity."""
    entity = loc.entity
    vec = _pad3(p.edit.args["vector"])
    start = insert_of(entity)
    inspection.plan = {"vector": vec}
    inspection.before = {"anchor": _shifted(start, earlier)}
    if entity.dxftype() != "VIEWPORT" and not _can_translate(entity):
        p.add("unsupported", f"{entity.dxftype()} cannot be moved by ezdxf")
        return
    now = vec if inspection.status == "pending" else [0.0, 0.0, 0.0]
    inspection.after = {"anchor": _shifted(start, earlier, now)}


def _plan_clone(
    p: _Plan, loc: Loc, doc: Drawing, inspection: Inspection, earlier: list[float]
) -> None:
    entity = loc.entity
    args = p.edit.args
    vec = _pad3(args["vector"])
    inspection.plan = {
        "vector": vec,
        "target_layer": args.get("target_layer"),
        "target_space": args.get("target_space"),
    }
    if entity.dxftype() in NOT_CLONABLE:
        p.add("unsupported", f"{entity.dxftype()} cannot be cloned on its own")
        return
    if not _can_translate(entity):
        p.add("unsupported", f"{entity.dxftype()} cannot be copied by ezdxf")
        return
    layer = args.get("target_layer")
    if layer is not None and not doc.layers.has_entry(layer):
        p.add("precondition", f"target_layer {layer!r} does not exist", field="target_layer")
    space = args.get("target_space")
    if space is not None:
        target = resolve_layout(doc, space)
        if target is None:
            p.add("precondition", f"target_space {space!r} does not exist", field="target_space")
        else:
            inspection.scope = _loc_for(target, entity).layout or target.name
            if _space_kind(target) == "block":
                inspection.scope = target.name
    anchor = _shifted(insert_of(entity), earlier, vec)
    inspection.before = None
    inspection.after = {"anchor": anchor} if anchor is not None else None


def _plan_pan(p: _Plan, loc: Loc, inspection: Inspection) -> None:
    entity = loc.entity
    if entity.dxftype() != "VIEWPORT" or loc.space != "paper":
        p.add("unsupported", "pan-viewport needs a VIEWPORT in a paper-space layout")
        return
    info = _viewport_info(entity)
    if info.vp_id == 1:
        p.add("unsupported", "the overall (id 1) viewport has no model view to pan")
        return
    if not info.top_view:
        p.add("unsupported", "pan-viewport supports plan (top) views only")
        return
    desired = p.edit.args["view_center"]
    inspection.before = {"view_center_wcs": view_center_wcs(entity)}
    new_dcs = dcs_center(info.twist, info.target, (float(desired[0]), float(desired[1])))
    inspection.plan = {
        "view_center": [float(desired[0]), float(desired[1])],
        "stored_center": [new_dcs[0], new_dcs[1]],
    }
    if inspection.status == "pending":
        inspection.after = {"view_center_wcs": [r4(desired[0]), r4(desired[1])]}
    else:
        inspection.after = dict(inspection.before)


def inspect_plan(
    doc: Drawing, index: dict[str, Loc], edits: list[PlannedEdit]
) -> tuple[list[Inspection], list[Problem]]:
    """Pass 1: check every edit against the unchanged drawing; collect all problems."""
    inspections: list[Inspection] = []
    problems: list[Problem] = []
    moved: dict[str, list[float]] = {}  # handle -> total move of the edits seen so far
    # Fields of an entity that some already-applied edit of this plan has changed: the expect
    # blocks of every edit on that entity describe the original, so those fields cannot be held
    # against a file that already went through the plan.
    skips: dict[str, frozenset[str]] = {}
    for edit in edits:
        found = index.get(norm_handle(edit.handle))
        if found is not None and edit.op != "delete" and _already_applied(found, edit, doc):
            key = norm_handle(edit.handle)
            skips[key] = skips.get(key, frozenset()) | _skips(edit)
    for edit in edits:
        plan = _Plan(edit)
        loc = index.get(norm_handle(edit.handle))
        if loc is None:
            problems.append(
                Problem(
                    edit.id,
                    "handle_not_found",
                    f"no entity with handle {edit.handle} in the drawing",
                    field="handle",
                    expected=edit.handle,
                    hint="handles are valid only for the file version they were read from",
                )
            )
            continue
        entity = loc.entity
        parent = loc.parent.dxf.handle if loc.parent is not None else entity.dxf.handle
        parent_kind = loc.parent.dxftype() if loc.parent is not None else entity.dxftype()
        applied = edit.op != "delete" and _already_applied(loc, edit, doc)
        inspection = Inspection(
            edit=edit,
            status="already_applied" if applied else "pending",
            entity_type=parent_kind if entity.dxftype() == "ATTRIB" else entity.dxftype(),
            target_type=entity.dxftype(),
            diff_handle=norm_handle(str(parent)),
            space=space_name(loc),
            scope=scope_name(loc),
            layer=str(entity.dxf.layer),
            before=None,
            after=None,
        )
        skip = skips.get(norm_handle(edit.handle), frozenset())
        for mismatch in check_expect(loc, edit.expect, skip):
            plan.add(
                "expect",
                f"{mismatch['field']}: expected {mismatch['expected']!r}, found {mismatch['actual']!r}",
                field=mismatch["field"],
                expected=mismatch["expected"],
                actual=mismatch["actual"],
            )
        if edit.op == "replace-text":
            _plan_replace_text(plan, loc, inspection)
        elif edit.op == "set-props":
            _plan_set_props(plan, loc, doc, inspection)
        elif edit.op == "move":
            earlier = moved.setdefault(norm_handle(edit.handle), [0.0, 0.0, 0.0])
            _plan_move(plan, loc, inspection, list(earlier))
            if inspection.status == "pending":
                vec = _pad3(edit.args["vector"])
                moved[norm_handle(edit.handle)] = [earlier[i] + vec[i] for i in range(3)]
        elif edit.op == "clone":
            earlier = moved.get(norm_handle(edit.handle), [0.0, 0.0, 0.0])
            _plan_clone(plan, loc, doc, inspection, list(earlier))
        elif edit.op == "pan-viewport":
            _plan_pan(plan, loc, inspection)
        else:  # delete
            raw = entity_text(entity)[1]
            inspection.before = {"type": entity.dxftype(), "layer": inspection.layer}
            if raw is not None:
                inspection.before["text"] = raw
        problems += plan.problems
        inspections.append(inspection)
    problems += _delete_conflicts(inspections, index)
    return inspections, problems


def _delete_conflicts(inspections: list[Inspection], index: dict[str, Loc]) -> list[Problem]:
    """An edit may not target an entity (or the owner of an attribute) another edit deletes."""
    deleted = {norm_handle(i.edit.handle) for i in inspections if i.edit.op == "delete"}
    out: list[Problem] = []
    for item in inspections:
        if item.edit.op == "delete":
            continue
        own = norm_handle(item.edit.handle)
        if own in deleted or item.diff_handle in deleted:
            out.append(
                Problem(
                    item.edit.id,
                    "precondition",
                    "the target is deleted by another edit of this plan",
                    hint="drop one of the two edits",
                )
            )
    return out


def read_state(entity: Any, item: Inspection) -> dict[str, Any] | None:
    """The state ``item.after`` describes, read from an entity (a document after the edit)."""
    op = item.edit.op
    if op == "replace-text":
        return {"text": raw_text_of(entity)}
    if op == "set-props":
        return {name: read_prop(entity, name) for name in item.plan["props"]}
    if op in ("move", "clone"):
        anchor = insert_of(entity)
        return {"anchor": [r4(v) for v in anchor] if anchor else None}
    if op == "pan-viewport":
        return {"view_center_wcs": view_center_wcs(entity)}
    return None


def states_match(
    item: Inspection, wanted: dict[str, Any] | None, actual: dict[str, Any] | None
) -> bool:
    """Whether a state read after the edit equals the planned one (tolerant for numbers)."""
    if wanted is None or actual is None:
        return wanted == actual
    for key, want in wanted.items():
        got = actual.get(key)
        if key == "text":
            # one exporter writes a character where another stores a unicode escape
            if want != got and decode_escapes(str(want)) != decode_escapes(str(got or "")):
                return False
        elif key == "anchor":
            if want is None or got is None:
                return bool(want == got)
            if not all(_close(a, b, 1e-3) for a, b in zip(want, got, strict=False)):
                return False
        elif key == "view_center_wcs":
            if not all(_close(a, b, 1e-3) for a, b in zip(want, got, strict=True)):
                return False
        elif not props_equal(key, got, want):
            return False
    return True


# -- pass 2 ---------------------------------------------------------------------------------------
class DxfBackend:
    """Applies inspected edits to an ezdxf document (a working copy, never the original)."""

    name = "ezdxf"

    def __init__(self, doc: Drawing) -> None:
        self.doc = doc
        self.index = build_index(doc)

    # -- helpers
    def _loc(self, handle: str) -> Loc:
        loc = self.index.get(norm_handle(handle))
        if loc is None or not loc.entity.is_alive:
            raise KeyError(f"entity {handle} not found in the working copy")
        return loc

    def apply(self, items: list[Inspection], log: Callable[[str], None]) -> list[Outcome]:
        """Apply every pending edit; all or nothing (on failure the caller discards the copy)."""
        outcomes: list[Outcome] = []
        for item in items:
            edit = item.edit
            if item.status == "already_applied":
                outcomes.append(
                    Outcome(
                        edit.id, edit.op, edit.handle, "already_applied", item.before, item.before
                    )
                )
                continue
            handler = {
                "replace-text": self._replace_text,
                "set-props": self._set_props,
                "delete": self._delete,
                "move": self._move,
                "clone": self._clone,
                "pan-viewport": self._pan,
            }[edit.op]
            try:
                outcome = handler(item)
            except (
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
                NotImplementedError,
                ezdxf.DXFError,
            ) as exc:
                outcomes.append(
                    Outcome(
                        edit.id, edit.op, edit.handle, "failed", item.before, None, error=str(exc)
                    )
                )
                outcomes += [
                    Outcome(i.edit.id, i.edit.op, i.edit.handle, "skipped")
                    for i in items[len(outcomes) :]
                ]
                raise ApplyError(f"edit {edit.id!r} failed: {exc}", outcomes) from exc
            log(f"applied {edit.id} ({edit.op} on {edit.handle})")
            outcomes.append(outcome)
        return outcomes

    # -- operations
    def _replace_text(self, item: Inspection) -> Outcome:
        loc = self._loc(item.edit.handle)
        entity = loc.entity
        plain, raw = entity_text(entity)
        raw = raw or ""
        why = text_problem(raw, plain, item.edit.args)
        if why is not None:
            raise ValueError(why)
        new_raw = raw.replace(item.edit.args["old"], item.edit.args["new"])
        if entity.dxftype() == "MTEXT":
            entity.text = new_raw
        else:
            entity.dxf.text = new_raw
        return Outcome(
            item.edit.id,
            item.edit.op,
            item.edit.handle,
            "applied",
            {"text": raw},
            {"text": new_raw},
        )

    def _set_props(self, item: Inspection) -> Outcome:
        entity = self._loc(item.edit.handle).entity
        before = {name: read_prop(entity, name) for name in item.plan["props"]}
        for name, value in item.plan["props"].items():
            _write_prop(entity, name, value)
        after = {name: read_prop(entity, name) for name in item.plan["props"]}
        return Outcome(item.edit.id, item.edit.op, item.edit.handle, "applied", before, after)

    def _delete(self, item: Inspection) -> Outcome:
        loc = self._loc(item.edit.handle)
        entity = loc.entity
        if entity.dxftype() == "ATTRIB" and loc.parent is not None:
            loc.parent.attribs.remove(entity)
            entity.destroy()
        else:
            _layout_of(self.doc, loc).delete_entity(entity)
        return Outcome(item.edit.id, item.edit.op, item.edit.handle, "applied", item.before, None)

    def _move(self, item: Inspection) -> Outcome:
        entity = self._loc(item.edit.handle).entity
        before = {"anchor": insert_of(entity)}
        dx, dy, dz = item.plan["vector"]
        if entity.dxftype() == "VIEWPORT":
            c = entity.dxf.center
            entity.dxf.center = (c.x + dx, c.y + dy, c.z + dz)
        else:
            entity.translate(dx, dy, dz)
        anchor = insert_of(entity)
        after = {"anchor": [r4(v) for v in anchor] if anchor else None}
        return Outcome(item.edit.id, item.edit.op, item.edit.handle, "applied", before, after)

    def _clone(self, item: Inspection) -> Outcome:
        loc = self._loc(item.edit.handle)
        plan = item.plan
        target = (
            resolve_layout(self.doc, plan["target_space"])
            if plan["target_space"]
            else _layout_of(self.doc, loc)
        )
        if target is None:
            raise KeyError(f"target space {plan['target_space']!r} not found")
        copy = loc.entity.copy()
        target.add_entity(copy)
        copy.translate(*plan["vector"])
        if plan["target_layer"]:
            copy.dxf.layer = plan["target_layer"]
        new_handle = str(copy.dxf.handle)
        self.index[norm_handle(new_handle)] = _loc_for(target, copy)
        anchor = insert_of(copy)
        after = {"handle": new_handle, "anchor": [r4(v) for v in anchor] if anchor else None}
        return Outcome(
            item.edit.id,
            item.edit.op,
            item.edit.handle,
            "applied",
            None,
            after,
            new_handle=new_handle,
        )

    def _pan(self, item: Inspection) -> Outcome:
        entity = self._loc(item.edit.handle).entity
        before = {"view_center_wcs": view_center_wcs(entity)}
        cx, cy = item.plan["stored_center"]
        entity.dxf.view_center_point = (cx, cy, 0.0)
        return Outcome(
            item.edit.id,
            item.edit.op,
            item.edit.handle,
            "applied",
            before,
            {"view_center_wcs": view_center_wcs(entity)},
        )

    # -- output
    def save(self, dst: Path) -> None:
        """Write the working copy to ``dst`` through a temporary sibling (atomic replace)."""
        atomic_write(Path(dst), lambda tmp: self.doc.saveas(tmp))


def _loc_for(layout: Any, entity: Any) -> Loc:
    kind = _space_kind(layout)
    if kind == "model":
        return Loc("model", "Model", None, entity)
    if kind == "paper":
        return Loc("paper", layout.name, None, entity)
    return Loc("block", None, layout.name, entity)


def _space_kind(layout: Any) -> str:
    if getattr(layout, "is_modelspace", False):
        return "model"
    if getattr(layout, "is_any_paperspace", False):
        return "paper"
    return "block"
