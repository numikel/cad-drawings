"""Fingerprint of the graphic entities of a drawing."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .result import CadError

if TYPE_CHECKING:
    from .runs import RunContext
from .drawing import Loaded, _resolve_source, open_drawing
from .drawing_info import _header_units
from .entities import entity_text, geometry, iter_locations
from .printing import DocModel
from .util import _LAYOUT_BLOCKS, Deadline, _hash, _pt, r4
from .viewports import _viewport_info

FINGERPRINT_SCHEMA = 1


def build_fingerprint(loaded: Loaded, ctx: RunContext, deadline: Deadline) -> dict[str, Any]:
    from .runs import file_sha1

    doc = loaded.doc
    model = DocModel(doc)
    entities: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for i, loc in enumerate(iter_locations(doc, attribs=False)):
        if i % 2000 == 0:
            deadline.check()
        e = loc.entity
        kind = e.dxftype()
        if kind == "VIEWPORT" and loc.space == "paper":
            vp_info = _viewport_info(e)
            if vp_info.overall or vp_info.vp_id == 1:
                continue
        anchor, _abs, relative, bbox = geometry(e)
        plain, _raw = entity_text(e)
        layer = e.dxf.layer
        shape = _hash([kind, layer, relative])
        rec: dict[str, Any] = {
            "handle": str(e.dxf.handle),
            "type": kind,
            "space": loc.space,
            "scope": loc.layout if loc.space != "block" else loc.block,
            "layer": layer,
            "bbox": bbox,
            "anchor": anchor,
            "text_sha1": hashlib.sha1(plain.encode("utf-8")).hexdigest() if plain else None,
            "props": _abs,
            "shape": shape,
            "sig": _hash([shape, anchor]),
        }
        if kind == "VIEWPORT":
            rec["vp_id"] = _viewport_info(e).vp_id
        entities.append(rec)
        counts[kind] += 1
    layers = {
        lay.dxf.name: {
            "off": lay.is_off(),
            "frozen": lay.is_frozen(),
            "locked": lay.is_locked(),
            "color": int(lay.dxf.get("color", 7)),
            "linetype": str(lay.dxf.get("linetype", "")),
            "plot": bool(lay.dxf.get("plot", 1)),
        }
        for lay in doc.layers
    }
    viewports = [
        {
            "layout": name,
            "id": vp.vp_id,
            "status": vp.status,
            "overall": vp.overall,
            "center": [r4(vp.center[0]), r4(vp.center[1])],
            "size": [r4(vp.size[0]), r4(vp.size[1])],
            "view_center": [r4(vp.view_center[0]), r4(vp.view_center[1])],
            "view_center_wcs": _pt(vp.view_center_wcs),
            "target": _pt(vp.target),
            "view_height": r4(vp.view_height),
            "scale": r4(vp.scale),
            "twist": r4(vp.twist),
            "frozen_layers": sorted(vp.frozen_layers, key=str.lower),
        }
        for name, vps in model.viewports.items()
        for vp in vps
    ]
    return {
        "schema": FINGERPRINT_SCHEMA,
        "source": {
            "name": loaded.source.name,
            "bytes": loaded.source.stat().st_size,
            "sha1": file_sha1(loaded.source),
        },
        "header": {
            **_header_units(doc),
            "handseed": str(doc.header.get("$HANDSEED", "")),
            "dxf_version": doc.dxfversion,
        },
        "layouts": [
            {"name": n, "block": doc.layouts.get(n).block_record_name} for n in model.paper_names
        ],
        "blocks": [b.name for b in doc.blocks if not b.name.lower().startswith(_LAYOUT_BLOCKS)],
        "layers": layers,
        "viewports": viewports,
        "counts": dict(counts),
        "total": len(entities),
        "warnings": loaded.warnings + model.warnings,
        "entities": entities,
    }


def _fingerprint_of(
    path: Path, ctx: RunContext, deadline: Deadline, **conversion: Any
) -> tuple[dict[str, Any], bool]:
    """Fingerprint from a ``.json`` fingerprint file or from a drawing; second value: approximate."""
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(_resolve_source(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CadError(
                "BAD_FINGERPRINT",
                f"cannot read fingerprint {path.name}: {exc}",
                hint="regenerate it",
            ) from exc
        if not isinstance(data, dict) or data.get("schema") != FINGERPRINT_SCHEMA:
            raise CadError(
                "BAD_FINGERPRINT",
                f"{path.name} is not a fingerprint (schema {FINGERPRINT_SCHEMA})",
                hint="create it with the fingerprint command",
            )
        return data, False
    loaded = open_drawing(path, ctx, **conversion)
    return build_fingerprint(loaded, ctx, deadline), loaded.approximate
