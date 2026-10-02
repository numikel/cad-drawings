"""Hardening of the DXF commands: outputs, printing logic, diff matching, untrusted paths."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import ezdxf
import pytest
from jsonschema import Draft202012Validator

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import diffing, dxf, util
from cadlib.diffing import _pair_nearest, diff_fingerprints
from cadlib.result import CadError

EDIT_SCHEMA = json.loads((SKILL / "assets" / "edit-spec.schema.json").read_text("utf-8"))
EXPECT = Draft202012Validator({"$ref": "#/$defs/expect", "$defs": EDIT_SCHEMA["$defs"]})


def run_cmd(name: str, argv: list[str], runs: Path) -> Any:
    parser = argparse.ArgumentParser()
    command = dxf.COMMANDS[name]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def read_jsonl(result: Any, key: str) -> list[dict[str, Any]]:
    path = Path(result.outputs[key]["path"])
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


def _fp(entities: list[dict[str, Any]]) -> dict[str, Any]:
    """A minimal fingerprint document around hand-made entities."""
    return {
        "schema": 1,
        "source": {"name": "x"},
        "header": {"insunits": 4, "measurement": 1, "handseed": "1"},
        "layouts": [],
        "blocks": [],
        "layers": {},
        "viewports": [],
        "counts": {},
        "total": len(entities),
        "warnings": [],
        "entities": entities,
    }


def _text(handle: int, x: float, y: float, text: str = "NOTE") -> dict[str, Any]:
    props = {"text": text, "height": 2.5}
    shape = diffing._hash(["TEXT", "0", props])
    return {
        "handle": f"{handle:X}",
        "type": "TEXT",
        "space": "model",
        "scope": "Model",
        "layer": "0",
        "bbox": None,
        "anchor": [x, y],
        "props": props,
        "shape": shape,
        "sig": diffing._hash([shape, [x, y]]),
    }


# --------------------------------------------------------------------------------------
# 2. outputs
# --------------------------------------------------------------------------------------


def test_fingerprint_out_must_not_be_an_input_even_by_another_spelling(
    fixtures_dir: Path, runs: Path, tmp_path: Path
) -> None:
    src = tmp_path / "plan.dxf"
    src.write_bytes((fixtures_dir / "sheet_set.dxf").read_bytes())
    before = src.read_bytes()
    sneaky = tmp_path / "sub" / ".." / "plan.dxf"
    (tmp_path / "sub").mkdir()
    for dest in (src, sneaky):
        with pytest.raises(CadError) as err:
            run_cmd("fingerprint", [str(src), "--out", str(dest), "--overwrite"], runs)
        assert err.value.code == "BAD_ARGS" and "input" in err.value.message
    assert src.read_bytes() == before
    with pytest.raises(CadError):
        run_cmd("fingerprint", [str(src), "--out", str(tmp_path)], runs)


def test_fingerprint_out_is_written_atomically(
    fixtures_dir: Path, runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "fp.json"
    out.write_text("previous", encoding="utf-8")
    real_replace = os.replace
    seen: list[str] = []

    def spy(src: Any, dst: Any) -> None:
        seen.append(Path(src).name)
        assert Path(src).exists() and Path(src) != Path(dst)  # a temporary sibling
        real_replace(src, dst)

    monkeypatch.setattr(util.os, "replace", spy)
    run_cmd(
        "fingerprint", [str(fixtures_dir / "sheet_set.dxf"), "--out", str(out), "--overwrite"], runs
    )
    assert any(name.endswith(".tmp") for name in seen)
    assert json.loads(out.read_text("utf-8"))["schema"] == 1
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_keeps_the_old_file_when_writing_fails(tmp_path: Path) -> None:
    dest = tmp_path / "keep.txt"
    dest.write_text("old", encoding="utf-8")

    def boom(tmp: Path) -> None:
        tmp.write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    with pytest.raises(OSError):
        util.atomic_write(dest, boom)
    assert dest.read_text("utf-8") == "old" and len(list(tmp_path.iterdir())) == 1


# --------------------------------------------------------------------------------------
# 4. prints_on
# --------------------------------------------------------------------------------------


def _window_doc(tmp_path: Path, name: str = "w.dxf") -> tuple[Any, Path]:
    doc = ezdxf.new("R2018")
    doc.layers.add("NOPRINT").dxf.plot = 0
    layout = doc.layouts.new("T")
    layout.add_viewport(
        center=(100, 100), size=(100, 100), view_center_point=(500, 500), view_height=100
    )  # window: x 450..550, y 450..550
    return doc, tmp_path / name


def test_defpoints_style_layers_do_not_print(tmp_path: Path, runs: Path) -> None:
    doc, path = _window_doc(tmp_path)
    msp = doc.modelspace()
    msp.add_text("NORMAL", dxfattribs={"height": 5}).set_placement((500, 500))
    msp.add_text("HELPER", dxfattribs={"height": 5, "layer": "NOPRINT"}).set_placement((500, 500))
    doc.saveas(path)
    hits = {
        h["text"]: h
        for h in read_jsonl(run_cmd("find", [str(path), "--pattern", "."], runs), "hits")
    }
    assert hits["NORMAL"]["prints_on"] == ["T"]
    assert hits["HELPER"]["prints_on"] == [] and "plot flag" in hits["HELPER"]["prints_on_note"]
    assert hits["HELPER"]["visible_in_space"] is True  # on screen, just not on paper


def test_a_line_that_starts_outside_but_crosses_the_window_prints(
    tmp_path: Path, runs: Path
) -> None:
    doc, path = _window_doc(tmp_path)
    msp = doc.modelspace()
    msp.add_line((0, 500), (1000, 500))  # starts far left, crosses the window
    msp.add_line((0, 0), (300, 0))  # nowhere near
    msp.add_circle((440, 500), 20)  # centre outside, rim inside
    doc.saveas(path)
    recs = read_jsonl(run_cmd("dump", [str(path), "--space", "model"], runs), "entities")
    by_type = {r["type"]: [] for r in recs}
    for r in recs:
        by_type[r["type"]].append(r["prints_on"])
    assert ["T"] in by_type["LINE"] and [] in by_type["LINE"]
    assert by_type["CIRCLE"] == [["T"]]


def test_extent_test_is_exact_for_rotated_windows() -> None:
    from cadlib.viewports import ViewportInfo

    vp = ViewportInfo(
        handle="1", vp_id=2, status=1, center=(0.0, 0.0), size=(100.0, 20.0),
        view_center=(0.0, 0.0), view_height=20.0, twist=45.0, frozen_layers=(),
        top_view=True, clipped=False,
    )  # fmt: skip
    # the window is a thin rectangle turned 45 degrees: along its diagonal it is long
    assert vp.intersects_model_extent((30, -32, 32, -30))
    assert not vp.intersects_model_extent((30, 30, 32, 32))  # off the long axis
    assert vp.intersects_model_extent((-1, -1, 1, 1))
    assert not vp.intersects_model_extent((200, 200, 201, 201))


def test_hidden_layer_prints_nowhere_and_block_content_uses_extents(
    tmp_path: Path, runs: Path
) -> None:
    doc, path = _window_doc(tmp_path)
    blk = doc.blocks.new("LONG")
    blk.add_line((0, 0), (400, 0))
    doc.modelspace().add_blockref("LONG", (300, 500))  # content spans x 300..700 on y 500
    doc.saveas(path)
    recs = read_jsonl(run_cmd("dump", [str(path), "--space", "LONG"], runs), "entities")
    assert recs[0]["prints_on"] == ["T"] and "INSERT transforms" in recs[0]["prints_on_note"]


# --------------------------------------------------------------------------------------
# 5. diff
# --------------------------------------------------------------------------------------


def test_large_groups_are_matched_by_distance_not_by_position() -> None:
    cols = 20
    a = [_text(i, (i % cols) * 10.0, (i // cols) * 10.0) for i in range(300)]
    b = [_text(1000 + i, (i % cols) * 10.0 + 0.5, (i // cols) * 10.0) for i in range(100, 300)]
    assert len(a) * len(b) > 40000  # the case that used to fall back to list order
    report = diff_fingerprints(_fp(a), _fp(b))
    assert report["counts"] == {"changed": 0, "moved": 200, "removed": 100, "added": 0}
    moved = [c for c in report["changes"] if c["kind"] == "moved"]
    assert {tuple(c["vector"]) for c in moved} == {(0.5, 0.0)}
    removed = {c["handle"] for c in report["changes"] if c["kind"] == "removed"}
    assert removed == {f"{i:X}" for i in range(100)}


def test_pair_nearest_is_greedy_by_global_distance() -> None:
    a = [{"anchor": [0.0, 0.0]}, {"anchor": [10.0, 0.0]}]
    b = [{"anchor": [9.0, 0.0]}, {"anchor": [100.0, 0.0]}]
    pairs = _pair_nearest(a, b)
    assert [(p[0]["anchor"][0], p[1]["anchor"][0]) for p in pairs] == [(10.0, 9.0), (0.0, 100.0)]
    assert _pair_nearest(a, []) == [] and _pair_nearest([], b) == []


def test_pair_nearest_scales_to_many_items() -> None:
    import random
    import time

    rng = random.Random(7)
    pts = [(rng.uniform(0, 5000), rng.uniform(0, 5000)) for _ in range(6000)]
    a = [{"anchor": [x, y]} for x, y in pts]
    b = [{"anchor": [x + 0.25, y]} for x, y in pts]
    started = time.monotonic()
    pairs = _pair_nearest(a, b)
    assert time.monotonic() - started < 10
    assert len(pairs) == len(pts) and all(
        p[1]["anchor"][0] - p[0]["anchor"][0] == 0.25 for p in pairs
    )


def _diff_files(tmp_path: Path, runs: Path, before: Any, after: Any) -> Any:
    one, two = tmp_path / "one.dxf", tmp_path / "two.dxf"
    before.saveas(one)
    after.saveas(two)
    res = run_cmd("diff", [str(one), str(two)], runs)
    return res, json.loads(Path(res.outputs["diff"]["path"]).read_text("utf-8"))


def _hatch_doc(points: list[tuple[float, float]]) -> Any:
    doc = ezdxf.new("R2018")
    hatch = doc.modelspace().add_hatch(color=1)
    hatch.paths.add_polyline_path(points, is_closed=True)
    return doc


def test_hatch_with_a_changed_boundary_but_the_same_bbox_is_a_change(
    tmp_path: Path, runs: Path
) -> None:
    triangle_a = _hatch_doc([(0, 0), (10, 0), (10, 10)])
    triangle_b = _hatch_doc([(0, 0), (10, 0), (0, 10)])  # same bounding box, other shape
    res, report = _diff_files(tmp_path, runs, triangle_a, triangle_b)
    assert res.summary["identical"] is False
    changed = [c for c in report["changes"] if c["type"] == "HATCH"]
    assert changed and any(x["field"] == "interior" for x in changed[0]["changes"])
    same, _ = _diff_files(
        tmp_path,
        runs,
        _hatch_doc([(0, 0), (10, 0), (10, 10)]),
        _hatch_doc([(0, 0), (10, 0), (10, 10)]),
    )
    assert same.summary["identical"] is True


def test_hatch_moved_as_a_whole_is_a_move_not_a_change(tmp_path: Path, runs: Path) -> None:
    res, report = _diff_files(
        tmp_path,
        runs,
        _hatch_doc([(0, 0), (10, 0), (10, 10)]),
        _hatch_doc([(5, 5), (15, 5), (15, 15)]),
    )
    assert res.summary["moved"] == 1 and res.summary["changed"] == 0
    assert report["changes"][0]["vector"] == [5.0, 5.0]


def test_spline_and_leader_interior_changes_are_seen(tmp_path: Path, runs: Path) -> None:
    def make(mid: tuple[float, float], leader_mid: tuple[float, float]) -> Any:
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        msp.add_spline(fit_points=[(0, 0), mid, (20, 10), (30, 0)])
        msp.add_leader([(0, 20), leader_mid, (30, 20)])
        return doc

    res, report = _diff_files(tmp_path, runs, make((10, 5), (15, 25)), make((10, 6), (15, 24)))
    kinds = {(c["type"], c["kind"]) for c in report["changes"]}
    assert ("SPLINE", "changed") in kinds or {("SPLINE", "removed"), ("SPLINE", "added")} <= kinds
    assert res.summary["identical"] is False
    same, _ = _diff_files(tmp_path, runs, make((10, 5), (15, 25)), make((10, 5), (15, 25)))
    assert same.summary["identical"] is True


def test_multileader_geometry_is_part_of_the_signature(tmp_path: Path, runs: Path) -> None:
    from ezdxf.render.mleader import ConnectionSide

    def make(tip: tuple[float, float]) -> Any:
        doc = ezdxf.new("R2018")
        builder = doc.modelspace().add_multileader_mtext("Standard")
        builder.set_content("note")
        builder.add_leader_line(ConnectionSide.left, [ezdxf.math.Vec2(*tip)])
        builder.build(insert=ezdxf.math.Vec2(10, 10))
        return doc

    res, _ = _diff_files(tmp_path, runs, make((0, 0)), make((0, 1)))
    assert res.summary["identical"] is False


# --------------------------------------------------------------------------------------
# 6. untrusted paths
# --------------------------------------------------------------------------------------


def test_path_is_local_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    assert util.path_is_local("relative/dir/file.dxf")
    assert not util.path_is_local("\\\\server\\share\\ref.dxf")
    assert not util.path_is_local("//server/share/ref.dxf")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(util, "_drive_is_fixed", lambda drive: drive.upper() == "C:")
    assert util.path_is_local("C:\\Projects\\ref.dxf")
    assert not util.path_is_local("Z:\\mapped\\ref.dxf")
    assert not util.path_is_local("z:/mapped/ref.dxf")
    assert util.safe_is_file("Z:\\mapped\\ref.dxf") is None


def test_info_never_touches_network_xref_or_font_paths(
    tmp_path: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = ezdxf.new("R2018")
    doc.add_xref_def(filename="\\\\fileserver\\share\\site.dxf", name="NETREF")
    doc.add_xref_def(filename="Q:\\remote\\other.dxf", name="DRIVEREF")
    doc.add_xref_def(filename="local\\missing.dxf", name="LOCALREF")
    doc.styles.add("NETFONT", font="\\\\fileserver\\fonts\\custom.shx")
    path = tmp_path / "net.dxf"
    doc.saveas(path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(util, "_drive_is_fixed", lambda drive: drive.upper() == "C:")
    touched: list[str] = []
    real_is_file = Path.is_file

    def spy(self: Path, *args: Any, **kwargs: Any) -> bool:
        touched.append(str(self))
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", spy)
    res = run_cmd("info", [str(path)], runs)
    info = json.loads(Path(res.outputs["info"]["path"]).read_text("utf-8"))
    xrefs = {x["block"]: x for x in info["xrefs"]}
    assert xrefs["NETREF"]["checked"] is False and xrefs["DRIVEREF"]["checked"] is False
    assert xrefs["LOCALREF"]["checked"] is True and xrefs["LOCALREF"]["resolved_on_disk"] is False
    assert not [t for t in touched if "fileserver" in t or "remote" in t]
    assert res.summary["xrefs"]["unchecked"] == 2
    assert any("not checked" in w and "NETREF" in w for w in res.warnings)
    assert all(f["found"] is None for f in info["fonts"]["shx"])


# --------------------------------------------------------------------------------------
# 7. regex bounds
# --------------------------------------------------------------------------------------


def test_pattern_length_and_subject_length_are_bounded(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("a" * 6000 + "NEEDLE", dxfattribs={"height": 1})
    doc.modelspace().add_text("short NEEDLE", dxfattribs={"height": 1})
    path = tmp_path / "long.dxf"
    doc.saveas(path)
    with pytest.raises(CadError) as err:
        run_cmd("find", [str(path), "--pattern", "x" * 501], runs)
    assert err.value.code == "BAD_ARGS"
    with pytest.raises(CadError) as err2:
        run_cmd("find", [str(path), "--pattern", "(unclosed"], runs)
    assert err2.value.code == "BAD_ARGS"
    res = run_cmd("find", [str(path), "--pattern", "NEEDLE"], runs)
    assert res.summary["hits"] == 1  # the needle past 5000 characters is not searched
    assert res.summary["truncated_subjects"] >= 1
    assert any("searched cut" in w for w in res.warnings)
    helptext = argparse.ArgumentParser()
    dxf.COMMANDS["find"].add_arguments(helptext)
    assert "5000" in helptext.format_help() and "500" in helptext.format_help()


# --------------------------------------------------------------------------------------
# 9. expect objects for edit plans
# --------------------------------------------------------------------------------------


def test_hits_carry_an_expect_object_valid_for_the_edit_schema(
    fixtures_dir: Path, runs: Path
) -> None:
    src = str(fixtures_dir / "sheet_set.dxf")
    hits = read_jsonl(run_cmd("find", [src, "--pattern", "FIRE"], runs), "hits")
    spaces = set()
    for hit in hits:
        if hit["type"] in ("LAYER", "BLOCK"):
            assert "expect" not in hit  # table entries are not editable entities
            continue
        EXPECT.validate(hit["expect"])
        assert hit["expect"]["type"] == hit["type"] and hit["expect"]["layer"] == hit["layer"]
        spaces.add(hit["expect"]["space"])
        assert hit["expect"]["text"] == hit["raw"] and hit["expect"]["text_is_plain"] is False
    assert spaces == {"model", "Sheet-A", "block:UNUSED_SYMBOL"}


def test_dump_expect_for_inserts_has_attributes_and_insertion_point(
    fixtures_dir: Path, runs: Path, truth: dict[str, Any]
) -> None:
    claims = truth["files"]["blocks_attribs.dxf"]["inserts"][0]
    res = run_cmd(
        "dump", [str(fixtures_dir / "blocks_attribs.dxf"), "--handle", claims["handle"]], runs
    )
    rec = read_jsonl(res, "entities")[0]
    EXPECT.validate(rec["expect"])
    assert rec["expect"]["attrib"] == {t: a["value"] for t, a in claims["attribs"].items()}
    assert rec["expect"]["insert"][:2] == claims["insert"][:2]
    assert rec["expect"]["space"] == "model"
