"""render.py: ezdxf workarounds, PNG limits, crop/tiles geometry, PDF raster and a fake COM path."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ezdxf
import pytest
from PIL import Image
from typing_extensions import Self

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import render
from cadlib.result import CadError, ExitCode, Result

NON_ASCII = "zażółć gęślą jaźń"
LOW_DPI = "50"  # the fixtures are A3 sheets: 50 dpi gives ~830 x 585 px, enough for the checks


def run_render(argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = render.COMMANDS["render"]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def open_png(result: Result, key: str) -> Image.Image:
    return Image.open(result.outputs[key]["path"]).convert("RGB")


def pixels_of(image: Image.Image) -> list[tuple[int, int, int]]:
    flat = getattr(image, "get_flattened_data", None)  # getdata is deprecated in newer Pillow
    return list(flat() if flat is not None else image.getdata())


def region_pixels(
    image: Image.Image,
    box: tuple[float, float, float, float],
    area: tuple[float, float, float, float],
) -> list[tuple[int, int, int]]:
    """Pixels of ``area`` (layout coordinates, y up) in an image that covers ``box``."""
    left, top, right, bottom = render.crop_to_pixels(box, image.size, list(area))
    return pixels_of(image.crop((left, top, right, bottom)))


def inked(pixels: list[tuple[int, int, int]]) -> float:
    """Share of pixels that are not (near) white."""
    return sum(1 for p in pixels if min(p) < 235) / max(1, len(pixels))


def count_colour(
    pixels: list[tuple[int, int, int]], colour: tuple[int, int, int], tolerance: int = 90
) -> int:
    """Pixels close to ``colour`` (thin anti-aliased lines never reach the pure colour)."""
    return sum(
        1 for p in pixels if all(abs(a - b) < tolerance for a, b in zip(p, colour, strict=True))
    )


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture(scope="module")
def sheet(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sheet_set.dxf"


@pytest.fixture(scope="module")
def sheet_render(sheet: Path, tmp_path_factory: pytest.TempPathFactory) -> Result:
    """Both sheets rendered once; reused by the read-only checks."""
    base = tmp_path_factory.mktemp("render-runs")
    return run_render([str(sheet), "--dpi", LOW_DPI, "--max-px", "600"], base)


# Paper-space window of the viewport in both fixture sheets (see sheet_set.dxf truth).
VP_AREA = (20.0, 20.0, 275.0, 277.0)
BLANK_AREA = (300.0, 120.0, 400.0, 270.0)  # right of the viewport, above the title block
PAGE_BOX = (-10.0, -10.0, 410.0, 287.0)  # paper limits of the fixture layouts


# --------------------------------------------------------------------------------------
# the ezdxf render of the fixture sheets
# --------------------------------------------------------------------------------------


def test_render_names_sizes_and_result_fields(sheet_render: Result, sheet: Path) -> None:
    assert sheet_render.backend == "ezdxf" and sheet_render.approximate is True
    assert any("approximate render" in w for w in sheet_render.warnings)
    assert [m["name"] for m in sheet_render.summary["layouts"]] == ["Sheet-A", "Sheet-B"]
    for layout in ("Sheet-A", "Sheet-B"):
        out = sheet_render.outputs[layout]
        assert Path(out["path"]).name == f"sheet_set__{layout}.png"
        assert "source_mtime" in out and out["bytes"] > 1000
        image = open_png(sheet_render, layout)
        assert max(image.size) <= 600  # --max-px caps the longest side
        assert image.size[0] / image.size[1] == pytest.approx(420 / 297, abs=0.01)
    manifest = json.loads(Path(sheet_render.outputs["manifest"]["path"]).read_text("utf-8"))
    assert manifest["renders"][0]["master_px"][0] > 600  # master kept at --dpi, main capped


def test_viewport_content_lands_where_the_viewport_is(sheet_render: Result) -> None:
    for layout in ("Sheet-A", "Sheet-B"):
        image = open_png(sheet_render, layout)
        assert inked(region_pixels(image, PAGE_BOX, VP_AREA)) > 0.05, layout
        assert inked(region_pixels(image, PAGE_BOX, BLANK_AREA)) < 0.002, layout


def test_twisted_sheet_differs_and_frozen_layer_is_hidden_only_there(sheet_render: Result) -> None:
    magenta = (255, 0, 255)  # the layer frozen in Sheet-B's viewport only
    a = region_pixels(open_png(sheet_render, "Sheet-A"), PAGE_BOX, VP_AREA)
    b = region_pixels(open_png(sheet_render, "Sheet-B"), PAGE_BOX, VP_AREA)
    assert count_colour(a, magenta) > 30
    assert count_colour(b, magenta) == 0
    assert a != b
    assert not any("twisted" in w for w in sheet_render.warnings)  # the maths is exact now


# Wall mask (54 x 52 bits, packed, base64) of the viewport area of Sheet-B, taken from a real CAD
# plot of the same drawing (twist 90, centre stored in the display system). The ezdxf render must
# put the same content in the same place; thin lines and text are left out of the comparison.
CAD_PLOT_WALL_MASK = (
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAADAAAMAAAADAAAMAAAADAAAMAAAADAAAMAAAADAAAMAAAADAAAMAA"
    "AADAAAMAAAADAAAMAAAADAAAMAAAADAAAMAAAADAAAMAAAADAAAMAAAADAAAP/////AAAP/////AAAAGAAADAAAAGAAADAAA"
    "AMAAADAAAAMAAADAAAAYAAADAAAAYAAADAAAAQAAADAAAAgAAADAAABgAAADAAABgAAADAAADAAAADAAADAAAADAAAGAAAAD"
    "AAAGAAAAD///MAAAAD///MAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAA"
    "AADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAADwADMAAAAD"
)
MASK_AREA = (12.0, 17.0, 283.0, 279.0)  # paper area visible in both images


def wall_mask(image: Image.Image) -> Any:
    import numpy as np
    from PIL import ImageFilter

    left, top, right, bottom = render.crop_to_pixels(PAGE_BOX, image.size, list(MASK_AREA))
    grey = (
        image.convert("L").crop((left, top, right, bottom)).resize((270, 260), Image.Resampling.BOX)
    )
    walls = grey.point(lambda v: 255 if v < 110 else 0)
    opened = walls.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    small = opened.resize((54, 52), Image.Resampling.BOX)
    return np.asarray(small) > 127


def test_twisted_sheet_matches_a_real_cad_plot(sheet_render: Result) -> None:
    import base64

    import numpy as np

    reference = np.unpackbits(np.frombuffer(base64.b64decode(CAD_PLOT_WALL_MASK), dtype=np.uint8))
    reference = reference[: 54 * 52].reshape(52, 54).astype(bool)
    mine = wall_mask(open_png(sheet_render, "Sheet-B"))
    iou = (mine & reference).sum() / (mine | reference).sum()
    assert reference.sum() > 200 and iou > 0.85, iou
    # the untwisted sheet must not match: the check discriminates
    other = wall_mask(open_png(sheet_render, "Sheet-A"))
    assert (other & reference).sum() / (other | reference).sum() < 0.5


def test_no_stdout_noise_and_no_agpl_module(sheet: Path, runs: Path, capsys: Any) -> None:
    run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "30"], runs)
    assert capsys.readouterr().out == ""
    assert "ezdxf.addons.drawing.pymupdf" not in sys.modules
    assert "pymupdf" not in sys.modules or render.pymupdf_available()


def test_never_reuses_an_older_artifact(sheet: Path, runs: Path) -> None:
    first = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "30"], runs)
    second = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "30"], runs)
    assert first.outputs["Sheet-A"]["path"] != second.outputs["Sheet-A"]["path"]
    assert first.run_dir != second.run_dir
    assert Path(first.outputs["Sheet-A"]["path"]).is_file()
    assert second.outputs["Sheet-A"]["mtime"] >= second.outputs["Sheet-A"]["source_mtime"]


# --------------------------------------------------------------------------------------
# workaround 1: viewport normalisation
# --------------------------------------------------------------------------------------


def _layout_with_viewports(statuses: list[int], *, overall: bool) -> Any:
    doc = ezdxf.new("R2018")
    layout = doc.layouts.new("L")
    if overall:
        vp = layout.add_viewport((100, 100), (200, 200), (100, 100), 200, status=statuses.pop(0))
        vp.dxf.id = 1
    for i, status in enumerate(statuses):
        layout.add_viewport((50 + i, 50), (40, 40), (500, 500), 80, status=status)
    return layout


@pytest.mark.parametrize(
    ("statuses", "overall"),
    [([1, 0, -1, 3], True), ([1, 2], True), ([0, 0], True), ([1], False), ([0, 1, 2], False)],
)
def test_normalise_viewports_leaves_every_real_viewport_drawable(
    statuses: list[int], overall: bool
) -> None:
    layout = _layout_with_viewports(list(statuses), overall=overall)
    before = len(list(layout.query("VIEWPORT")))
    warnings = render.normalise_viewports(layout)
    vps = list(layout.query("VIEWPORT"))
    assert sorted(vp.dxf.status for vp in vps if vp.dxf.id == 1) == [1]  # exactly one overall
    real = sorted(vp.dxf.status for vp in vps if vp.dxf.id != 1)
    assert real == list(range(2, 2 + len(real)))  # all > 1, so ezdxf keeps every one of them
    assert len(real) == before - (1 if overall else 0)
    assert any("not reliable" in w for w in warnings) == any(
        s <= 0 for s in statuses[(1 if overall else 0) :]
    )
    if not overall:
        assert any("placeholder" in w for w in warnings)


def test_overall_viewport_found_by_geometry_when_id_is_wrong() -> None:
    doc = ezdxf.new("R2018")
    layout = doc.layouts.new("L")
    overall = layout.add_viewport((100, 100), (200, 200), (100, 100), 200, status=1)
    overall.dxf.id = 5  # not trusted: the centre/height heuristic still identifies it
    real = layout.add_viewport((50, 50), (40, 40), (500, 500), 80, status=0)
    render.normalise_viewports(layout)
    assert (overall.dxf.status, real.dxf.status) == (1, 2)


def test_status_zero_file_renders_content_only_with_normalisation(
    sheet: Path, runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = ezdxf.readfile(sheet)
    for name in ("Sheet-A", "Sheet-B"):
        for vp in doc.layouts.get(name).query("VIEWPORT"):
            vp.dxf.status = 0 if vp.dxf.id > 1 else 1
    broken = tmp_path / "status0.dxf"
    doc.saveas(broken)
    res = run_render([str(broken), "--layout", "Sheet-A", "--dpi", LOW_DPI], runs)
    image = open_png(res, "Sheet-A")
    assert inked(region_pixels(image, PAGE_BOX, VP_AREA)) > 0.05
    assert any("not reliable" in w for w in res.warnings)
    # the trap, shown: without normalisation ezdxf leaves the viewport empty
    monkeypatch.setattr(render, "normalise_viewports", lambda layout: [])
    raw = run_render([str(broken), "--layout", "Sheet-A", "--dpi", LOW_DPI], runs)
    assert inked(region_pixels(open_png(raw, "Sheet-A"), PAGE_BOX, VP_AREA)) < 0.002


# --------------------------------------------------------------------------------------
# workarounds 2-5, 6, 7
# --------------------------------------------------------------------------------------


def test_ctb_applied_only_when_given(sheet: Path, runs: Path, tmp_path: Path) -> None:
    from ezdxf.addons import acadctb

    plain = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", LOW_DPI], runs)
    blue = (0, 0, 255)
    assert count_colour(region_pixels(open_png(plain, "Sheet-A"), PAGE_BOX, VP_AREA), blue) == 0
    ctb = acadctb.new_ctb()
    for aci in range(1, 256):
        ctb[aci].color = blue
    ctb_path = tmp_path / "all-blue.ctb"
    ctb.save(str(ctb_path))
    styled = run_render(
        [str(sheet), "--layout", "Sheet-A", "--dpi", LOW_DPI, "--ctb", str(ctb_path)], runs
    )
    pixels = region_pixels(open_png(styled, "Sheet-A"), PAGE_BOX, VP_AREA)
    assert count_colour(pixels, blue) > 100
    assert count_colour(pixels, (255, 0, 255)) == 0  # the layer colours are gone
    with pytest.raises(CadError) as err:
        run_render([str(sheet), "--ctb", str(tmp_path / "missing.ctb")], runs)
    assert err.value.code == "BAD_ARGS"


def test_named_plot_style_without_ctb_is_reported(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0, 0), (10, 10))
    layout = doc.layouts.new("L")
    layout.set_plot_style("named.ctb")
    layout.add_viewport((100, 100), (100, 100), (5, 5), 20)
    path = tmp_path / "styled.dxf"
    doc.saveas(path)
    res = run_render([str(path), "--dpi", "30"], runs)
    assert any("plot style" in w and "named.ctb" in w for w in res.warnings)


def test_support_dirs_and_shx_order_are_applied_then_restored(tmp_path: Path) -> None:
    before_dirs = list(ezdxf.options.support_dirs)
    before_order = ezdxf.options.get("drawing-addon", "shx_resolve_order", "tsl")
    with render._ezdxf_options([str(tmp_path)]):
        assert str(tmp_path) in ezdxf.options.support_dirs
        assert ezdxf.options.get("drawing-addon", "shx_resolve_order", "") == "sl"
    assert list(ezdxf.options.support_dirs) == before_dirs
    assert ezdxf.options.get("drawing-addon", "shx_resolve_order", "tsl") in (before_order, "tsl")
    with render._ezdxf_options([]):
        assert list(ezdxf.options.support_dirs) == before_dirs


def test_lineweight_scaling_changes_ink(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    for i in range(5):
        doc.modelspace().add_line((0, i * 10), (100, i * 10), dxfattribs={"lineweight": 100})
    path = tmp_path / "weights.dxf"
    doc.saveas(path)
    ink = {}
    for scaling in ("0.2", "3"):
        res = run_render(
            [str(path), "--layout", "Model", "--dpi", "100", "--lineweight-scaling", scaling], runs
        )
        ink[scaling] = inked(pixels_of(open_png(res, "Model")))
    assert ink["3"] > ink["0.2"]


def test_one_to_one_geometry_follows_the_paper_limits(sheet: Path, runs: Path) -> None:
    res = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "100", "--max-px", "5000"], runs)
    manifest = json.loads(Path(res.outputs["manifest"]["path"]).read_text("utf-8"))["renders"][0]
    assert manifest["box"] == [-10.0, -10.0, 410.0, 287.0]
    width_px, height_px = manifest["master_px"]
    assert width_px == pytest.approx(420 / 25.4 * 100, abs=1.5)
    assert height_px == pytest.approx(297 / 25.4 * 100, abs=1.5)
    # no fitting: a 100 mm paper distance is exactly 100/25.4*dpi pixels, wherever it is
    image = open_png(res, "Sheet-A")
    cols = [
        x
        for x in range(image.width)
        if image.getpixel((x, image.height // 2 + 80)) != (255, 255, 255)
    ]
    assert cols  # content crosses that scan line
    assert manifest["dpi"] == 100


def test_hide_layer_removes_content_inside_viewports(sheet: Path, runs: Path) -> None:
    magenta = (255, 0, 255)
    shown = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", LOW_DPI], runs)
    hidden = run_render(
        [str(sheet), "--layout", "Sheet-A", "--dpi", LOW_DPI, "--hide-layer", "vp-frozen"], runs
    )
    a = region_pixels(open_png(shown, "Sheet-A"), PAGE_BOX, VP_AREA)
    b = region_pixels(open_png(hidden, "Sheet-A"), PAGE_BOX, VP_AREA)
    assert count_colour(a, magenta) > 30 and count_colour(b, magenta) == 0
    assert inked(b) > 0.05  # the rest of the plan is still there


def _write_xref_chain(folder: Path) -> Path:
    sub = folder / "refs"
    sub.mkdir()
    leaf = ezdxf.new("R2018")
    leaf.modelspace().add_circle((500, 500), 300, dxfattribs={"color": 1})
    leaf.saveas(sub / "leaf.dxf")
    mid = ezdxf.new("R2018")
    mid.add_xref_def(filename="leaf.dxf", name="LEAF")
    mid.modelspace().add_blockref("LEAF", (0, 0))
    mid.modelspace().add_line((0, 500), (1000, 500), dxfattribs={"color": 3})
    mid.saveas(sub / "mid.dxf")
    host = ezdxf.new("R2018")
    host.add_xref_def(filename="refs\\mid.dxf", name="MID")  # backslash as written by Windows CAD
    host.modelspace().add_blockref("MID", (0, 0))
    path = folder / "host.dxf"
    host.saveas(path)
    return path


def test_xrefs_are_embedded_with_backslash_paths_and_nesting(tmp_path: Path, runs: Path) -> None:
    host = _write_xref_chain(tmp_path)
    res = run_render([str(host), "--layout", "Model", "--dpi", "30", "--max-px", "400"], runs)
    assert not [w for w in res.warnings if "xref" in w], res.warnings
    pixels = pixels_of(open_png(res, "Model"))
    assert count_colour(pixels, (255, 0, 0)) > 20  # the circle of the nested reference
    assert count_colour(pixels, (0, 255, 0)) > 20  # the line of the first-level reference


def test_missing_xref_still_renders_with_a_warning(fixtures_dir: Path, runs: Path) -> None:
    res = run_render(
        [str(fixtures_dir / "xref_missing.dxf"), "--layout", "Model", "--max-px", "300"], runs
    )
    assert any("xref" in w and "not found" in w for w in res.warnings)
    assert open_png(res, "Model").size[0] > 10 and res.exit_code == ExitCode.OK


def test_embed_xrefs_reports_dwg_references(tmp_path: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.add_xref_def(filename="other.dwg", name="OTHER")
    warnings = render.embed_xrefs(doc, tmp_path)
    assert len(warnings) == 1 and "DWG" in warnings[0]


# --------------------------------------------------------------------------------------
# crop and tiles
# --------------------------------------------------------------------------------------


def test_crop_and_tile_geometry_helpers() -> None:
    box = (0.0, 0.0, 100.0, 50.0)
    assert render.crop_to_pixels(box, (1000, 500), [0, 0, 50, 25]) == (0, 250, 500, 500)
    assert render.crop_to_pixels(box, (1000, 500), [50, 25, 0, 0]) == (0, 250, 500, 500)
    assert render.crop_to_pixels(box, (1000, 500), [-50, -50, 200, 200]) == (0, 0, 1000, 500)
    with pytest.raises(CadError):
        render.crop_to_pixels(box, (1000, 500), [200, 200, 300, 300])
    tiles = list(render.tile_boxes((0, 0, 1000, 500), 3, 2))
    assert [(c, r) for c, r, _ in tiles] == [(1, 1), (2, 1), (3, 1), (1, 2), (2, 2), (3, 2)]
    covered = sum((b[2] - b[0]) * (b[3] - b[1]) for _, _, b in tiles)
    assert covered == 1000 * 500  # no gaps, no overlaps
    assert tiles[0][2] == (0, 0, 333, 250) and tiles[-1][2] == (667, 250, 1000, 500)
    assert render.parse_tiles("3x2") == (3, 2) and render.parse_tiles("2X2") == (2, 2)
    for bad in ("3", "0x2", "axb", "20x20"):
        with pytest.raises(CadError):
            render.parse_tiles(bad)


def test_cap_longest_side_never_upscales() -> None:
    image = Image.new("RGB", (400, 200), "white")
    assert render.cap_longest_side(image, 1000) is image
    assert render.cap_longest_side(image, 100).size == (100, 50)


def test_crop_and_tiles_are_written_at_master_resolution(sheet: Path, runs: Path) -> None:
    res = run_render(
        [str(sheet), "--layout", "Sheet-A", "--dpi", "100", "--max-px", "500",
         "--crop", "20,20,275,277", "--tiles", "2x2"],
        runs,
    )  # fmt: skip
    manifest = json.loads(Path(res.outputs["manifest"]["path"]).read_text("utf-8"))["renders"][0]
    main = open_png(res, "Sheet-A")
    assert max(main.size) == 500 and max(manifest["master_px"]) > 1500
    extras = {(e["kind"], e.get("col"), e.get("row")): e for e in manifest["extras"]}
    assert set(extras) == {
        ("crop", None, None),
        ("tile", 1, 1),
        ("tile", 2, 1),
        ("tile", 1, 2),
        ("tile", 2, 2),
    }
    crop_w, crop_h = extras[("crop", None, None)]["size_px"]
    assert crop_w == pytest.approx(255 / 25.4 * 100, abs=2) and crop_h == pytest.approx(
        257 / 25.4 * 100, abs=2
    )
    assert crop_w > main.width * 0.9  # full resolution of the master, not of the capped PNG
    tile_w = [extras[("tile", c, 1)]["size_px"][0] for c in (1, 2)]
    assert sum(tile_w) == crop_w
    names = sorted(Path(e["path"]).name for e in manifest["extras"])
    assert names[0] == "sheet_set__Sheet-A__crop.png"
    assert "sheet_set__Sheet-A__tile-2-1.png" in names
    for entry in manifest["extras"]:
        assert Image.open(entry["path"]).size == tuple(entry["size_px"])
    assert res.summary["extra_images"] == 5


def test_tiles_without_crop_cover_the_whole_page(sheet: Path, runs: Path) -> None:
    res = run_render([str(sheet), "--layout", "Sheet-B", "--dpi", "40", "--tiles", "3x1"], runs)
    manifest = json.loads(Path(res.outputs["manifest"]["path"]).read_text("utf-8"))["renders"][0]
    widths = [e["size_px"][0] for e in manifest["extras"]]
    assert len(widths) == 3 and sum(widths) == manifest["master_px"][0]


def test_bad_arguments(sheet: Path, runs: Path) -> None:
    for argv in (["--crop", "1,2,3"], ["--tiles", "x"], ["--dpi", "5"]):
        with pytest.raises(CadError) as err:
            run_render([str(sheet), *argv], runs)
        assert err.value.code == "BAD_ARGS", argv
    with pytest.raises(CadError) as err:
        run_render([str(sheet), "--layout", "Nope"], runs)
    assert err.value.code == "LAYOUT_NOT_FOUND" and err.value.exit_code == ExitCode.BAD_ARGS
    assert "Sheet-A" in (err.value.hint or "")
    with pytest.raises(CadError) as err2:
        run_render([str(sheet.parent / "missing.dxf")], runs)
    assert err2.value.code == "FILE_NOT_FOUND" and err2.value.exit_code == ExitCode.BAD_ARGS


def test_model_space_render_and_empty_model(fixtures_dir: Path, tmp_path: Path, runs: Path) -> None:
    res = run_render([str(fixtures_dir / "plan_cm_v1.dxf"), "--max-px", "400"], runs)
    assert next(iter(res.outputs)) == "Model"  # its only paper layout is empty and skipped
    assert any("skipped empty layout" in w for w in res.warnings)
    assert max(open_png(res, "Model").size) == 400
    empty = tmp_path / "empty.dxf"
    ezdxf.new("R2018").saveas(empty)
    with pytest.raises(CadError) as err:
        run_render([str(empty)], runs)
    assert err.value.code == "EMPTY_LAYOUT"


def test_non_ascii_path_and_layout_name_in_png(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.modelspace().add_circle((0, 0), 5)
    layout = doc.layouts.new("Arkusz ą 1")
    layout.add_viewport((100, 100), (100, 100), (0, 0), 20)
    folder = tmp_path / NON_ASCII
    folder.mkdir()
    path = folder / "rysunek ż.dxf"
    doc.saveas(path)
    res = run_render([str(path), "--dpi", "30"], runs)
    name = Path(res.outputs["Arkusz ą 1"]["path"]).name
    assert name == "rysunek ż__Arkusz ą 1.png"
    assert render.png_name("s", 'a/b:c*d?"e') == "s__a_b_c_d_e.png"
    assert render.png_name("s", "///") == "s___.png"


# --------------------------------------------------------------------------------------
# PDF -> PNG and the COM path (fake session)
# --------------------------------------------------------------------------------------


def _make_pdf(path: Path, width_mm: float = 297.0, height_mm: float = 210.0) -> None:
    from matplotlib.figure import Figure

    figure = Figure(figsize=(width_mm / 25.4, height_mm / 25.4))
    axes = figure.add_axes((0.1, 0.1, 0.5, 0.5))
    axes.set_facecolor("black")
    figure.savefig(path, format="pdf")


def test_pdf_to_image_uses_pypdfium2_with_the_right_scale(tmp_path: Path) -> None:
    pdf = tmp_path / "page.pdf"
    _make_pdf(pdf)
    image = render.pdf_to_image(pdf, 72)
    assert image.size == (
        pytest.approx(297 / 25.4 * 72, abs=1.5),
        pytest.approx(210 / 25.4 * 72, abs=1.5),
    )
    assert inked(pixels_of(image)) > 0.1
    assert render.pdf_to_image(pdf, 144).width == pytest.approx(image.width * 2, abs=2)


def test_pymupdf_is_never_imported_unless_asked_and_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    assert render.pymupdf_available() == (importlib.util.find_spec("pymupdf") is not None)
    pdf = tmp_path / "page.pdf"
    _make_pdf(pdf)
    monkeypatch.setattr(render, "pymupdf_available", lambda: False)
    with pytest.raises(CadError) as err:
        render.pdf_to_image(pdf, 72, raster="pymupdf")
    assert err.value.exit_code == ExitCode.MISSING_DEPENDENCY


class FakeSession:
    """Stands in for ``acad.AcadSession``: writes a PDF per plotted layout, starts nothing."""

    def __init__(self) -> None:
        self.plotted: list[tuple[Path, str, Path]] = []
        self.warnings: list[str] = ["session warning"]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def plot_layout_pdf(
        self, src: Path, layout: str, dst: Path, *, page_setup: Any = None
    ) -> list[str]:
        self.plotted.append((src, layout, dst))
        _make_pdf(dst)
        return [f"VIEWER_WARNING: close the PDF viewer ({layout})"]


@contextlib.contextmanager
def _patched_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> Iterator[None]:
    monkeypatch.setattr(render, "_com_session", lambda ctx=None: session)
    monkeypatch.setattr(render, "_com_candidate", lambda: True)
    yield


def test_com_backend_plots_each_layout_and_rasterises(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession()
    with _patched_session(monkeypatch, session):
        res = run_render([str(sheet), "--backend", "com", "--dpi", "72", "--max-px", "500"], runs)
    assert [(p[1]) for p in session.plotted] == ["Sheet-A", "Sheet-B"]
    # CAD works on a staged copy inside the run directory, never on the original
    assert all(p[0] != sheet.resolve() and p[0].name == sheet.name for p in session.plotted)
    assert all(Path(res.run_dir) in p[0].parents for p in session.plotted)
    assert all(p[2].suffix == ".pdf" for p in session.plotted)
    # the plot's own warnings and the session's warnings reach the user
    assert any("VIEWER_WARNING" in w and "Sheet-A" in w for w in res.warnings)
    assert "session warning" in res.warnings
    assert res.backend == "com" and res.approximate is None
    assert not any("approximate render" in w for w in res.warnings)
    for layout in ("Sheet-A", "Sheet-B"):
        out = res.outputs[layout]
        assert "source_mtime" in out and Path(out["path"]).name == f"sheet_set__{layout}.png"
        assert max(open_png(res, layout).size) == 500


def test_com_crop_is_mapped_through_the_paper_limits(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _patched_session(monkeypatch, FakeSession()):
        res = run_render(
            [str(sheet), "--backend", "com", "--layout", "Sheet-A", "--dpi", "72",
             "--crop", "0,0,200,100"],
            runs,
        )  # fmt: skip
    assert any("plot area equals the paper limits" in w for w in res.warnings)
    manifest = json.loads(Path(res.outputs["manifest"]["path"]).read_text("utf-8"))["renders"][0]
    assert manifest["extras"][0]["kind"] == "crop"


def test_auto_prefers_ezdxf_unless_com_is_allowed(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession()
    with _patched_session(monkeypatch, session):
        plain = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "30"], runs)
        assert plain.backend == "ezdxf" and not session.plotted
        assert any("--backend com" in n for n in plain.next)
        allowed = run_render(
            [str(sheet), "--layout", "Sheet-A", "--allow-com", "--dpi", "30"], runs
        )
        assert allowed.backend == "com" and len(session.plotted) == 1


def test_com_unavailable_is_an_error_when_explicit_and_a_fallback_when_auto(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ctx: Any = None) -> Any:
        raise CadError(
            "NO_BACKEND", "no CAD found", exit_code=ExitCode.MISSING_DEPENDENCY, hint="use ezdxf"
        )

    monkeypatch.setattr(render, "_com_session", broken)
    monkeypatch.setattr(render, "_com_candidate", lambda: True)
    with pytest.raises(CadError) as err:
        run_render([str(sheet), "--backend", "com", "--layout", "Sheet-A"], runs)
    assert err.value.code == "NO_BACKEND" and err.value.exit_code == ExitCode.MISSING_DEPENDENCY
    res = run_render([str(sheet), "--allow-com", "--layout", "Sheet-A", "--dpi", "30"], runs)
    assert res.backend == "ezdxf" and any("fell back to ezdxf" in w for w in res.warnings)


def test_many_layouts_keep_the_result_small(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.modelspace().add_circle((0, 0), 5)
    for i in range(render.MAX_LISTED_OUTPUTS + 3):
        doc.layouts.new(f"L{i:02d}").add_viewport((100, 100), (100, 100), (0, 0), 20)
    path = tmp_path / "many.dxf"
    doc.saveas(path)
    res = run_render([str(path), "--dpi", "20"], runs)
    assert (
        res.summary["count"] == render.MAX_LISTED_OUTPUTS + 3
    )  # the default Layout1 is empty, skipped
    assert len(res.to_json().encode("utf-8")) <= 4096
    manifest = json.loads(Path(res.outputs["manifest"]["path"]).read_text("utf-8"))
    assert len(manifest["renders"]) == render.MAX_LISTED_OUTPUTS + 3


# --------------------------------------------------------------------------------------
# hardening: consent, lazy imports, names, memory, atomic writes
# --------------------------------------------------------------------------------------


def test_a_run_directory_exists_even_when_the_arguments_are_bad(sheet: Path, runs: Path) -> None:
    with pytest.raises(CadError):
        run_render([str(sheet), "--crop", "1,2"], runs)
    assert len(list(runs.glob("*-render-*"))) == 1


def test_dwg_input_goes_through_ensure_dxf_with_the_shared_consent(
    sheet: Path, runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from cadlib import convert as convert_module

    calls: list[dict[str, Any]] = []

    def fake(src: Path, ctx: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            path=sheet,
            backend="libredwg",
            approximate=True,
            cached=False,
            warnings=["from convert"],
        )

    monkeypatch.setattr(convert_module, "ensure_dxf", fake)
    monkeypatch.setattr(render, "_com_candidate", lambda: False)  # never start real CAD
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(b"AC1032")
    res = run_render([str(dwg), "--layout", "Sheet-A", "--dpi", "30"], runs)
    assert calls == [{"allow_com": False, "prefer": None}]
    assert "from convert" in res.warnings and any("approximate" in w.lower() for w in res.warnings)
    run_render([str(dwg), "--layout", "Sheet-A", "--dpi", "30", "--convert-with", "oda"], runs)
    run_render([str(dwg), "--layout", "Sheet-A", "--dpi", "30", "--allow-com"], runs)
    assert calls[1:] == [
        {"allow_com": False, "prefer": "oda"},
        {"allow_com": True, "prefer": None},
    ]


@pytest.mark.parametrize(
    ("module", "needle"),
    [("matplotlib", "matplotlib"), ("PIL", "Pillow")],
)
def test_missing_packages_give_a_clean_dependency_error(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch, module: str, needle: str
) -> None:
    monkeypatch.setitem(sys.modules, module, None)  # makes "import <module>" raise ImportError
    if module == "matplotlib":
        for name in list(sys.modules):
            if name.startswith(("matplotlib.", "ezdxf.addons.drawing.matplotlib")):
                monkeypatch.delitem(sys.modules, name)
    with pytest.raises(CadError) as err:
        run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "30"], runs)
    assert (
        err.value.code == "MISSING_DEPENDENCY"
        and err.value.exit_code == ExitCode.MISSING_DEPENDENCY
    )
    assert needle in err.value.message and "pip install" in (err.value.hint or "")


def test_missing_pdf_rasteriser_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "page.pdf"
    _make_pdf(pdf)
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    monkeypatch.setitem(sys.modules, "pdf2image", None)
    with pytest.raises(CadError) as err:
        render.pdf_to_image(pdf, 72)
    assert err.value.code == "MISSING_DEPENDENCY" and "pypdfium2" in (err.value.hint or "")


def test_layout_names_that_sanitise_alike_get_distinct_files() -> None:
    slugs = render.assign_slugs(["A/B", "A:B", "A_B", "a_b", "Other"])
    assert slugs == {"A/B": "A_B", "A:B": "A_B-2", "A_B": "A_B-3", "a_b": "a_b-4", "Other": "Other"}
    assert len({render.png_name("s", v) for v in slugs.values()}) == 5
    assert render.assign_slugs(["x"]) == {"x": "x"}


def test_master_size_cap_lowers_the_resolution(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert render.MAX_MASTER_PIXELS == 50_000_000
    monkeypatch.setattr(render, "MAX_MASTER_PIXELS", 200_000)
    res = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "200"], runs)
    manifest = json.loads(Path(res.outputs["manifest"]["path"]).read_text("utf-8"))["renders"][0]
    width, height = manifest["master_px"]
    assert width * height <= 200_000 * 1.02 and manifest["dpi"] < 200
    assert any("size cap" in w for w in res.warnings)


def test_pngs_are_written_atomically(
    sheet: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from cadlib import util

    replaced: list[str] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        replaced.append(Path(dst).suffix)
        real(src, dst)

    monkeypatch.setattr(util.os, "replace", spy)
    res = run_render([str(sheet), "--layout", "Sheet-A", "--dpi", "30", "--tiles", "2x1"], runs)
    assert replaced.count(".png") >= 3  # main image and two tiles
    assert not list(Path(res.run_dir).glob(".*.tmp"))


def test_xref_on_a_network_path_is_skipped_without_touching_it(
    tmp_path: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cadlib import util

    doc = ezdxf.new("R2018")
    doc.add_xref_def(filename="\\\\fileserver\\share\\site.dxf", name="NET")
    doc.modelspace().add_circle((0, 0), 5)
    path = tmp_path / "net.dxf"
    doc.saveas(path)
    touched: list[str] = []
    real = Path.is_file

    def spy(self: Path, *args: Any, **kwargs: Any) -> bool:
        touched.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", spy)
    assert not util.path_is_local("\\\\fileserver\\share\\site.dxf")
    res = run_render([str(path), "--layout", "Model", "--max-px", "200"], runs)
    assert not [t for t in touched if "fileserver" in t]
    assert any("NET" in w and "not checked" in w for w in res.warnings)
