"""dxf.py: info, find, dump, fingerprint and diff against the synthetic fixtures' ground truth."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ezdxf
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import diffing, dxf
from cadlib.result import CadError, ExitCode, Result

NON_ASCII = "zażółć gęślą jaźń"


def run_cmd(name: str, argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = dxf.COMMANDS[name]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def read_jsonl(result: Result, key: str) -> list[dict[str, Any]]:
    path = Path(result.outputs[key]["path"])
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def read_json(result: Result, key: str) -> Any:
    return json.loads(Path(result.outputs[key]["path"]).read_text(encoding="utf-8"))


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture(scope="module")
def fx(fixtures_dir: Path) -> Path:
    return fixtures_dir


@pytest.fixture(scope="module")
def sheet_truth(truth: dict[str, Any]) -> dict[str, Any]:
    return truth["files"]["sheet_set.dxf"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------------------
# find
# --------------------------------------------------------------------------------------


def test_find_reaches_every_space_and_matches_ground_truth(
    fx: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    res = run_cmd("find", [str(fx / "sheet_set.dxf"), "--pattern", "FIRE"], runs)
    hits = {h["handle"]: h for h in read_jsonl(res, "hits")}
    claims = {o["handle"]: o for o in sheet_truth["search"]["occurrences"]}
    assert set(hits) == set(claims)
    for handle, occ in claims.items():
        hit = hits[handle]
        assert (hit["type"], hit["space"], hit["layout"], hit["block"]) == (
            occ["type"],
            occ["space"],
            occ["layout"],
            occ["block"],
        )
        assert hit["visible_in_space"] == occ["visible"], occ["id"]
        assert hit["prints_on"] == occ["prints_on"], occ["id"]
    assert hits[claims_id(claims, "attrib_value")]["parent_handle"] == "4B"
    assert res.summary["hits"] == 6 and res.summary["hidden"] == 2
    assert res.summary["by_space"] == {"model": 3, "paper": 1, "block": 1, "table": 1}


def claims_id(claims: dict[str, Any], key: str) -> str:
    return next(h for h, o in claims.items() if o["id"] == key)


def test_find_hidden_modes_and_where_filter(fx: Path, runs: Path) -> None:
    src = str(fx / "sheet_set.dxf")
    only = run_cmd("find", [src, "--pattern", "FIRE", "--hidden", "only"], runs)
    assert {h["handle"] for h in read_jsonl(only, "hits")} == {"44", "56"}
    excl = run_cmd("find", [src, "--pattern", "FIRE", "--hidden", "exclude"], runs)
    assert {h["handle"] for h in read_jsonl(excl, "hits")} == {"4E", "35", "59", "76"}
    layer = run_cmd("find", [src, "--pattern", "fire", "-i", "--where", "layer"], runs)
    assert [h["type"] for h in read_jsonl(layer, "hits")] == ["LAYER"]
    assert run_cmd("find", [src, "--pattern", "fire", "--where", "text"], runs).summary["hits"] == 0
    assert (
        run_cmd("find", [src, "--pattern", "fire", "-i", "--where", "text"], runs).summary["hits"]
        == 4
    )


def test_find_mtext_plain_and_raw(fx: Path, runs: Path, truth: dict[str, Any]) -> None:
    claims = truth["files"]["mtext_cases.dxf"]["mtext"]
    res = run_cmd("find", [str(fx / "mtext_cases.dxf"), "--pattern", ".", "--where", "text"], runs)
    hits = {h["handle"]: h for h in read_jsonl(res, "hits")}
    assert hits, "mtext_cases.dxf has MTEXT entities"
    for case in claims if isinstance(claims, list) else claims.values():
        hit = hits[case["handle"]]
        assert hit["raw"] == case["raw"]
        assert hit["text"] == case["plain_text"]


def test_find_attributes_attdefs_and_block_names(
    fx: Path, runs: Path, truth: dict[str, Any]
) -> None:
    claims = truth["files"]["blocks_attribs.dxf"]
    src = str(fx / "blocks_attribs.dxf")
    res = run_cmd("find", [src, "--pattern", "OFFICE|STORE|HALL|WC", "--where", "attrib"], runs)
    hits = read_jsonl(res, "hits")
    assert {h["type"] for h in hits} == {"ATTRIB", "ATTDEF"} or {h["type"] for h in hits} == {
        "ATTRIB"
    }
    expected = {a["handle"] for i in claims["inserts"] for a in i["attribs"].values()}
    got = {h["handle"] for h in hits if h["type"] == "ATTRIB"}
    assert got == {
        a["handle"] for i in claims["inserts"] for t, a in i["attribs"].items() if t == "ROOM_NAME"
    }
    assert got <= expected
    by_tag = run_cmd("find", [src, "--pattern", "ROOM_NO", "--where", "block,text,attrib"], runs)
    assert by_tag.summary["hits"] == 0  # tags are not values; block name is ROOM_TAG
    blk = run_cmd("find", [src, "--pattern", "^ROOM_TAG$", "--where", "block"], runs)
    assert [h["type"] for h in read_jsonl(blk, "hits")] == ["BLOCK"]
    anon = run_cmd("find", [src, "--pattern", "ANON CONTENT"], runs)
    assert read_jsonl(anon, "hits")[0]["block"].startswith("*U")


def test_find_several_files_and_bad_regex(fx: Path, runs: Path) -> None:
    res = run_cmd(
        "find",
        [str(fx / "sheet_set.dxf"), str(fx / "sheet_set_v1.dxf"), "--pattern", "STORE|DRAWN"],
        runs,
    )
    assert set(res.summary["by_file"]) == {"sheet_set.dxf", "sheet_set_v1.dxf"}
    with pytest.raises(CadError) as err:
        run_cmd("find", [str(fx / "sheet_set.dxf"), "--pattern", "("], runs)
    assert err.value.code == "BAD_ARGS"


def test_find_limit_truncates_with_warning(fx: Path, runs: Path) -> None:
    res = run_cmd("find", [str(fx / "sheet_set.dxf"), "--pattern", ".", "--limit", "3"], runs)
    assert res.summary["hits"] == 3 and res.summary["truncated"]
    assert any("--limit" in w for w in res.warnings)


# --------------------------------------------------------------------------------------
# prints_on edge cases on small purpose-built drawings
# --------------------------------------------------------------------------------------


def _drawing(tmp_path: Path) -> Path:
    doc = ezdxf.new("R2018")
    doc.layers.add("TXT")
    doc.layers.add("OFF").off()
    blk = doc.blocks.new("SYM")
    blk.add_text("SYMBOL", dxfattribs={"layer": "TXT", "height": 1}).set_placement((0, 0))
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("SYM", (10, 10))
    msp = doc.modelspace()
    for name, pos, layer in (
        ("INSIDE", (500, 500), "TXT"),
        ("OUTSIDE", (5000, 5000), "TXT"),
        ("HIDDENLAYER", (500, 500), "OFF"),
        ("EDGE", (600, 500), "TXT"),
    ):
        msp.add_text(name, dxfattribs={"layer": layer, "height": 5}).set_placement(pos)
    msp.add_blockref("OUTER", (400, 400))  # SYM ends up at (410, 410): inside
    layout = doc.layouts.new("L1")
    layout.add_viewport(
        center=(100, 100), size=(100, 100), view_center_point=(500, 500), view_height=200
    )
    layout.add_blockref("SYM", (50, 50))
    bad = doc.layouts.new("BROKEN")
    vp = bad.add_viewport(
        center=(100, 100), size=(100, 100), view_center_point=(500, 500), view_height=200
    )
    vp.dxf.view_height = 0
    path = tmp_path / "small.dxf"
    doc.saveas(path)
    return path


def test_prints_on_window_edges_layers_blocks_and_unknown(tmp_path: Path, runs: Path) -> None:
    src = _drawing(tmp_path)
    res = run_cmd("find", [str(src), "--pattern", ".", "--where", "text"], runs)
    hits = {(h["text"], h["space"], h["block"]): h for h in read_jsonl(res, "hits")}
    inside = hits[("INSIDE", "model", None)]
    # the layout with an unusable viewport makes the answer unknown only when nothing else hits
    assert inside["prints_on"] == ["L1"]
    outside = hits[("OUTSIDE", "model", None)]
    assert outside["prints_on"] is None and "BROKEN" in outside["prints_on_note"]
    hidden = hits[("HIDDENLAYER", "model", None)]
    assert hidden["visible_in_space"] is False and hidden["prints_on"] == []
    assert hits[("EDGE", "model", None)]["prints_on"] == ["L1"]  # exactly on the window border
    sym = hits[("SYMBOL", "block", "SYM")]
    assert sym["visible_in_space"] is True
    assert sym["prints_on"] == ["L1"]  # via the paper-space insert; nested model copy at (410,410)
    assert "base point ignored" in sym["prints_on_note"]


def test_prints_on_respects_twist_target_and_viewport_freeze(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.layers.add("A")
    doc.layers.add("B")
    msp = doc.modelspace()
    msp.add_text("A-ITEM", dxfattribs={"layer": "A"}).set_placement((590, 500))
    msp.add_text("B-ITEM", dxfattribs={"layer": "B"}).set_placement((500, 500))
    msp.add_text("C-ITEM", dxfattribs={"layer": "A"}).set_placement((520, 590))
    layout = doc.layouts.new("T")
    # the stored centre is in the display system: DCS = R(+twist) * (WCS - target)
    # twist 90, target origin: WCS centre (500, 500) is stored as (-500, 500)
    vp = layout.add_viewport(
        center=(100, 100), size=(200, 100), view_center_point=(-500, 500), view_height=100
    )
    vp.dxf.view_twist_angle = 90.0
    vp.frozen_layers = ["B"]
    src = tmp_path / "twist.dxf"
    doc.saveas(src)
    res = run_cmd("find", [str(src), "--pattern", "ITEM"], runs)
    hits = {h["text"]: h for h in read_jsonl(res, "hits")}
    # 200 x 100 on paper is 100 x 200 in model after the twist: x 450..550, y 400..600
    assert hits["A-ITEM"]["prints_on"] == []  # x = 590 is outside
    assert hits["C-ITEM"]["prints_on"] == ["T"]  # inside only because the centre is read as DCS
    assert hits["B-ITEM"]["prints_on"] == []  # frozen in the viewport
    info = run_cmd("info", [str(src)], runs)
    layouts = {lay["name"]: lay for lay in read_json(info, "info")["layouts"]}
    vp_info = next(v for v in layouts["T"]["viewports"] if not v["overall"])
    assert vp_info["view_center"] == [-500.0, 500.0]
    assert vp_info["view_center_wcs"] == pytest.approx([500.0, 500.0])
    # untwisted with a non-zero target: window centre = target + stored centre
    vp.dxf.view_twist_angle = 0.0
    vp.dxf.view_center_point = (100, 500)
    vp.dxf.view_target_point = (400, 0, 0)
    src2 = tmp_path / "target.dxf"
    doc.saveas(src2)
    res = run_cmd("find", [str(src2), "--pattern", "ITEM"], runs)
    hits = {h["text"]: h for h in read_jsonl(res, "hits")}
    assert hits["A-ITEM"]["prints_on"] == ["T"]  # window x 400..600, y 450..550
    assert hits["C-ITEM"]["prints_on"] == []
    assert hits["B-ITEM"]["prints_on"] == []
    # a viewport that does not look straight down cannot be tested
    vp.dxf.view_direction_vector = (0, -1, 1)
    src3 = tmp_path / "oblique.dxf"
    doc.saveas(src3)
    res = run_cmd("find", [str(src3), "--pattern", "A-ITEM"], runs)
    hit = read_jsonl(res, "hits")[0]
    assert hit["prints_on"] is None and "top-view" in hit["prints_on_note"]


def test_unreliable_viewport_status_gives_unknown_never_a_guess(
    fx: Path, runs: Path, tmp_path: Path
) -> None:
    doc = ezdxf.readfile(fx / "sheet_set.dxf")
    for name in ("Sheet-A", "Sheet-B"):
        for vp in doc.layouts.get(name).query("VIEWPORT"):
            if vp.dxf.id > 1:
                vp.dxf.status = 0
    src = tmp_path / "status0.dxf"
    doc.saveas(src)
    res = run_cmd("find", [str(src), "--pattern", "FIRE RATING"], runs)
    hit = read_jsonl(res, "hits")[0]
    # the old skill said "Sheet-A only"; true and false would both be guesses
    assert hit["prints_on"] is None
    assert hit["prints_on_unknown"] == ["Sheet-A", "Sheet-B"]
    assert "status 0" in hit["prints_on_note"] and "COM export" in hit["prints_on_note"]
    assert any("cannot be trusted" in w for w in res.warnings)
    info = run_cmd("info", [str(src)], runs)
    assert info.summary["viewports_status_unreliable"] == 2


def test_one_trustworthy_layout_is_confirmed_next_to_an_unknown_one(
    fx: Path, runs: Path, tmp_path: Path
) -> None:
    doc = ezdxf.readfile(fx / "sheet_set.dxf")
    for vp in doc.layouts.get("Sheet-B").query("VIEWPORT"):
        if vp.dxf.id > 1:
            vp.dxf.status = -1
    src = tmp_path / "mixed.dxf"
    doc.saveas(src)
    hit = read_jsonl(run_cmd("find", [str(src), "--pattern", "FIRE RATING"], runs), "hits")[0]
    assert hit["prints_on"] == ["Sheet-A"] and hit["prints_on_unknown"] == ["Sheet-B"]


# --------------------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------------------


def test_info_reports_viewports_units_and_layers(
    fx: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    res = run_cmd("info", [str(fx / "sheet_set.dxf")], runs)
    info = read_json(res, "info")
    assert info["units"]["insunits"] == sheet_truth["insunits"] == 4
    assert info["units"]["insunits_name"] == "mm" and info["units"]["measurement_name"] == "metric"
    names = [lay["name"] for lay in info["layouts"]]
    assert names == ["Model", "Sheet-A", "Sheet-B"]  # tab order
    for lay, claim in zip(info["layouts"][1:], sheet_truth["layouts"], strict=True):
        user = [vp for vp in lay["viewports"] if not vp["overall"]]
        assert len(user) == 1 and len(lay["viewports"]) == 2
        vp, ref = user[0], claim["viewports"][0]
        assert vp["handle"] == ref["handle"] and vp["center"] == pytest.approx(ref["center"])
        assert vp["size"] == pytest.approx(ref["size"])
        assert vp["scale"] == pytest.approx(ref["view_height"] / ref["size"][1], abs=1e-3)
        assert vp["twist"] == ref["twist_deg"] and vp["frozen_layers"] == ref["frozen_layers"]
        assert vp["view_center"] == pytest.approx(ref["view_center"])  # as stored (DCS)
        assert vp["view_center_wcs"] == pytest.approx(ref["view_center_wcs"])
        assert vp["status"] > 0
    frozen = [lay["name"] for lay in info["layers"] if lay["frozen"]]
    assert frozen == sheet_truth["globally_frozen_layers"]
    assert res.summary["layouts"] == ["Sheet-A", "Sheet-B"] and res.summary["viewports"] == 2


def test_info_xref_missing_and_block_stats(fx: Path, runs: Path, truth: dict[str, Any]) -> None:
    res = run_cmd("info", [str(fx / "xref_missing.dxf")], runs)
    info = read_json(res, "info")
    claim = truth["files"]["xref_missing.dxf"]["xrefs"][0]
    assert [x["block"] for x in info["xrefs"]] == [claim["block_name"]]
    assert info["xrefs"][0]["resolved_on_disk"] is False and not info["xrefs"][0]["overlay"]
    assert res.summary["xrefs"] == {"count": 1, "unresolved": 1, "unchecked": 0, "bound": 0}
    assert any("not found on disk" in w for w in res.warnings)
    blocks = read_json(run_cmd("info", [str(fx / "blocks_attribs.dxf")], runs), "info")["blocks"]
    tag = next(b for b in blocks if b["name"] == "ROOM_TAG")
    assert tag["attdefs"] == 3 and tag["instances"] == 4
    assert any(b["anonymous"] for b in blocks)


def test_info_lock_files_and_conventions(fx: Path, runs: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(fx / "sheet_set.dxf", work / "sheet_set.dxf")
    (work / "sheet_set.dwl").write_text("x")
    (work / "sheet_set.dwl2").write_text("x")
    (work / "other.dwl").write_text("x")
    res = run_cmd("info", [str(work / "sheet_set.dxf"), "--conventions"], runs)
    assert res.summary["lock_files"] == ["sheet_set.dwl", "sheet_set.dwl2"]
    assert any("lock files" in w for w in res.warnings)
    conv = read_json(res, "info")["conventions"]
    assert conv["layout_names"] == ["Sheet-A", "Sheet-B"] and conv["text_styles"]
    assert conv["layer_prefixes"] and conv["text_heights"]


def test_missing_and_unsupported_input(runs: Path, tmp_path: Path) -> None:
    for command in ("info", "dump", "fingerprint"):
        with pytest.raises(CadError) as err:
            run_cmd(command, [str(tmp_path / "nope.dxf")], runs)
        assert err.value.code == "FILE_NOT_FOUND" and err.value.exit_code == ExitCode.BAD_ARGS
    other = tmp_path / "x.txt"
    other.write_text("hi")
    with pytest.raises(CadError) as err2:
        run_cmd("info", [str(other)], runs)
    assert err2.value.code == "UNSUPPORTED" and err2.value.exit_code == ExitCode.BAD_ARGS


def test_damaged_dxf_is_recovered_with_a_warning(fx: Path, runs: Path, tmp_path: Path) -> None:
    text = (fx / "sheet_set.dxf").read_text(encoding="utf-8")
    broken = tmp_path / "broken.dxf"
    broken.write_text(text[: int(len(text) * 0.8)], encoding="utf-8")
    res = run_cmd("info", [str(broken)], runs)
    assert any("recover" in w for w in res.warnings)
    assert res.summary["layers"]["count"] >= 1


# --------------------------------------------------------------------------------------
# dump
# --------------------------------------------------------------------------------------


def test_dump_filters(fx: Path, runs: Path) -> None:
    src = str(fx / "sheet_set.dxf")
    texts = read_jsonl(
        run_cmd("dump", [src, "--space", "model", "--type", "TEXT"], runs), "entities"
    )
    assert (
        texts
        and {t["type"] for t in texts} == {"TEXT"}
        and {t["space"] for t in texts} == {"model"}
    )
    assert all("text" in t and "visible_in_space" in t for t in texts)
    sheet = run_cmd("dump", [src, "--space", "Sheet-B"], runs)
    assert {e["layout"] for e in read_jsonl(sheet, "entities")} == {"Sheet-B"}
    by_handle = read_jsonl(
        run_cmd("dump", [src, "--handle", "4e", "--handle", "44"], runs), "entities"
    )
    assert {e["handle"] for e in by_handle} == {"4E", "44"}
    blocks = run_cmd("dump", [src, "--space", "UNUSED_SYMBOL"], runs)
    assert {e["block"] for e in read_jsonl(blocks, "entities")} == {"UNUSED_SYMBOL"}


def test_dump_window_layer_and_limit(fx: Path, runs: Path) -> None:
    src = str(fx / "sheet_set.dxf")
    win = run_cmd("dump", [src, "--space", "model", "--window", "0,0,100,100"], runs)
    everything = run_cmd("dump", [src, "--space", "model"], runs)
    assert 0 < win.summary["entities"] < everything.summary["entities"]
    layer = run_cmd("dump", [src, "--layer", "a-text"], runs)
    assert {e["layer"] for e in read_jsonl(layer, "entities")} == {"A-TEXT"}
    cut = run_cmd("dump", [src, "--limit", "4"], runs)
    assert cut.summary == {**cut.summary, "entities": 4, "truncated": True}
    assert any("--limit" in w for w in cut.warnings)
    with pytest.raises(CadError):
        run_cmd("dump", [src, "--window", "1,2,3"], runs)


def test_dump_geometry_of_known_entities(fx: Path, runs: Path, truth: dict[str, Any]) -> None:
    claims = truth["files"]["sheet_set.dxf"]["rooms"][0]
    res = run_cmd("dump", [str(fx / "sheet_set.dxf"), "--handle", claims["polygon_handle"]], runs)
    rec = read_jsonl(res, "entities")[0]
    pts = [p[:2] for p in rec["props"]["points"]]
    assert pts == [list(v) for v in claims["vertices_units"]] and rec["props"]["closed"] is True
    assert rec["bbox"] == [0.0, 0.0, 5000.0, 4000.0]


# --------------------------------------------------------------------------------------
# fingerprint and diff
# --------------------------------------------------------------------------------------


def test_fingerprint_contents_and_out_protection(fx: Path, runs: Path, tmp_path: Path) -> None:
    out = tmp_path / "fp" / "sheet.json"
    res = run_cmd("fingerprint", [str(fx / "sheet_set.dxf"), "--out", str(out)], runs)
    fp = json.loads(out.read_text(encoding="utf-8"))
    assert fp["schema"] == 1 and fp["total"] == len(fp["entities"]) == res.summary["entities"]
    assert sum(fp["counts"].values()) == fp["total"] and fp["counts"]["TEXT"] > 5
    assert not any(e["type"] == "VIEWPORT" and e["props"]["size"][0] > 400 for e in fp["entities"])
    assert {v["layout"] for v in fp["viewports"]} == {"Sheet-A", "Sheet-B"}
    assert sum(1 for v in fp["viewports"] if v["overall"]) == 2
    first = fp["entities"][0]
    assert {"handle", "type", "scope", "layer", "bbox", "anchor", "text_sha1", "sig"} <= set(first)
    assert res.outputs["fingerprint"]["path"] == str(out)
    assert len(json.dumps(res.to_dict())) < 4096
    with pytest.raises(CadError) as err:
        run_cmd("fingerprint", [str(fx / "sheet_set.dxf"), "--out", str(out)], runs)
    assert err.value.code == "EXISTS"
    run_cmd("fingerprint", [str(fx / "sheet_set.dxf"), "--out", str(out), "--overwrite"], runs)


def test_diff_finds_exactly_the_three_real_changes(
    fx: Path, runs: Path, changes: dict[str, Any]
) -> None:
    res = run_cmd("diff", [str(fx / "sheet_set_v1.dxf"), str(fx / "plan_v2.dxf")], runs)
    assert res.summary["changed"] == res.summary["moved"] == res.summary["removed"] == 1
    assert res.summary["added"] == 0 and res.summary["structural"] == 0
    report = read_json(res, "diff")
    real = {c["id"]: c for c in changes["real_changes"]}
    by_kind = {c["kind"]: c for c in report["changes"]}
    assert len(report["changes"]) == 3
    assert by_kind["changed"]["handle"] == real["C1"]["v1_handle"]
    assert by_kind["changed"]["handle_b"] == real["C1"]["v2_handle"]
    assert [(c["field"], c["old"], c["new"]) for c in by_kind["changed"]["changes"]] == [
        ("text", "STORE", "ARCHIVE")
    ]
    assert by_kind["removed"]["handle"] == real["C2"]["v1_handle"]
    assert by_kind["removed"]["to_confirm"] is True
    assert "cause not determined" in by_kind["removed"]["description"]
    assert by_kind["moved"]["handle"] == real["C3"]["v1_handle"]
    assert by_kind["moved"]["handle_b"] == real["C3"]["v2_handle"]
    assert by_kind["moved"]["vector"] == pytest.approx(real["C3"]["vector"])


def test_diff_noise_is_counted_not_listed(fx: Path, runs: Path) -> None:
    res = run_cmd("diff", [str(fx / "sheet_set_v1.dxf"), str(fx / "plan_v2.dxf")], runs)
    noise = res.summary["noise"]
    assert noise["handles_renumbered"] > 30
    assert noise["viewport_ids_changed"] == 2
    assert noise["anonymous_blocks_renamed"] == 1
    assert noise["paper_space_block_names_changed"] == 1
    assert noise["handseed_changed"] == 1
    assert all(isinstance(v, int) for v in noise.values())
    assert len(res.summary["first_changes"]) == 3


def test_self_diff_is_empty(fx: Path, runs: Path) -> None:
    for name in ("sheet_set.dxf", "plan_v2.dxf", "blocks_attribs.dxf", "hatch_assoc.dxf"):
        res = run_cmd("diff", [str(fx / name), str(fx / name)], runs)
        assert res.summary["identical"] is True, name
        assert res.summary["changed"] == res.summary["moved"] == 0


def _renumbered_copy(src: Path, dst: Path) -> None:
    """Same drawing, but every graphic entity re-created in reverse order (new handles)."""
    doc = ezdxf.readfile(src)
    layouts = [doc.modelspace(), *(doc.layouts.get(n) for n in doc.layouts.names_in_taborder()[1:])]
    for layout in layouts:
        for entity in reversed([e for e in layout if e.dxftype() != "VIEWPORT"]):
            clone = entity.copy()
            layout.add_entity(clone)
            layout.delete_entity(entity)
    doc.saveas(dst)


def test_diff_ignores_reordering_and_handle_renumbering(
    fx: Path, runs: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "renumbered.dxf"
    _renumbered_copy(fx / "sheet_set.dxf", copy)
    res = run_cmd("diff", [str(fx / "sheet_set.dxf"), str(copy)], runs)
    assert res.summary["identical"] is True, res.summary["first_changes"]
    assert res.summary["noise"]["handles_renumbered"] > 20


def test_diff_accepts_fingerprint_files_and_reports_structure(
    fx: Path, runs: Path, tmp_path: Path
) -> None:
    fp_path = tmp_path / "a.json"
    run_cmd("fingerprint", [str(fx / "sheet_set.dxf"), "--out", str(fp_path)], runs)
    same = run_cmd("diff", [str(fp_path), str(fx / "sheet_set.dxf")], runs)
    assert same.summary["identical"] is True
    doc = ezdxf.readfile(fx / "sheet_set.dxf")
    doc.layers.add("NEW-LAYER")
    doc.layers.get("A-TEXT").dxf.color = 1
    doc.modelspace().add_text("NEW TEXT", dxfattribs={"layer": "NEW-LAYER", "height": 3})
    doc.layouts.delete("Sheet-B")
    changed = tmp_path / "changed.dxf"
    doc.saveas(changed)
    res = run_cmd("diff", [str(fp_path), str(changed)], runs)
    report = read_json(res, "diff")
    kinds = {(s["what"], s["kind"], s["name"]) for s in report["structural"]}
    assert ("layout", "removed", "Sheet-B") in kinds
    assert ("layer", "added", "NEW-LAYER") in kinds
    assert ("layer", "changed", "A-TEXT") in kinds
    added = [c for c in report["changes"] if c["kind"] == "added"]
    assert [c["type"] for c in added] == ["TEXT"] and added[0]["to_confirm"]
    assert res.next and "confirm" in res.next[0]
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(CadError) as err:
        run_cmd("diff", [str(bad), str(changed)], runs)
    assert err.value.code == "BAD_FINGERPRINT"


def test_diff_full_flag_lifts_the_cap(
    fx: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diffing, "DIFF_JSON_CAP", 1)
    capped = run_cmd("diff", [str(fx / "sheet_set_v1.dxf"), str(fx / "plan_v2.dxf")], runs)
    assert len(read_json(capped, "diff")["changes"]) == 1 and capped.warnings
    full = run_cmd("diff", [str(fx / "sheet_set_v1.dxf"), str(fx / "plan_v2.dxf"), "--full"], runs)
    assert len(read_json(full, "diff")["changes"]) == 3


# --------------------------------------------------------------------------------------
# DWG path through a fake converter, cache and temporary files
# --------------------------------------------------------------------------------------


class FakeEnsure:
    """Stands in for ``convert.ensure_dxf``: records the consent flags, returns a fixed DXF."""

    def __init__(
        self, dxf_path: Path, warnings: list[str] | None = None, approximate: bool = False
    ):
        self.dxf_path, self.warnings, self.approximate = dxf_path, warnings or [], approximate
        self.calls: list[dict[str, Any]] = []

    def __call__(self, src: Path, ctx: Any, **kwargs: Any) -> Any:
        self.calls.append({"src": src, **kwargs})
        backend = "libredwg" if self.approximate else "oda"
        return SimpleNamespace(
            path=self.dxf_path,
            backend=backend,
            approximate=self.approximate,
            cached=False,
            warnings=list(self.warnings),
        )


@pytest.fixture
def dwg(tmp_path: Path) -> Path:
    path = tmp_path / "drawing.dwg"
    path.write_bytes(b"AC1032 not really a dwg")
    return path


def test_every_command_reads_dwg_through_the_one_pipeline(
    fx: Path, runs: Path, dwg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import convert as convert_module

    fake = FakeEnsure(fx / "sheet_set.dxf", ["fake converter warning"])
    monkeypatch.setattr(convert_module, "ensure_dxf", fake)
    runs_of = {
        "info": run_cmd("info", [str(dwg)], runs),
        "find": run_cmd("find", [str(dwg), "--pattern", "FIRE"], runs),
        "dump": run_cmd("dump", [str(dwg), "--type", "TEXT"], runs),
        "fingerprint": run_cmd("fingerprint", [str(dwg)], runs),
        "diff": run_cmd("diff", [str(dwg), str(fx / "sheet_set.dxf")], runs),
    }
    assert len(fake.calls) == 5 and all(c["src"] == dwg.resolve() for c in fake.calls)
    # reading a DWG never silently starts CAD: no consent unless it is given
    assert all(c["allow_com"] is False and c["prefer"] is None for c in fake.calls)
    for command, res in runs_of.items():
        assert any("fake converter warning" in w for w in res.warnings), command
    assert runs_of["diff"].summary["identical"] is True


def test_consent_and_backend_flags_reach_the_converter(
    fx: Path, runs: Path, dwg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import convert as convert_module

    fake = FakeEnsure(fx / "sheet_set.dxf")
    monkeypatch.setattr(convert_module, "ensure_dxf", fake)
    run_cmd("info", [str(dwg), "--allow-com"], runs)
    run_cmd("find", [str(dwg), "--pattern", "x", "--backend", "oda"], runs)
    run_cmd("dump", [str(dwg), "--backend", "com"], runs)
    assert [(c["allow_com"], c["prefer"]) for c in fake.calls] == [
        (True, None),
        (False, "oda"),
        (False, "com"),
    ]
    parser = argparse.ArgumentParser()
    dxf.COMMANDS["info"].add_arguments(parser)
    assert parser.parse_args(["x.dxf"]).timeout == 100.0


def test_approximate_conversion_is_flagged_on_every_result(
    fx: Path, runs: Path, dwg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import convert as convert_module

    fake = FakeEnsure(fx / "sheet_set.dxf", approximate=True)
    monkeypatch.setattr(convert_module, "ensure_dxf", fake)
    for res in (
        run_cmd("info", [str(dwg)], runs),
        run_cmd("find", [str(dwg), "--pattern", "FIRE"], runs),
        run_cmd("dump", [str(dwg)], runs),
        run_cmd("fingerprint", [str(dwg)], runs),
        run_cmd("diff", [str(dwg), str(fx / "sheet_set.dxf")], runs),
    ):
        assert res.approximate is True
        assert any("approximate" in w.lower() for w in res.warnings)


def test_no_backend_is_surfaced_unchanged(
    runs: Path, dwg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import convert as convert_module

    def refuse(src: Path, ctx: Any, **kwargs: Any) -> Any:
        raise CadError("NO_BACKEND", "no converter", hint="install ODA")

    monkeypatch.setattr(convert_module, "ensure_dxf", refuse)
    for command, argv in (
        ("info", [str(dwg)]),
        ("find", [str(dwg), "--pattern", "x"]),
        ("dump", [str(dwg)]),
        ("fingerprint", [str(dwg)]),
        ("diff", [str(dwg), str(dwg)]),
    ):
        with pytest.raises(CadError) as err:
            run_cmd(command, argv, runs)
        assert err.value.code == "NO_BACKEND" and err.value.exit_code == ExitCode.MISSING_DEPENDENCY


# --------------------------------------------------------------------------------------
# non-ASCII paths, every command
# --------------------------------------------------------------------------------------


def test_every_command_accepts_non_ascii_paths(fx: Path, tmp_path: Path, runs: Path) -> None:
    folder = tmp_path / NON_ASCII
    folder.mkdir()
    a, b = folder / "rysunek ą.dxf", folder / "rysunek ę.dxf"
    shutil.copy(fx / "sheet_set_v1.dxf", a)
    shutil.copy(fx / "plan_v2.dxf", b)
    assert run_cmd("info", [str(a)], runs).summary["layouts"] == ["Sheet-A", "Sheet-B"]
    assert (
        run_cmd("find", [str(a), str(b), "--pattern", "STORE|ARCHIVE"], runs).summary["hits"] == 2
    )
    assert run_cmd("dump", [str(a), "--type", "CIRCLE"], runs).summary["entities"] == 1
    out = folder / "odcisk ż.json"
    run_cmd("fingerprint", [str(a), "--out", str(out)], runs)
    assert out.is_file()
    assert run_cmd("diff", [str(out), str(b)], runs).summary["changed"] == 1


# --------------------------------------------------------------------------------------
# large fixture (slow)
# --------------------------------------------------------------------------------------

# Budget per command on the 30 000-entity fixture (reading the DXF included, about 2.4 s of it).
# Measured on the development machine (Windows, Python 3.13, ezdxf 1.4.4): info 1.2 s, find 1.1 s,
# fingerprint 4.5 s, i.e. roughly 0.4 / 0.4 / 1.5 s per 10 000 entities. Budgets are about 5x because CI machines are slower and noisier.
LARGE_BUDGET_S = {"info": 8.0, "find": 8.0, "fingerprint": 20.0}


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("CAD_DRAWINGS_RUN_SLOW") != "1", reason="set CAD_DRAWINGS_RUN_SLOW=1"
)
def test_large_fixture_performance(tmp_path: Path, runs: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "cad_make_fixtures_large", SKILL.parents[1] / "evals" / "make_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    sys.modules["cad_make_fixtures_large"] = gen
    spec.loader.exec_module(gen)
    gen.generate(tmp_path, large=True)
    big = str(tmp_path / "plan_large.dxf")
    timings: dict[str, float] = {}
    for name, argv in (
        ("info", [big]),
        ("find", [big, "--pattern", "ROOM|NOTE"]),
        ("fingerprint", [big]),
    ):
        started = time.monotonic()
        res = run_cmd(name, argv, runs)
        timings[name] = time.monotonic() - started
        assert res.exit_code == 0
    print("large fixture timings (s):", {k: round(v, 2) for k, v in timings.items()})
    for name, limit in LARGE_BUDGET_S.items():
        assert timings[name] < limit, f"{name} took {timings[name]:.1f}s (budget {limit}s)"
