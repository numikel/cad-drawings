"""Semantic diff of two fingerprints."""

from __future__ import annotations

import heapq
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from .util import _brief, _hash, _is_anonymous, r4

SUMMARY_CHANGES = 10


DIFF_JSON_CAP = 5000


def _scope_names(fp: dict[str, Any]) -> dict[str, str]:
    """Block name -> comparison name; anonymous blocks are identified by their content."""
    members: dict[str, list[str]] = defaultdict(list)
    for e in fp["entities"]:
        if e["space"] == "block":
            members[e["scope"]].append(e["sig"])
    out: dict[str, str] = {}
    for name in fp.get("blocks", []):
        out[name] = (
            f"*anon:{_hash(sorted(members.get(name, [])), 10)}" if _is_anonymous(name) else name
        )
    return out


def _prop_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key in sorted(set(old) | set(new)):
        if key != "field" and old.get(key) != new.get(key):  # "field" is a marker, not a change
            out.append({"field": key, "old": _brief(old.get(key)), "new": _brief(new.get(key))})
    return out


def _changed(ea: dict[str, Any], eb: dict[str, Any], diffs: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``changed`` record; ``field`` marks entities that carry a CAD field in either version."""
    rec = {"kind": "changed", **_ref(ea), "handle_b": eb["handle"], "changes": diffs}
    if ea["props"].get("field") or eb["props"].get("field"):
        rec["field"] = True
    return rec


def _pair_nearest(
    a_items: list[dict[str, Any]], b_items: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair two equally-shaped groups greedily by smallest anchor distance.

    Repeatedly takes the globally closest remaining pair (ties broken by position in the
    input). A uniform grid over the B anchors keeps this near O(n log n) for any group size, so
    large groups are paired by distance too - never by list position. Items left over on either
    side stay unpaired.
    """
    pa = [tuple(e["anchor"] or (0.0, 0.0)) for e in a_items]
    pb = [tuple(e["anchor"] or (0.0, 0.0)) for e in b_items]
    if not pa or not pb:
        return []
    xs = [p[0] for p in pa + pb]
    ys = [p[1] for p in pa + pb]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    cell = span / max(1, int(math.sqrt(len(pb))))

    def key(p: tuple[float, float]) -> tuple[int, int]:
        return (math.floor(p[0] / cell), math.floor(p[1] / cell))

    grid: dict[tuple[int, int], set[int]] = defaultdict(set)
    for j, p in enumerate(pb):
        grid[key(p)].add(j)
    cells = [key(p) for p in pa + pb]
    max_ring = (
        max(
            max(c[0] for c in cells) - min(c[0] for c in cells),
            max(c[1] for c in cells) - min(c[1] for c in cells),
        )
        + 1
    )

    def nearest(i: int) -> tuple[float, int] | None:
        cx, cy = key(pa[i])
        best: tuple[float, int] | None = None
        for r in range(max_ring + 1):
            if best is not None and (max(r - 1, 0) * cell) ** 2 > best[0]:
                break
            ring = (
                [(cx + dx, cy + dy) for dx in range(-r, r + 1) for dy in (-r, r)]
                + [(cx + dx, cy + dy) for dy in range(-r + 1, r) for dx in (-r, r)]
                if r
                else [(cx, cy)]
            )
            for c in ring:
                for j in grid.get(c, ()):
                    d = (pa[i][0] - pb[j][0]) ** 2 + (pa[i][1] - pb[j][1]) ** 2
                    if best is None or (d, j) < best:
                        best = (d, j)
        return best

    heap: list[tuple[float, int, int]] = []
    for i in range(len(pa)):
        found = nearest(i)
        if found is not None:
            heap.append((found[0], i, found[1]))
    heapq.heapify(heap)
    taken: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    while heap and len(taken) < len(pb):
        _d, i, j = heapq.heappop(heap)
        if j in taken:
            found = nearest(i)
            if found is not None:
                heapq.heappush(heap, (found[0], i, found[1]))
            continue
        taken.add(j)
        grid[key(pb[j])].discard(j)
        pairs.append((a_items[i], b_items[j]))
    return pairs


def _group(items: Iterable[dict[str, Any]], key: Any) -> dict[Any, list[dict[str, Any]]]:
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return groups


def _ref(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "handle": e["handle"],
        "type": e["type"],
        "space": e["space"],
        "scope": e["scope"],
        "layer": e["layer"],
        "anchor": e["anchor"],
    }


def _describe_change(c: dict[str, Any]) -> str:
    where = c["scope"] if c["space"] != "model" else "model space"
    base = f"{c['type']} on layer {c['layer']} in {where}"
    kind = c["kind"]
    if kind == "moved":
        v = c["vector"]
        return f"{base} moved by ({v[0]:g}, {v[1]:g}), shape unchanged"
    if kind == "changed":
        parts = [f"{x['field']}: {x['old']!r} -> {x['new']!r}" for x in c["changes"][:3]]
        return f"{base} changed ({'; '.join(parts)})"
    if kind == "removed":
        return f"{base} at {c['anchor']} has no counterpart in B (cause not determined)"
    return f"{base} at {c['anchor']} has no counterpart in A (cause not determined)"


def diff_fingerprints(
    a: dict[str, Any], b: dict[str, Any], *, full: bool = False
) -> dict[str, Any]:
    """Semantic diff of two fingerprints; see the module docstring of the diff command."""
    names_a, names_b = _scope_names(a), _scope_names(b)

    def scoped(
        fp: dict[str, Any], names: dict[str, str]
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for e in fp["entities"]:
            scope = names.get(e["scope"], e["scope"]) if e["space"] == "block" else e["scope"]
            groups[(e["space"], scope)].append(e)
        return groups

    sa, sb = scoped(a, names_a), scoped(b, names_b)
    tol, handle_bound = _near_tolerances(a, b)
    changes: list[dict[str, Any]] = []
    noise: Counter[str] = Counter()
    matched_pairs = 0

    def note_pair(ea: dict[str, Any], eb: dict[str, Any]) -> None:
        nonlocal matched_pairs
        matched_pairs += 1
        if ea["handle"] != eb["handle"]:
            noise["handles_renumbered"] += 1
        if ea["type"] == "VIEWPORT" and ea.get("vp_id") != eb.get("vp_id"):
            noise["viewport_ids_changed"] += 1

    for scope in sorted(set(sa) | set(sb)):
        left, right = list(sa.get(scope, [])), list(sb.get(scope, []))
        # 1. identical content
        by_sig = _group(right, lambda e: e["sig"])
        rest_a: list[dict[str, Any]] = []
        for ea in left:
            bucket = by_sig.get(ea["sig"])
            if bucket:
                note_pair(ea, bucket.pop(0))
            else:
                rest_a.append(ea)
        rest_b = [e for bucket in by_sig.values() for e in bucket]
        # 2. same shape somewhere else: moved
        groups_b = _group(rest_b, lambda e: (e["type"], e["layer"], e["shape"]))
        still_a: list[dict[str, Any]] = []
        by_shape_a = _group(rest_a, lambda e: (e["type"], e["layer"], e["shape"]))
        paired_b: set[int] = set()
        for key, items_a in by_shape_a.items():
            items_b = groups_b.get(key, [])
            if not items_b or any(e["anchor"] is None for e in items_a + items_b):
                still_a.extend(items_a)
                continue
            pairs = _pair_nearest(items_a, items_b)
            done_a = {id(p[0]) for p in pairs}
            for ea, eb in pairs:
                note_pair(ea, eb)
                vec = [r4(eb["anchor"][0] - ea["anchor"][0]), r4(eb["anchor"][1] - ea["anchor"][1])]
                changes.append(
                    {
                        "kind": "moved",
                        **_ref(ea),
                        "handle_b": eb["handle"],
                        "anchor_b": eb["anchor"],
                        "vector": vec,
                    }
                )
                paired_b.add(id(eb))
            still_a.extend(e for e in items_a if id(e) not in done_a)
        rest_b = [e for e in rest_b if id(e) not in paired_b]
        # 3. same place, different content: changed (first with the same layer, then any)
        for key_fn in (
            lambda e: (e["type"], e["layer"], tuple(e["anchor"] or ())),
            lambda e: (e["type"], tuple(e["anchor"] or ())),
        ):
            groups_b = _group(rest_b, key_fn)
            next_a: list[dict[str, Any]] = []
            used_b: set[int] = set()
            for ea in still_a:
                bucket = groups_b.get(key_fn(ea))
                bucket = [x for x in bucket if id(x) not in used_b] if bucket else []
                if bucket and ea["anchor"] is not None:
                    eb = bucket[0]
                    used_b.add(id(eb))
                    note_pair(ea, eb)
                    diffs = _prop_changes(ea["props"], eb["props"])
                    if ea["layer"] != eb["layer"]:
                        diffs.insert(0, {"field": "layer", "old": ea["layer"], "new": eb["layer"]})
                    changes.append(_changed(ea, eb, diffs))
                else:
                    next_a.append(ea)
            still_a = next_a
            rest_b = [e for e in rest_b if id(e) not in used_b]
        # 4. same entity edited in place: the anchor drifted a little (CAD recomputes the
        #    insertion point of aligned text) and the content differs, so no exact match
        near_pairs, still_a, rest_b = _pair_near_changed(still_a, rest_b, tol, handle_bound)
        for ea, eb in near_pairs:
            note_pair(ea, eb)
            diffs = _prop_changes(ea["props"], eb["props"])
            if ea["anchor"] != eb["anchor"]:
                diffs.append({"field": "anchor", "old": ea["anchor"], "new": eb["anchor"]})
            if ea.get("bbox") != eb.get("bbox"):
                diffs.append({"field": "bbox", "old": ea.get("bbox"), "new": eb.get("bbox")})
            changes.append(_changed(ea, eb, diffs))
        for ea in still_a:
            changes.append({"kind": "removed", **_ref(ea), "to_confirm": True})
        for eb in rest_b:
            changes.append({"kind": "added", **_ref(eb), "to_confirm": True})

    # anonymous / layout block names that differ although the content matched
    anon_a = {names_a[n]: n for n in a.get("blocks", []) if _is_anonymous(n)}
    anon_b = {names_b[n]: n for n in b.get("blocks", []) if _is_anonymous(n)}
    renamed = sum(1 for key, name in anon_a.items() if key in anon_b and name != anon_b[key])
    if renamed:
        noise["anonymous_blocks_renamed"] = renamed
    layouts_a = {x["name"]: x["block"] for x in a.get("layouts", [])}
    layouts_b = {x["name"]: x["block"] for x in b.get("layouts", [])}
    block_renamed = sum(1 for n, blk in layouts_a.items() if n in layouts_b and layouts_b[n] != blk)
    if block_renamed:
        noise["paper_space_block_names_changed"] = block_renamed
    if a["header"].get("handseed") != b["header"].get("handseed"):
        noise["handseed_changed"] = 1

    structural = _structural_changes(a, b, layouts_a, layouts_b)
    order = {"changed": 0, "moved": 1, "removed": 2, "added": 3}
    changes.sort(key=lambda c: (order[c["kind"]], c["scope"] or "", c["type"], c["handle"]))
    for c in changes:
        c["description"] = _describe_change(c)
    counts = Counter(c["kind"] for c in changes)
    cap = None if full else DIFF_JSON_CAP
    return {
        "counts": {k: counts.get(k, 0) for k in order},
        "groups": group_changes(changes),
        "structural": structural,
        "noise": dict(noise),
        "matched_entities": matched_pairs,
        "entities_a": a["total"],
        "entities_b": b["total"],
        "changes": changes if cap is None else changes[:cap],
        "truncated": cap is not None and len(changes) > cap,
        "total_changes": len(changes) + len(structural),
    }


def _structural_changes(
    a: dict[str, Any], b: dict[str, Any], layouts_a: dict[str, str], layouts_b: dict[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(kind: str, what: str, name: str, **extra: Any) -> None:
        out.append(
            {
                "kind": kind,
                "what": what,
                "name": name,
                **extra,
                "description": f"{what} {name!r} {kind}",
            }
        )

    for name in sorted(set(layouts_a) - set(layouts_b)):
        add("removed", "layout", name, to_confirm=True)
    for name in sorted(set(layouts_b) - set(layouts_a)):
        add("added", "layout", name, to_confirm=True)
    for name in sorted(set(a["layers"]) - set(b["layers"])):
        add("removed", "layer", name, to_confirm=True)
    for name in sorted(set(b["layers"]) - set(a["layers"])):
        add("added", "layer", name, to_confirm=True)
    for name in sorted(set(a["layers"]) & set(b["layers"])):
        if a["layers"][name] != b["layers"][name]:
            diffs = _prop_changes(a["layers"][name], b["layers"][name])
            add("changed", "layer", name, changes=diffs)
    plain_a = {n for n in a.get("blocks", []) if not n.startswith("*")}
    plain_b = {n for n in b.get("blocks", []) if not n.startswith("*")}
    for name in sorted(plain_a - plain_b):
        add("removed", "block definition", name, to_confirm=True)
    for name in sorted(plain_b - plain_a):
        add("added", "block definition", name, to_confirm=True)
    for key in ("insunits", "measurement"):
        if a["header"].get(key) != b["header"].get(key):
            add(
                "changed",
                "setting",
                key,
                changes=[{"field": key, "old": a["header"].get(key), "new": b["header"].get(key)}],
            )
    return out


MAX_SUMMARY_GROUPS = 10
MAX_NAME_CHARS = 60
MAX_FIRST_STRUCTURAL = 3
MAX_DESCRIPTION_CHARS = 110


def _scope_label(change: dict[str, Any]) -> str:
    space = change["space"]
    if space == "model":
        return "model"
    return f"block:{change['scope']}" if space == "block" else str(change["scope"])


def _short(text: Any, limit: int = MAX_NAME_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def group_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changes grouped by (scope, layer, type), largest total volume first.

    Ties keep the order in which the groups first appear (changed, moved, removed, added).
    """
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in changes:
        key = (_scope_label(c), str(c["layer"]), c["type"])
        group = table.get(key)
        if group is None:
            group = table[key] = {
                "scope": key[0],
                "layer": key[1],
                "type": key[2],
                "removed": 0,
                "added": 0,
                "changed": 0,
                "moved": 0,
            }
        group[c["kind"]] += 1
    volume = lambda g: g["removed"] + g["added"] + g["changed"] + g["moved"]
    return sorted(table.values(), key=lambda g: -volume(g))  # sorted() is stable


def summary_groups(groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Top groups for the size-capped summary (names shortened) and a truncation note."""
    top = [
        {**g, "scope": _short(g["scope"]), "layer": _short(g["layer"]), "type": _short(g["type"])}
        for g in groups[:MAX_SUMMARY_GROUPS]
    ]
    note = None
    if len(groups) > MAX_SUMMARY_GROUPS:
        note = f"top {MAX_SUMMARY_GROUPS} of {len(groups)} groups; the full table is in diff.json"
    return top, note


def pick_first_changes(
    structural: list[dict[str, Any]], changes: list[dict[str, Any]], limit: int
) -> list[str]:
    """A varied sample: up to 3 structural changes, then one change per group in turn."""
    out = [
        _short(c["description"], MAX_DESCRIPTION_CHARS) for c in structural[:MAX_FIRST_STRUCTURAL]
    ]
    queues: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for c in changes:
        queues.setdefault((_scope_label(c), str(c["layer"]), c["type"]), []).append(c)
    ordered = [queues[(g["scope"], g["layer"], g["type"])] for g in group_changes(changes) if g]
    depth = 0
    while len(out) < limit and any(depth < len(q) for q in ordered):
        for queue in ordered:
            if depth < len(queue) and len(out) < limit:
                out.append(_short(queue[depth]["description"], MAX_DESCRIPTION_CHARS))
        depth += 1
    return out


def dominant_hint(groups: list[dict[str, Any]], total: int) -> str | None:
    """A ``next`` line when one group holds more than half of all entity changes."""
    if not groups or total <= 0:
        return None
    top = groups[0]
    volume = top["removed"] + top["added"] + top["changed"] + top["moved"]
    if volume * 2 <= total:
        return None
    layer, kind = _short(top["layer"]), _short(top["type"])
    return (
        f"most changes are {kind} on layer {layer} ({volume}): "
        f"consider `dump --layer {layer} --type {kind}`"
    )


NEAR_TOLERANCE = 0.01  # drawing units; scaled up for very large drawings
NEAR_RELATIVE = 1e-6
HANDLE_BOUND_RELATIVE = 0.01  # of the drawing extent


def _near_tolerances(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, float]:
    """``(anchor tolerance, handle-match bound)`` from the extent of both drawings."""
    xs: list[float] = []
    ys: list[float] = []
    for fp in (a, b):
        for e in fp["entities"]:
            if e["anchor"] is not None:
                xs.append(e["anchor"][0])
                ys.append(e["anchor"][1])
    extent = max(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0
    return max(NEAR_TOLERANCE, NEAR_RELATIVE * extent), HANDLE_BOUND_RELATIVE * extent


def _pair_near_changed(
    removed: list[dict[str, Any]],
    added: list[dict[str, Any]],
    tol: float,
    handle_bound: float,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair leftover removed/added entities that are the same entity edited in place.

    Required: same type, same layer (the scope is already equal) and either an anchor within
    ``tol``, or the same handle with the anchor within ``handle_bound`` (a handle alone proves
    nothing: re-saves renumber them). One-to-one, nearest first, ties by input order.
    """
    if not removed or not added:
        return [], removed, added
    cell = max(tol, 1e-9)
    grid: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    by_handle: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for j, e in enumerate(added):
        if e["anchor"] is None:
            continue
        key = (e["type"], e["layer"])
        grid[(*key, math.floor(e["anchor"][0] / cell), math.floor(e["anchor"][1] / cell))].append(j)
        by_handle[(*key, e["handle"])].append(j)
    candidates: list[tuple[float, int, int]] = []
    for i, ea in enumerate(removed):
        if ea["anchor"] is None:
            continue
        ax, ay = ea["anchor"]
        cx, cy = math.floor(ax / cell), math.floor(ay / cell)
        seen: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((ea["type"], ea["layer"], cx + dx, cy + dy), ()):
                    seen.add(j)
        near = {
            j
            for j in seen
            if math.hypot(added[j]["anchor"][0] - ax, added[j]["anchor"][1] - ay) <= tol
        }
        for j in by_handle.get((ea["type"], ea["layer"], ea["handle"]), ()):
            d = math.hypot(added[j]["anchor"][0] - ax, added[j]["anchor"][1] - ay)
            if d <= handle_bound:
                near.add(j)
        for j in near:
            d = math.hypot(added[j]["anchor"][0] - ax, added[j]["anchor"][1] - ay)
            candidates.append((d, i, j))
    candidates.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for _d, i, j in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((removed[i], added[j]))
    return (
        pairs,
        [e for i, e in enumerate(removed) if i not in used_a],
        [e for j, e in enumerate(added) if j not in used_b],
    )
