"""Layer states, block placements and the ``prints_on`` / ``visible_in_space`` logic."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ezdxf.math import Matrix44, Vec3

if TYPE_CHECKING:
    from ezdxf.document import Drawing

from .entities import Loc, anchor_of, geometry, iter_locations
from .viewports import ViewportInfo, layout_viewports

MAX_BLOCK_PLACEMENTS = 500
MAX_NESTING = 8
_MISSING = object()
Extent = tuple[float, float, float, float]


@dataclass
class Placement:
    layout: str  # layout name (``Model`` for model space)
    matrices: list[Matrix44]
    layers: list[str]  # layers of the INSERTs on the way


@dataclass
class PrintVerdict:
    """Layouts a model-space extent is confirmed on, layouts where it cannot be decided."""

    confirmed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_confirmed(self, name: str) -> None:
        if name not in self.confirmed:
            self.confirmed.append(name)


def _status_note(status: int) -> str:
    return (
        f"viewport status {status} cannot be trusted (COM export without activating the layout, "
        "or a viewport that is switched off): whether it prints is unknown"
    )


def _world_extent(extent: Extent, matrices: list[Matrix44]) -> Extent:
    xs: list[float] = []
    ys: list[float] = []
    x1, y1, x2, y2 = extent
    for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        point = Vec3(x, y, 0.0)
        for matrix in matrices:
            point = matrix.transform(point)
        xs.append(point.x)
        ys.append(point.y)
    return (min(xs), min(ys), max(xs), max(ys))


def _loc_result(
    visible: bool | None, verdict: PrintVerdict | None, note: str | None = None
) -> dict[str, Any]:
    """Result fields: ``prints_on`` lists confirmed layouts only; layouts that cannot be decided
    go to ``prints_on_unknown`` and ``prints_on`` is null when nothing is confirmed."""
    out: dict[str, Any] = {"visible_in_space": visible}
    notes: list[str] = [note] if note else []
    if verdict is None:
        out["prints_on"] = None
    else:
        real_unknown = [n for n in verdict.unknown if n != "?"]
        undecided = bool(verdict.unknown) and not verdict.confirmed
        out["prints_on"] = None if undecided else list(verdict.confirmed)
        if real_unknown:
            out["prints_on_unknown"] = real_unknown
        notes += list(dict.fromkeys(verdict.notes))
    if notes:
        out["prints_on_note"] = "; ".join(notes)
    return out


class DocModel:
    """Layer states, viewports and block usage of one document, computed lazily and cached."""

    def __init__(self, doc: Drawing) -> None:
        self.doc = doc
        self.hidden_layers: set[str] = set()
        self.noplot_layers: set[str] = set()  # plot flag off (e.g. Defpoints): drawn, never printed
        for layer in doc.layers:
            if layer.is_off() or layer.is_frozen():
                self.hidden_layers.add(layer.dxf.name.lower())
            if not layer.dxf.get("plot", 1):
                self.noplot_layers.add(layer.dxf.name.lower())
        self.layout_names = list(doc.layouts.names_in_taborder())
        self.paper_names = [n for n in self.layout_names if n.lower() != "model"]
        self.viewports: dict[str, list[ViewportInfo]] = {
            name: layout_viewports(doc.layouts.get(name)) for name in self.paper_names
        }
        self.warnings: list[str] = []
        for name, vps in self.viewports.items():
            for vp in vps:
                if not vp.overall and not vp.status_reliable:
                    self.warnings.append(f"layout {name!r}: {_status_note(vp.status)}")
        self._users: dict[str, list[tuple[str, str, str | None, Any]]] | None = None
        self._placements: dict[str, list[Placement]] = {}

    def layer_hidden(self, name: str) -> bool:
        return name.lower() in self.hidden_layers

    def layer_noplot(self, name: str) -> bool:
        return name.lower() in self.noplot_layers

    # -- block usage -------------------------------------------------------------------

    def _block_users(self) -> dict[str, list[tuple[str, str, str | None, Any]]]:
        if self._users is None:
            users: dict[str, list[tuple[str, str, str | None, Any]]] = defaultdict(list)
            for loc in iter_locations(self.doc, attribs=False):
                if loc.entity.dxftype() == "INSERT":
                    users[loc.entity.dxf.name.lower()].append(
                        (loc.space, loc.layout or "", loc.block, loc.entity)
                    )
            self._users = users
        return self._users

    def placements(self, block_name: str, _seen: frozenset[str] = frozenset()) -> list[Placement]:
        """Where a block definition ends up (model/paper layout + transform chain)."""
        key = block_name.lower()
        if key in self._placements:
            return self._placements[key]
        out: list[Placement] = []
        if key in _seen or len(_seen) >= MAX_NESTING:
            return out
        for space, layout, container, insert in self._block_users().get(key, []):
            try:
                matrix = insert.matrix44()
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            if space in ("model", "paper"):
                out.append(Placement(layout, [matrix], [insert.dxf.layer]))
            elif container is not None:
                for sub in self.placements(container, _seen | {key}):
                    out.append(
                        Placement(
                            sub.layout, [matrix, *sub.matrices], [insert.dxf.layer, *sub.layers]
                        )
                    )
            if len(out) >= MAX_BLOCK_PLACEMENTS:
                break
        if not _seen:
            self._placements[key] = out
        return out

    # -- printing ----------------------------------------------------------------------

    def extent_prints_on(self, extent: Extent, layers: Iterable[str]) -> PrintVerdict:
        """Layouts whose viewport window meets the model-space extent (a point is a 0-size one).

        A layout is *confirmed* when a viewport with a trustworthy status meets it and does not
        freeze one of ``layers``; it is *unknown* when the only candidates are viewports whose
        status is <= 0 or that have no usable top-view window. Nothing is guessed.
        """
        layers = list(layers)
        verdict = PrintVerdict()
        if any(self.layer_hidden(name) for name in layers):
            return verdict
        if any(self.layer_noplot(name) for name in layers):
            verdict.notes.append("layer has the plot flag off: it is never printed")
            return verdict
        for name, vps in self.viewports.items():
            confirmed = False
            maybe = False
            for vp in vps:
                if vp.overall:
                    continue
                if not vp.usable:
                    maybe = True
                    verdict.notes.append(f"{name}: viewport has no usable top-view window")
                    continue
                if any(vp.freezes(layer) for layer in layers):
                    continue
                if not vp.intersects_model_extent(extent):
                    continue
                if vp.clipped:
                    verdict.notes.append(
                        f"{name}: non-rectangular clip approximated by its rectangle"
                    )
                if vp.status_reliable:
                    confirmed = True
                    break
                maybe = True
                verdict.notes.append(f"{name}: {_status_note(vp.status)}")
            if confirmed:
                verdict.confirmed.append(name)
            elif maybe:
                verdict.unknown.append(name)
        return verdict

    @staticmethod
    def _extent(loc: Loc, bbox: Any) -> Extent | None:
        if bbox is _MISSING:
            bbox = geometry(loc.entity)[3]
        if isinstance(bbox, list) and len(bbox) == 4:
            return (bbox[0], bbox[1], bbox[2], bbox[3])
        point = anchor_of(loc.entity)
        return None if point is None else (point[0], point[1], point[0], point[1])

    def locate(self, loc: Loc, bbox: Any = _MISSING) -> dict[str, Any]:
        """``visible_in_space``, ``prints_on`` (+ ``prints_on_unknown``, ``prints_on_note``).

        A model-space object prints on a layout when its extent (bounding box; the insertion
        point when no extent is known) meets a viewport window on a layer the viewport does not
        freeze, the layer's plot flag is on and the viewport status is trustworthy. Pass
        ``bbox`` (``[x1, y1, x2, y2]`` or ``None``) to avoid measuring the entity again.
        """
        entity = loc.entity
        chain = [entity.dxf.layer]
        if loc.parent is not None:
            chain.append(loc.parent.dxf.layer)
        invisible = bool(entity.dxf.get("invisible", 0)) or (
            loc.parent is not None and bool(loc.parent.dxf.get("invisible", 0))
        )
        if invisible or any(self.layer_hidden(name) for name in chain):
            return _loc_result(False, PrintVerdict())
        if any(self.layer_noplot(name) for name in chain):
            noplot = PrintVerdict(notes=["layer has the plot flag off: it is never printed"])
            return _loc_result(True, noplot)
        if loc.space == "paper":
            return _loc_result(True, PrintVerdict(confirmed=[loc.layout or ""]))
        extent = self._extent(loc, bbox)
        if loc.space == "model":
            if extent is None:
                return _loc_result(True, None, "no geometry to test against viewports")
            return _loc_result(True, self.extent_prints_on(extent, chain))
        # block definition: only the instantiated copies are drawn
        placements = self.placements(loc.block or "")
        if not placements:
            return _loc_result(False, PrintVerdict())
        merged = PrintVerdict()
        visible = False
        for placement in placements:
            if any(self.layer_hidden(name) for name in placement.layers):
                continue
            visible = True
            if any(self.layer_noplot(name) for name in placement.layers):
                continue
            if placement.layout.lower() != "model":
                merged.add_confirmed(placement.layout)
                continue
            if extent is None:
                merged.notes.append("no geometry to test against viewports")
                merged.unknown.append("?")
                continue
            world = _world_extent(extent, placement.matrices)
            sub = self.extent_prints_on(world, [*chain, *placement.layers])
            for name in sub.confirmed:
                merged.add_confirmed(name)
            merged.unknown.extend(n for n in sub.unknown if n not in merged.unknown)
            merged.notes.extend(sub.notes)
        merged.notes.append(
            "block content positioned through INSERT transforms (base point ignored)"
        )
        merged.unknown = [n for n in merged.unknown if n not in merged.confirmed]
        return _loc_result(visible, merged)
