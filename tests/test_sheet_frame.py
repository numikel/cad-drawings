"""sheet_frame: page setup boxes, frame detection and text estimates, without the qa command."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import ezdxf
import pytest
from ezdxf.entities.mtext import MTextColumns

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import sheet_frame as sf

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def sheet(
    *,
    size: tuple[float, float] = (420, 297),
    margins: tuple[float, float, float, float] = (0, 0, 0, 0),
    units: str = "mm",
    offset: tuple[float, float] = (0, 0),
) -> tuple[Any, Any]:
    doc = ezdxf.new("R2018", setup=True)
    layout = doc.layouts.new("S")
    layout.page_setup(size=size, margins=margins, units=units, offset=offset)
    return doc, layout


def rect(layout: Any, box: tuple[float, float, float, float], layer: str = "0") -> Any:
    x1, y1, x2, y2 = box
    return layout.add_lwpolyline(
        [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], close=True, dxfattribs={"layer": layer}
    )


def setup_of(layout: Any) -> sf.PaperSetup:
    setup = sf.paper_setup(layout)
    assert setup is not None
    return setup


# --------------------------------------------------------------------------------------
# page setup
# --------------------------------------------------------------------------------------


def test_printable_area_follows_each_margin_separately() -> None:
    _, layout = sheet(margins=(5, 6, 7, 20))  # top, right, bottom, left
    setup = setup_of(layout)
    assert setup.paper_box == pytest.approx((-20, -7, 400, 290))
    assert setup.printable_box == pytest.approx((0, 0, 394, 285))
    assert setup.margins_mm == pytest.approx((20, 7, 6, 5))  # left, bottom, right, top


def test_plot_origin_offset_moves_the_layout_not_the_printable_area() -> None:
    _, layout = sheet(margins=(5, 5, 5, 5), offset=(3, 4))
    setup = setup_of(layout)
    assert setup.printable_box == pytest.approx((-3, -4, 407, 283))
    assert setup.page_box((0, 0, 10, 10)) == pytest.approx((8, 9, 18, 19))


def test_inch_layouts_are_converted_to_millimetres() -> None:
    _, layout = sheet(size=(11, 8.5), margins=(0.25, 0.25, 0.25, 0.25), units="inch")
    setup = setup_of(layout)
    assert setup.unit_mm == pytest.approx(25.4)
    assert setup.paper_mm == pytest.approx((279.4, 215.9))
    assert setup.printable_box == pytest.approx((0, 0, 10.5, 8.0))
    assert setup.page_box((0, 0, 1, 1)) == pytest.approx((6.35, 6.35, 31.75, 31.75))
    over = sf.overshoot_mm((0, 0, 10.5, 8.1), setup.printable_box, 25.4)
    assert over == pytest.approx({"top": 2.54})


def test_unusable_limits_fall_back_to_the_paper_size() -> None:
    _, layout = sheet()
    layout.dxf.limmin = (0, 0)
    layout.dxf.limmax = (0, 0)
    setup = setup_of(layout)
    assert setup.limits_missing and setup.paper_box == pytest.approx((0, 0, 420, 297))
    assert setup.outside_skip_reason() is not None


def test_no_paper_size_at_all_gives_no_setup() -> None:
    _, layout = sheet()
    layout.dxf.limmin = (0, 0)
    layout.dxf.limmax = (0, 0)
    layout.dxf.paper_width = 0
    layout.dxf.paper_height = 0
    assert sf.paper_setup(layout) is None


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"plot_rotation": 1}, "rotated"),
        ({"plot_type": 1}, "plot area"),
        ({"scale_numerator": 1.0, "scale_denominator": 2.0}, "scale"),
        ({"plot_layout_flags": 688 | 4}, "centered"),
        ({"plot_paper_units": 2}, "pixels"),
    ],
)
def test_position_checks_are_skipped_with_a_reason(change: dict[str, Any], word: str) -> None:
    _, layout = sheet()
    for key, value in change.items():
        layout.dxf.set(key, value)
    reason = sf.paper_setup(layout).position_skip_reason()  # type: ignore[union-attr]
    assert reason is not None and word in reason


def test_a_plain_layout_has_no_skip_reason() -> None:
    _, layout = sheet()
    setup = setup_of(layout)
    assert setup.position_skip_reason() is None and setup.outside_skip_reason() is None


def test_scaled_to_fit_is_not_a_one_to_one_plot() -> None:
    _, layout = sheet()
    layout.dxf.plot_layout_flags = 688 | 16
    layout.dxf.standard_scale_type = 0
    assert "scale" in (sf.paper_setup(layout).position_skip_reason() or "")  # type: ignore[union-attr]


# --------------------------------------------------------------------------------------
# frame detection
# --------------------------------------------------------------------------------------


def test_a_rectangle_with_a_bulge_is_not_a_frame() -> None:
    _, layout = sheet()
    layout.add_lwpolyline(
        [(10, 10, 0, 0, 0.2), (410, 10, 0, 0, 0), (410, 287, 0, 0, 0), (10, 287, 0, 0, 0)],
        format="xyseb",
        close=True,
    )
    assert sf.find_frame(layout, setup_of(layout)) is None


def test_a_tilted_rectangle_is_not_a_frame() -> None:
    _, layout = sheet()
    layout.add_lwpolyline([(10, 10), (410, 20), (410, 297), (10, 287)], close=True)
    assert sf.find_frame(layout, setup_of(layout)) is None


def test_an_open_four_point_polyline_is_not_a_frame() -> None:
    _, layout = sheet()
    layout.add_lwpolyline([(10, 10), (410, 10), (410, 287), (10, 287)])
    assert sf.find_frame(layout, setup_of(layout)) is None


def test_a_five_point_polyline_that_returns_to_its_start_is_a_frame() -> None:
    _, layout = sheet()
    layout.add_lwpolyline([(10, 10), (410, 10), (410, 287), (10, 287), (10, 10)])
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.box == pytest.approx((10, 10, 410, 287))


def test_an_old_style_polyline_is_a_frame() -> None:
    _, layout = sheet()
    layout.add_polyline2d([(10, 10), (410, 10), (410, 287), (10, 287)], close=True)
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.box == pytest.approx((10, 10, 410, 287))


def test_four_lines_with_a_wide_gap_are_not_a_frame() -> None:
    _, layout = sheet()
    for a, b in [((10, 10), (410, 10)), ((410, 10), (410, 287)), ((410, 287), (10, 287))]:
        layout.add_line(a, b)
    layout.add_line((10, 287), (10, 100))  # stops 90 mm short
    assert sf.find_frame(layout, setup_of(layout)) is None


def test_frame_in_a_nested_block_is_found_with_the_outer_insert_as_handle() -> None:
    doc, layout = sheet()
    inner = doc.blocks.new("INNER")
    rect(inner, (0, 0, 400, 277))
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (10, 10))
    ref = layout.add_blockref("OUTER", (0, 0))
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.box == pytest.approx((10, 10, 410, 287))
    assert frame.handle == ref.dxf.handle


def test_a_scaled_and_moved_block_frame_is_found_where_it_lands() -> None:
    doc, layout = sheet()
    blk = doc.blocks.new("UNIT")
    rect(blk, (0, 0, 1, 1))
    layout.add_blockref("UNIT", (10, 10), dxfattribs={"xscale": 400, "yscale": 277})
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.box == pytest.approx((10, 10, 410, 287))


def test_a_minsert_array_is_not_expanded() -> None:
    doc, layout = sheet()
    blk = doc.blocks.new("BORDER")
    rect(blk, (0, 0, 400, 277))
    array = layout.add_blockref("BORDER", (10, 10))
    array.dxf.column_count = 2
    array.dxf.column_spacing = 500
    assert sf.find_frame(layout, setup_of(layout)) is None


def test_a_frame_on_a_hidden_layer_is_ignored() -> None:
    doc, layout = sheet()
    doc.layers.add("OFF").off()
    rect(layout, (10, 10, 410, 287), layer="OFF")
    unprintable = sf.unprintable_layers(doc.layers)
    assert sf.find_frame(layout, setup_of(layout), unprintable=unprintable) is None
    assert sf.find_frame(layout, setup_of(layout)) is not None


def test_polyline_wins_a_tie_with_four_lines() -> None:
    _, layout = sheet()
    for a, b in [((10, 10), (410, 10)), ((410, 10), (410, 287)), ((410, 287), (10, 287))]:
        layout.add_line(a, b)
    layout.add_line((10, 287), (10, 10))
    poly = rect(layout, (10, 10, 410, 287))
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.kind == "polyline" and frame.handle == poly.dxf.handle


def test_the_larger_of_two_frames_wins() -> None:
    _, layout = sheet()
    rect(layout, (10, 10, 410, 287))
    rect(layout, (30, 30, 390, 267))
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.box == pytest.approx((10, 10, 410, 287))


def test_the_paper_outline_alone_is_still_a_frame() -> None:
    _, layout = sheet()
    rect(layout, (0, 0, 420, 297))
    frame = sf.find_frame(layout, setup_of(layout))
    assert frame is not None and frame.box == pytest.approx((0, 0, 420, 297))


def test_frame_layer_accepts_a_small_rectangle_but_the_heuristic_does_not() -> None:
    doc, layout = sheet()
    doc.layers.add("TB")
    rect(layout, (20, 20, 120, 70), layer="TB")
    assert sf.find_frame(layout, setup_of(layout)) is None
    frame = sf.find_frame(layout, setup_of(layout), frame_layer="TB")
    assert frame is not None and frame.layer == "TB"


# --------------------------------------------------------------------------------------
# text estimates
# --------------------------------------------------------------------------------------


def test_mtext_lines_count_paragraphs_and_wrapping() -> None:
    _, layout = sheet()
    two = layout.add_mtext(r"first\Psecond", dxfattribs={"insert": (0, 0), "char_height": 3})
    assert sf.mtext_lines(two) == 2
    narrow = layout.add_mtext(
        "word " * 40, dxfattribs={"insert": (0, 0), "char_height": 3, "width": 40}
    )
    assert sf.mtext_lines(narrow) > 2
    free = layout.add_mtext("word " * 40, dxfattribs={"insert": (0, 0), "char_height": 3})
    assert sf.mtext_lines(free) == 1


def test_an_mtext_estimate_ignores_the_stale_stored_rectangle() -> None:
    """After an edit the stored rect_width/rect_height still describe the old text."""
    _, layout = sheet()
    mtext = layout.add_mtext("x", dxfattribs={"insert": (100, 200), "char_height": 3, "width": 100})
    mtext.dxf.rect_width = 5.0
    mtext.dxf.rect_height = 3.0
    mtext.text = "word " * 400  # many lines even with a narrow font
    box = sf.text_box(mtext)
    assert box is not None
    assert box[3] - box[1] > 12  # many lines, nothing like the stored 3 mm
    assert sf.mtext_lines(mtext) > 3


def test_mtext_with_columns_has_no_line_count() -> None:
    _, layout = sheet()
    mtext = layout.add_mtext("a b c", dxfattribs={"insert": (0, 0), "char_height": 3})
    columns = MTextColumns()
    columns.count = 2
    columns.width = 30.0
    columns.gutter_width = 3.0
    columns.defined_height = 10.0
    mtext.setup_columns(columns)
    assert mtext.has_columns
    assert sf.mtext_lines(mtext) is None


def test_mtext_box_follows_attachment_point_and_rotation() -> None:
    _, layout = sheet()
    kwargs = {"insert": (100, 100), "char_height": 5}
    top_left = sf.text_box(
        layout.add_mtext("ABC DEF", dxfattribs={**kwargs, "attachment_point": 1})
    )
    bottom_right = sf.text_box(
        layout.add_mtext("ABC DEF", dxfattribs={**kwargs, "attachment_point": 9})
    )
    assert top_left is not None and bottom_right is not None
    assert top_left[0] == pytest.approx(100) and top_left[3] == pytest.approx(100)
    assert bottom_right[2] == pytest.approx(100) and bottom_right[1] == pytest.approx(100)
    turned = sf.text_box(layout.add_mtext("ABC DEF", dxfattribs={**kwargs, "rotation": 90}))
    assert turned is not None
    assert (turned[2] - turned[0]) == pytest.approx(top_left[3] - top_left[1], rel=1e-6)
    assert (turned[3] - turned[1]) == pytest.approx(top_left[2] - top_left[0], rel=1e-6)


def test_text_tolerance_is_a_share_of_the_texts_own_size() -> None:
    frame = (0.0, 0.0, 100.0, 100.0)
    assert not sf.outside_frame(frame, (50, 50, 60, 55))
    assert not sf.outside_frame(frame, (90, 50, 101, 55))  # 1 of 11 wide sticks out
    assert sf.outside_frame(frame, (90, 50, 102, 55))  # 2 of 12 is more than a tenth
    assert sf.outside_frame(frame, (50, 96, 60, 100.6))  # 0.6 of 4.6 high


def test_growth_is_judged_per_axis_with_ten_percent() -> None:
    old = (0.0, 0.0, 100.0, 10.0)
    assert not sf.grew(old, (0, 0, 109, 10))
    assert sf.grew(old, (0, 0, 111, 10))
    assert sf.grew(old, (0, 0, 100, 11.5))
    assert not sf.grew(old, (0, 0, 50, 5))  # shrinking is not growth
    assert not sf.grew((0, 0, 0, 0), (0, 0, 50, 5))  # no size to compare with


def test_text_collection_skips_block_definitions_and_other_spaces_on_request() -> None:
    from cadlib.util import Deadline

    doc, layout = sheet()
    doc.blocks.new("B").add_text("inside a definition", height=2)
    layout.add_text("on the sheet", height=3).set_placement((20, 20))
    doc.modelspace().add_text("in model", height=3).set_placement((0, 0))
    deadline = Deadline(None)
    everything = sf.collect_texts(doc, frozenset(), deadline)
    assert sorted(t.space for t in everything) == ["Model", "S"]
    only = sf.collect_texts(doc, frozenset(), deadline, spaces={"S"})
    assert [t.space for t in only] == ["S"]
    assert math.isfinite(only[0].box[0])


def test_mtext_right_attachment_uses_the_reference_width() -> None:
    """Text is left-aligned inside the reference width, so a right-attached short text sits
    at the left of that box, a full reference width left of the insertion point."""
    _, layout = sheet()
    mtext = layout.add_mtext(
        "Rev",
        dxfattribs={"insert": (300, 100), "char_height": 5, "width": 100, "attachment_point": 3},
    )
    box = sf.text_box(mtext)
    assert box is not None
    assert box[0] == pytest.approx(200) and box[2] < 260  # not [300 - w, 300]


def test_text_beside_the_frame_is_not_outside_it() -> None:
    frame = (10.0, 10.0, 100.0, 100.0)
    assert not sf.outside_frame(frame, (110, 50, 130, 55))  # wholly to the right
    assert not sf.outside_frame(frame, (0, 50, 9, 55))  # wholly to the left
    assert not sf.outside_frame(frame, (50, 100, 60, 105))  # only touching the top edge
    assert sf.outside_frame(frame, (95, 50, 120, 55))  # crosses the right edge
