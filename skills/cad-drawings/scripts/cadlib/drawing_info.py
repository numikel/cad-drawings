"""The ``info`` report of a drawing."""

from __future__ import annotations

import contextlib
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ezdxf
import ezdxf.bbox
import ezdxf.units

if TYPE_CHECKING:
    from ezdxf.document import Drawing

from .drawing import Loaded
from .entities import anchor_of, iter_locations
from .printing import DocModel
from .util import _LAYOUT_BLOCKS, _is_anonymous, _pt, path_is_local, r4, safe_is_file

_BOUND_XREF = re.compile(r"^(?P<xref>.+)\$\d+\$(?P<name>.+)$")


def _header_units(doc: Drawing) -> dict[str, Any]:
    insunits = int(doc.header.get("$INSUNITS", 0))
    measurement = int(doc.header.get("$MEASUREMENT", 0))
    return {
        "insunits": insunits,
        "insunits_name": ezdxf.units.decode(insunits) or "unitless",
        "measurement": measurement,
        "measurement_name": "metric" if measurement == 1 else "imperial",
    }


def _extents(doc: Drawing) -> dict[str, Any]:
    lo, hi = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
    if lo is not None and hi is not None and abs(lo[0]) < 1e19 and abs(hi[0]) < 1e19:
        return {"min": _pt(lo), "max": _pt(hi), "source": "header"}
    # header values are missing or unset: a cheap sweep over vertices and anchors (text and block
    # extents are not measured; ezdxf.bbox.extents costs ~0.15 ms per entity)
    xs: list[float] = []
    ys: list[float] = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        points: list[Any] = []
        if kind == "LINE":
            points = [entity.dxf.start, entity.dxf.end]
        elif kind == "CIRCLE":
            c, r = entity.dxf.center, entity.dxf.radius
            points = [(c.x - r, c.y - r), (c.x + r, c.y + r)]
        elif kind == "LWPOLYLINE":
            points = [(x, y) for x, y, *_ in entity.get_points("xy")]
        else:
            anchor = anchor_of(entity)
            points = [anchor] if anchor else []
        for p in points:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return {"min": None, "max": None, "source": "empty"}
    return {
        "min": [r4(min(xs)), r4(min(ys))],
        "max": [r4(max(xs)), r4(max(ys))],
        "source": "computed from vertices and anchors (text and block extents not measured)",
    }


def _dget(namespace: Any, attr: str, default: Any) -> Any:
    """``dxf.get`` that tolerates attributes the entity type does not define."""
    try:
        return namespace.get(attr, default)
    except AttributeError:
        return default


def _layout_info(doc: Drawing, model: DocModel, name: str) -> dict[str, Any]:
    layout = doc.layouts.get(name)
    d = layout.dxf
    info: dict[str, Any] = {"name": name, "block": layout.block_record_name}
    if name.lower() == "model":
        return info
    info["page_setup"] = {
        "paper_size_mm": [r4(d.get("paper_width", 0.0)), r4(d.get("paper_height", 0.0))],
        "paper_units": int(d.get("plot_paper_units", 1)),
        "rotation": int(d.get("plot_rotation", 0)),
        "plot_style": layout.get_plot_style_filename() or None,
        "device": str(_dget(d, "plot_configuration_file", "")) or None,
        "page_setup_name": str(_dget(d, "page_setup_name", "")) or None,
    }
    with contextlib.suppress(Exception):
        lo, hi = layout.get_paper_limits()
        info["page_setup"]["limits"] = [r4(lo.x), r4(lo.y), r4(hi.x), r4(hi.y)]
    info["viewports"] = [
        {
            "handle": vp.handle,
            "id": vp.vp_id,
            "overall": vp.overall,
            "status": vp.status,
            "center": [r4(vp.center[0]), r4(vp.center[1])],
            "size": [r4(vp.size[0]), r4(vp.size[1])],
            "view_center": [r4(vp.view_center[0]), r4(vp.view_center[1])],
            "view_center_wcs": _pt(vp.view_center_wcs),
            "target": _pt(vp.target),
            "view_height": r4(vp.view_height),
            "scale": r4(vp.scale),
            "twist": r4(vp.twist),
            "frozen_layers": list(vp.frozen_layers),
        }
        for vp in model.viewports.get(name, [])
    ]
    return info


