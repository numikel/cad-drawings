"""Fields (FILENAME, SAVEDATE, ...) refreshed by the CAD application on save are not unintended.

Synthetic drawings only: a field is faked by an extension dictionary with an ``ACAD_FIELD`` key,
which is what CAD writes next to the text it evaluates.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import ezdxf
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import diffing, dxf, edit_dxf
from cadlib.entities import has_field
from cadlib.result import ExitCode, Result

DATE_EDIT = {"old": "2026-01-15", "new": "2026-10-01"}


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture
def work(tmp_path: Path, fixtures_dir: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    (target / "sheet_set.dxf").write_bytes((fixtures_dir / "sheet_set.dxf").read_bytes())
    return target


def mark_as_field(entity: Any) -> None:
    xdict = entity.new_extension_dict()
    xdict.add_dictionary("ACAD_FIELD")


def fingerprint_of(path: Path, runs: Path) -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    command = dxf.COMMANDS["fingerprint"]
    command.add_arguments(parser)
    res = command.run(parser.parse_args([str(path), "--run-dir", str(runs)]))
    return json.loads(Path(res.outputs["fingerprint"]["path"]).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# -- fingerprint flag -------------------------------------------------------------------------
def test_fingerprint_flags_entities_with_an_acad_field_key(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_text("plain", dxfattribs={"insert": (0, 0)})
    field_text = msp.add_text("field text", dxfattribs={"insert": (0, 10)})
    field_mtext = msp.add_mtext("field mtext", dxfattribs={"insert": (0, 20)})
    other_xdict = msp.add_text("other xdict", dxfattribs={"insert": (0, 30)})
    mark_as_field(field_text)
    mark_as_field(field_mtext)
    other_xdict.new_extension_dict().add_dictionary("SOMETHING_ELSE")
    path = tmp_path / "fields.dxf"
    doc.saveas(path)
    reread = ezdxf.readfile(path)
    assert [has_field(e) for e in reread.modelspace()] == [False, True, True, False]
    flags = {
        e["props"].get("text"): e["props"].get("field", False)
        for e in fingerprint_of(path, runs)["entities"]
    }
    assert flags == {
        "plain": False,
        "field text": True,
        "field mtext": True,
        "other xdict": False,
    }


def test_an_insert_with_a_field_attribute_is_flagged(tmp_path: Path, runs: Path) -> None:
    doc = ezdxf.new("R2018")
    blk = doc.blocks.new("TB")
    blk.add_attdef("FILE", (0, 0), dxfattribs={"height": 2.5})
    ins = doc.modelspace().add_blockref("TB", (5, 5))
    ins.add_auto_attribs({"FILE": "a.dwg"})
    mark_as_field(ins.attribs[0])
    path = tmp_path / "attrib.dxf"
    doc.saveas(path)
    inserts = [e for e in fingerprint_of(path, runs)["entities"] if e["type"] == "INSERT"]
    assert [e["props"].get("field") for e in inserts] == [True]


def test_fingerprints_without_the_flag_still_diff() -> None:
    def ent(handle: str, text: str, **extra: Any) -> dict[str, Any]:
        props = {"text": text, "height": 2.5, **extra}
        shape = diffing._hash(["TEXT", "0", {"text": text, "height": 2.5}])
        anchor = [1.0, 2.0]
        return {
            "handle": handle,
            "type": "TEXT",
            "space": "model",
            "scope": "Model",
            "layer": "0",
            "bbox": None,
            "anchor": anchor,
            "text_sha1": None,
            "props": props,
            "shape": shape,
            "sig": diffing._hash([shape, anchor]),
        }

    def fp(*entities: dict[str, Any]) -> dict[str, Any]:
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
            "entities": list(entities),
        }

    old = diffing.diff_fingerprints(fp(ent("A", "x.dwg")), fp(ent("A", "x_edited.dwg")))
    assert old["counts"]["changed"] == 1 and "field" not in old["changes"][0]
    new = diffing.diff_fingerprints(
        fp(ent("A", "x.dwg", field=True)), fp(ent("A", "x_edited.dwg", field=True))
    )
    (change,) = new["changes"]
    assert change["kind"] == "changed" and change["field"] is True
    assert [c["field"] for c in change["changes"]] == ["text"]  # the flag is metadata, not a change
    one_sided = diffing.diff_fingerprints(
        fp(ent("A", "x.dwg")), fp(ent("A", "x_edited.dwg", field=True))
    )
    assert one_sided["changes"][0]["field"] is True


# -- edit verification ------------------------------------------------------------------------
class RefreshingBackend(edit_dxf.DxfBackend):
    """Applies the plan, then 'the CAD application' rewrites the text of one other entity."""

    only_fields: bool | None = True

    def apply(self, items: list[Any], log: Any) -> list[Any]:
        outcomes = super().apply(items, log)
        planned = {edit_dxf.norm_handle(i.edit.handle) for i in items}
        for loc in list(self.index.values()):
            e = loc.entity
            if loc.space != "model" or e.dxftype() != "TEXT":
                continue
            if edit_dxf.norm_handle(str(e.dxf.handle)) in planned:
                continue
            if has_field(e) != self.only_fields:
                continue
            e.dxf.text = str(e.dxf.text) + " (refreshed)"
            break
        return outcomes


def _prepare(work: Path, truth: dict[str, Any], *, as_field: bool) -> tuple[Path, str, str]:
    """Copy of the sheet set where one unplanned TEXT is (or is not) a field.

    Returns ``(path, planned handle, handle of the entity the backend will rewrite)``.
    """
    sheet = truth["files"]["sheet_set.dxf"]
    planned = sheet["layouts"][0]["title_block"]["DATE"]["value_handle"]
    path = work / "sheet_set.dxf"
    doc = ezdxf.readfile(path)
    target = next(
        e
        for e in doc.modelspace()
        if e.dxftype() == "TEXT" and edit_dxf.norm_handle(str(e.dxf.handle)) != planned.upper()
    )
    if as_field:
        mark_as_field(target)
    doc.saveas(path)
    return path, planned, str(target.dxf.handle)


def _run(path: Path, planned: str, runs: Path) -> Result:
    from test_edit import E, make_spec, run_edit

    return run_edit(make_spec(path, [E(path, planned, "replace-text", DATE_EDIT)]), runs)


def test_a_refreshed_field_is_informational_not_unintended(
    work: Path, runs: Path, truth: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, planned, field_handle = _prepare(work, truth, as_field=True)
    RefreshingBackend.only_fields = True
    monkeypatch.setattr(edit_dxf, "DxfBackend", RefreshingBackend)
    res = _run(path, planned, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    assert res.summary["verified"] is True and res.summary["unintended"] == 0
    assert res.summary["field_updates"] == 1
    verification = json.loads(Path(res.outputs["report"]["path"]).read_text("utf-8"))[
        "verification"
    ]
    assert verification["unintended"] == [] and verification["verified"] is True
    (update,) = verification["field_updates"]
    assert update["handle"] == field_handle
    assert {"scope", "layer", "old", "new"} <= set(update)
    assert update["new"].endswith("(refreshed)") and "(refreshed)" not in update["old"]
    assert any(
        "1 field(s) were refreshed by the CAD application when saving" in w
        and "not caused by the plan" in w
        for w in res.warnings
    )


def test_the_same_change_on_a_non_field_entity_stays_unintended(
    work: Path, runs: Path, truth: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, planned, _handle = _prepare(work, truth, as_field=False)
    RefreshingBackend.only_fields = False
    monkeypatch.setattr(edit_dxf, "DxfBackend", RefreshingBackend)
    res = _run(path, planned, runs)
    assert res.exit_code == ExitCode.PARTIAL
    assert res.errors[0]["code"] == "UNINTENDED_CHANGE"
    assert res.summary["verified"] is False and res.summary["unintended"] == 1
    assert res.summary.get("field_updates", 0) == 0


def test_a_field_that_also_moved_is_not_waved_through(
    work: Path, runs: Path, truth: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class MovingRefresh(RefreshingBackend):
        def apply(self, items: list[Any], log: Any) -> list[Any]:
            outcomes = super().apply(items, log)
            for loc in self.index.values():
                if has_field(loc.entity):
                    loc.entity.dxf.layer = "SOMEWHERE_ELSE"
            return outcomes

    path, planned, _handle = _prepare(work, truth, as_field=True)
    RefreshingBackend.only_fields = True
    doc = ezdxf.readfile(path)
    doc.layers.add("SOMEWHERE_ELSE")
    doc.saveas(path)
    monkeypatch.setattr(edit_dxf, "DxfBackend", MovingRefresh)
    res = _run(path, planned, runs)
    assert res.exit_code == ExitCode.PARTIAL
    assert res.summary["unintended"] == 1
