"""The ``find`` search over text, attributes, layer and block names."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from .drawing import Loaded
from .entities import _ATTRIB_TYPES, _TEXT_TYPES, anchor_of, entity_text, expect_for, iter_locations
from .printing import DocModel
from .result import CadError
from .util import _LAYOUT_BLOCKS, Deadline

_WHERE = ("text", "attrib", "layer", "block")
MAX_PATTERN_CHARS = 500
MAX_SUBJECT_CHARS = 5000


def compile_pattern(pattern: str, ignore_case: bool) -> re.Pattern[str]:
    """Compile a user pattern; over-long patterns and syntax errors are BAD_ARGS."""
    if len(pattern) > MAX_PATTERN_CHARS:
        raise CadError(
            "BAD_ARGS",
            f"--pattern has {len(pattern)} characters; the limit is {MAX_PATTERN_CHARS}",
            hint="use a shorter pattern",
        )
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise CadError("BAD_ARGS", f"invalid regular expression: {exc}") from exc


def _bounded(text: str | None, stats: dict[str, int]) -> str | None:
    """Cut a subject to ``MAX_SUBJECT_CHARS`` so a backtracking pattern cannot run for long."""
    if text is not None and len(text) > MAX_SUBJECT_CHARS:
        stats["truncated_subjects"] = stats.get("truncated_subjects", 0) + 1
        return text[:MAX_SUBJECT_CHARS]
    return text


def _matches(
    rx: re.Pattern[str], plain: str | None, raw: str | None, stats: dict[str, int] | None = None
) -> str | None:
    stats = stats if stats is not None else {}
    plain, raw = _bounded(plain, stats), _bounded(raw, stats)
    in_plain = plain is not None and rx.search(plain) is not None
    in_raw = raw is not None and rx.search(raw) is not None
    if in_plain and in_raw:
        return "both"
    return "plain" if in_plain else "raw" if in_raw else None


def find_hits(
    loaded: Loaded,
    rx: re.Pattern[str],
    where: set[str],
    hidden: str,
    deadline: Deadline,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Hits of ``rx`` in one document; see the module docstring for the field meanings."""
    stats = stats if stats is not None else {}
    doc = loaded.doc
    model = DocModel(doc)
    name = str(loaded.source)
    for i, loc in enumerate(iter_locations(doc)):
        if i % 2000 == 0:
            deadline.check()
        kind = loc.entity.dxftype()
        if kind in _ATTRIB_TYPES:
            if "attrib" not in where:
                continue
        elif kind in _TEXT_TYPES:
            if "text" not in where:
                continue
        else:
            continue
        plain, raw = entity_text(loc.entity)
        matched = _matches(rx, plain, raw, stats)
        if matched is None:
            continue
        place = model.locate(loc)
        is_hidden = place["visible_in_space"] is False
        if (hidden == "exclude" and is_hidden) or (hidden == "only" and not is_hidden):
            continue
        hit = {
            "file": name,
            "handle": str(loc.entity.dxf.handle),
            "type": kind,
            "space": loc.space,
            "layout": loc.layout,
            "block": loc.block,
            "layer": loc.entity.dxf.layer,
            "text": plain,
            "raw": raw,
            "matched_in": matched,
            "insert": anchor_of(loc.entity),
            **place,
            "expect": expect_for(loc),
        }
        if loc.parent is not None:
            hit["parent_handle"] = str(loc.parent.dxf.handle)
        yield hit
    table_ok = hidden != "only"
    if "layer" in where and table_ok:
        for layer in doc.layers:
            if rx.search(_bounded(layer.dxf.name, stats) or ""):
                yield _table_hit(name, "LAYER", layer.dxf.handle, layer.dxf.name, layer.dxf.name)
    if "block" in where and table_ok:
        for blk in doc.blocks:
            if blk.name.lower().startswith(_LAYOUT_BLOCKS):
                continue
            if rx.search(_bounded(blk.name, stats) or ""):
                handle = blk.block_record.dxf.handle if blk.block_record is not None else None
                yield _table_hit(name, "BLOCK", handle, blk.name, None)


def _table_hit(file: str, kind: str, handle: Any, text: str, layer: str | None) -> dict[str, Any]:
    return {
        "file": file,
        "handle": str(handle) if handle is not None else None,
        "type": kind,
        "space": "table",
        "layout": None,
        "block": text if kind == "BLOCK" else None,
        "layer": layer,
        "text": text,
        "raw": text,
        "matched_in": kind.lower(),
        "insert": None,
        "visible_in_space": None,
        "prints_on": None,
    }