def _block_stats(
    doc: Drawing, source_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    instances: Counter[str] = Counter()
    for loc in iter_locations(doc, attribs=False):
        if loc.entity.dxftype() == "INSERT":
            instances[loc.entity.dxf.name.lower()] += 1
    blocks: list[dict[str, Any]] = []
    xrefs: list[dict[str, Any]] = []
    for blk in doc.blocks:
        if blk.name.lower().startswith(_LAYOUT_BLOCKS):
            continue
        flags = int(blk.block.dxf.get("flags", 0))
        is_xref = bool(flags & 4)
        entry = {
            "name": blk.name,
            "anonymous": _is_anonymous(blk.name),
            "entities": len(blk),
            "instances": instances.get(blk.name.lower(), 0),
            "attdefs": sum(1 for e in blk if e.dxftype() == "ATTDEF"),
            "xref": is_xref,
        }
        blocks.append(entry)
        if is_xref:
            stored = str(blk.block.dxf.get("xref_path", ""))
            raw = stored.replace("\\", "/")
            resolved: bool | None = False
            if raw:
                # never probe a path that could reach the network (UNC, network or removable drive)
                resolved = safe_is_file(raw) if path_is_local(raw) else None
                if resolved is False and path_is_local(raw):
                    resolved = safe_is_file(source_dir / raw) or False
            xrefs.append(
                {
                    "block": blk.name,
                    "path": stored,
                    "overlay": bool(flags & 8),
                    "resolved_on_disk": bool(resolved),
                    "checked": resolved is not None,
                    "instances": instances.get(blk.name.lower(), 0),
                }
            )
    return blocks, xrefs


def _bound_xrefs(doc: Drawing) -> list[str]:
    found: set[str] = set()
    for layer in doc.layers:
        m = _BOUND_XREF.match(layer.dxf.name)
        if m:
            found.add(m.group("xref"))
    for blk in doc.blocks:
        m = _BOUND_XREF.match(blk.name)
        if m:
            found.add(m.group("xref"))
    return sorted(found)


def _lock_files(source: Path) -> list[str]:
    stem = source.stem.lower()
    try:
        return sorted(
            p.name
            for p in source.parent.iterdir()
            if p.is_file() and p.suffix.lower() in (".dwl", ".dwl2") and p.stem.lower() == stem
        )
    except OSError:
        return []


def _conventions(doc: Drawing, model: DocModel) -> dict[str, Any]:
    styles = [
        {
            "name": s.dxf.name,
            "font": str(s.dxf.get("font", "")),
            "height": r4(s.dxf.get("height", 0)),
        }
        for s in doc.styles
    ]
    heights: Counter[float] = Counter()
    per_layout_text: Counter[str] = Counter()
    paper_inserts: Counter[str] = Counter()
    for loc in iter_locations(doc, attribs=False):
        kind = loc.entity.dxftype()
        if kind in ("TEXT", "MTEXT"):
            h = loc.entity.dxf.get("height" if kind == "TEXT" else "char_height", 0.0)
            heights[r4(h)] += 1
            if loc.space == "paper" and loc.layout:
                per_layout_text[loc.layout] += 1
        elif kind == "INSERT" and loc.space == "paper":
            paper_inserts[loc.entity.dxf.name] += 1

    def prefixes(names: Iterable[str]) -> list[dict[str, Any]]:
        counts = Counter(re.split(r"[-_$ ]", n, maxsplit=1)[0] for n in names if n)
        return [{"prefix": k, "count": v} for k, v in counts.most_common(10)]

    layer_names = [layer.dxf.name for layer in doc.layers]
    block_names = [b.name for b in doc.blocks if not b.name.startswith("*")]
    title_candidates = [
        {"block": name, "paper_space_inserts": n}
        for name, n in paper_inserts.most_common(5)
        if n >= max(1, len(model.paper_names) // 2)
    ]
    return {
        "text_styles": styles,
        "text_heights": [{"height": h, "count": n} for h, n in heights.most_common(10)],
        "title_block_guess": {
            "repeated_paper_space_blocks": title_candidates,
            "layout_with_most_text": per_layout_text.most_common(1)[0][0]
            if per_layout_text
            else None,
        },
        "layer_prefixes": prefixes(layer_names),
        "block_prefixes": prefixes(block_names),
        "layout_names": model.paper_names,
    }


def _font_report(fonts: list[str], source_dir: Path) -> dict[str, Any]:
    """SHX fonts used by text styles; ``found`` is true when the file is beside the drawing or in
    ezdxf's support folders, otherwise null (the CAD installation's folders are not searched)."""
    folders = [source_dir, *(Path(d) for d in ezdxf.options.support_dirs)]
    folders = [f for f in folders if path_is_local(f)]
    shx = []
    for name in (f for f in fonts if f.lower().endswith(".shx")):
        found = path_is_local(name) and any(
            safe_is_file(folder / name) is True for folder in folders
        )
        shx.append({"name": name, "found": True if found else None})
    return {
        "shx": shx,
        "other": [f for f in fonts if not f.lower().endswith(".shx")],
        "note": "found=null means not beside the drawing or in support folders; "
        "the CAD installation is not searched",
    }


def build_info(loaded: Loaded, conventions: bool) -> dict[str, Any]:
    doc = loaded.doc
    model = DocModel(doc)
    blocks, xrefs = _block_stats(doc, loaded.source.parent)
    layers = [
        {
            "name": lay.dxf.name,
            "off": lay.is_off(),
            "frozen": lay.is_frozen(),
            "locked": lay.is_locked(),
            "color": int(lay.dxf.get("color", 7)),
            "plot": bool(lay.dxf.get("plot", 1)),
        }
        for lay in doc.layers
    ]
    fonts = sorted({str(s.dxf.get("font", "")) for s in doc.styles if s.dxf.get("font", "")})
    info: dict[str, Any] = {
        "file": str(loaded.source),
        "dxf_version": doc.dxfversion,
        "units": _header_units(doc),
        "extents": _extents(doc),
        "layouts": [_layout_info(doc, model, n) for n in model.layout_names],
        "layers": layers,
        "blocks": blocks,
        "xrefs": xrefs,
        "bound_xref_prefixes": _bound_xrefs(doc),
        "fonts": _font_report(fonts, loaded.source.parent),
        "lock_files": _lock_files(loaded.source),
        "warnings": loaded.warnings + model.warnings,
    }
    if conventions:
        info["conventions"] = _conventions(doc, model)
    return info
