"""edit: plan validation, two-pass semantics, the ezdxf backend, verification and outputs.

Everything runs on the synthetic fixtures (generated into a temp directory) or on fakes; no CAD
application is started here (the real-CAD test lives in test_edit_com.py).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any, ClassVar

import ezdxf
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import edit, edit_dxf
from cadlib.entities import expect_for
from cadlib.result import CadError, ExitCode, Result

SCHEMA_PATH = SKILL / "assets" / "edit-spec.schema.json"
SHA = "a" * 40


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def make_spec(path: Path, edits: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "base": {"path": str(path), "sha1": sha1_of(path)},
        "edits": edits,
        **extra,
    }


def run_edit(
    spec: dict[str, Any] | Path, runs: Path, *flags: str, spec_dir: Path | None = None
) -> Result:
    if isinstance(spec, dict):
        spec_path = (spec_dir or runs.parent) / "spec.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
    else:
        spec_path = spec
    parser = argparse.ArgumentParser()
    command = edit.COMMANDS["edit"]
    command.add_arguments(parser)
    args = parser.parse_args(["--spec", str(spec_path), *flags, "--run-dir", str(runs)])
    try:
        return command.run(args)
    except CadError as err:  # what cad.py does
        return Result.from_error("edit", err)


def read_jsonl(result: Result, key: str) -> list[dict[str, Any]]:
    path = Path(result.outputs[key]["path"])
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def read_report(result: Result) -> dict[str, Any]:
    return json.loads(Path(result.outputs["report"]["path"]).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture
def work(tmp_path: Path, fixtures_dir: Path) -> Path:
    """A private copy of the fixtures: tests change files and hashes."""
    target = tmp_path / "work"
    target.mkdir()
    for name in ("sheet_set.dxf", "mtext_cases.dxf", "blocks_attribs.dxf"):
        (target / name).write_bytes((fixtures_dir / name).read_bytes())
    return target


# --------------------------------------------------------------------------------------
# 1. plan validation (no jsonschema at run time) cross-checked against the schema
# --------------------------------------------------------------------------------------


def good_plans() -> list[dict[str, Any]]:
    def one(op: str, args: dict[str, Any] | None, expect: dict[str, Any] | None = None) -> dict:
        item: dict[str, Any] = {
            "id": "e1",
            "op": op,
            "handle": "2F",
            "expect": expect or {"type": "TEXT"},
        }
        if args is not None:
            item["args"] = args
        return {"version": 1, "base": {"path": "a.dxf", "sha1": SHA}, "edits": [item]}

    plans = [
        one("replace-text", {"old": "a", "new": "b"}),
        one("replace-text", {"old": "a", "new": "b", "count": 2}),
        one("set-props", {"props": {"layer": "X", "color": 3}}),
        one("delete", None),
        one("delete", {}),
        one("move", {"vector": [1, 2]}),
        one("move", {"vector": [1, 2.5, 3]}),
        one("clone", {"vector": [0, 1], "target_layer": "L", "target_space": "Sheet-A"}),
        one("pan-viewport", {"view_center": [10, 20]}, {"type": "VIEWPORT"}),
        one(
            "replace-text",
            {"old": "a", "new": "b"},
            {
                "type": "INSERT",
                "layer": "L",
                "space": "model",
                "text": "x",
                "text_is_plain": True,
                "attrib": {"A": "1"},
                "insert": [1, 2, 3],
                "tolerance": 0.5,
            },
        ),
    ]
    plans.append(
        {
            **plans[0],
            "output": {"path": "o.dxf", "overwrite": True},
            "note": "why",
            "edits": plans[0]["edits"] + [{**plans[3]["edits"][0], "id": "e2"}],
        }
    )
    return plans


def test_good_plans_are_valid() -> None:
    for plan in good_plans():
        assert edit.validate_spec(plan) == [], plan


BAD_EDITS: list[tuple[str, Any]] = [
    ("not an object", []),
    ("missing version", {"base": {"path": "a", "sha1": SHA}, "edits": []}),
    ("empty edits", {"version": 1, "base": {"path": "a", "sha1": SHA}, "edits": []}),
    ("version 2", {"version": 2, "base": {"path": "a", "sha1": SHA}, "edits": []}),
    ("version true", {"version": True, "base": {"path": "a", "sha1": SHA}, "edits": []}),
    ("short sha1", {"version": 1, "base": {"path": "a", "sha1": "abc"}, "edits": []}),
    ("upper sha1", {"version": 1, "base": {"path": "a", "sha1": "A" * 40}, "edits": []}),
    ("extra top key", {**good_plans()[0], "extra": 1}),
    ("extra base key", {**good_plans()[0], "base": {"path": "a", "sha1": SHA, "x": 1}}),
    ("output overwrite str", {**good_plans()[0], "output": {"overwrite": "yes"}}),
    ("note int", {**good_plans()[0], "note": 3}),
]


@pytest.mark.parametrize(("label", "plan"), BAD_EDITS, ids=[b[0] for b in BAD_EDITS])
def test_bad_plans_are_rejected_like_the_schema(label: str, plan: Any) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    assert not validator.is_valid(plan), f"test data is not invalid: {label}"
    assert edit.validate_spec(plan), label


def _paths(node: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = [prefix]
    if isinstance(node, dict):
        for key, value in node.items():
            out += _paths(value, (*prefix, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _paths(value, (*prefix, i))
    return out


def _set(root: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return value
    node = root
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return root


def _delete(root: Any, path: tuple[Any, ...]) -> Any:
    node = root
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return root


REPLACEMENTS: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    -1,
    1.0,
    1.5,
    "",
    "x",
    "ab" * 20,
    [],
    {},
    [1],
    [1, 2],
    [1, 2, 3, 4],
    ["a"],
    {"k": "v"},
]


def test_validator_agrees_with_the_schema_on_mutations() -> None:
    """Every single-point mutation of the good plans: same verdict as jsonschema."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    checked = 0
    disagreements: list[str] = []
    for plan in good_plans():
        for path in _paths(plan):
            variants: list[Any] = [_set(copy.deepcopy(plan), path, r) for r in REPLACEMENTS]
            if path:
                variants.append(_delete(copy.deepcopy(plan), path))
            if isinstance(plan_at(plan, path), dict):
                variants.append(_set(copy.deepcopy(plan), (*path, "unexpected"), 1))
            for variant in variants:
                expected = validator.is_valid(variant)
                actual = edit.validate_spec(variant) == []
                checked += 1
                if expected != actual:
                    disagreements.append(f"{expected=} {actual=} {json.dumps(variant)[:200]}")
    assert checked > 1500
    assert not disagreements, disagreements[:5]


def plan_at(plan: Any, path: tuple[Any, ...]) -> Any:
    node = plan
    for key in path:
        node = node[key]
    return node


def test_op_specific_required_args_agree_with_the_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text("utf-8")))
    base = good_plans()[0]
    for op, args in itertools.product(
        ("replace-text", "set-props", "delete", "move", "clone", "pan-viewport", "bogus"),
        (
            None,
            {},
            {"old": "a"},
            {"new": "b"},
            {"old": "a", "new": "b"},
            {"props": {}},
            {"vector": [1, 2]},
            {"view_center": [1, 2]},
        ),
    ):
        plan = copy.deepcopy(base)
        plan["edits"][0]["op"] = op
        if args is None:
            plan["edits"][0].pop("args", None)
        else:
            plan["edits"][0]["args"] = args
        assert (edit.validate_spec(plan) == []) == validator.is_valid(plan), (op, args)


def test_validation_messages_name_the_location() -> None:
    plan = copy.deepcopy(good_plans()[0])
    plan["edits"][0]["handle"] = "XYZ"
    problems = edit.validate_spec(plan)
    assert problems and "edits[0].handle" in problems[0]


def test_semantic_checks_reject_duplicate_ids_and_empty_old(tmp_path: Path, runs: Path) -> None:
    plan = copy.deepcopy(good_plans()[-1])
    plan["edits"][1]["id"] = plan["edits"][0]["id"]
    assert any("duplicate" in p for p in edit.validate_semantics(plan))
    plan2 = copy.deepcopy(good_plans()[0])
    plan2["edits"][0]["args"]["old"] = ""
    assert any("old" in p for p in edit.validate_semantics(plan2))
    res = run_edit(plan, runs)
    assert res.exit_code == ExitCode.BAD_ARGS
    assert res.errors[0]["code"] == "SPEC_INVALID"
    assert not (runs.exists() and any(runs.rglob("*_edited.*")))


def test_unreadable_or_non_json_spec(tmp_path: Path, runs: Path) -> None:
    missing = run_edit(tmp_path / "nope.json", runs)
    assert missing.errors[0]["code"] == "FILE_NOT_FOUND"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    res = run_edit(bad, runs)
    assert res.exit_code == ExitCode.BAD_ARGS and res.errors[0]["code"] == "SPEC_INVALID"


# --------------------------------------------------------------------------------------
# shared helpers for the sections below
# --------------------------------------------------------------------------------------
def expect_of(path: Path, handle: str) -> dict[str, Any]:
    """The ``expect`` block ``find``/``dump`` would emit for ``handle``."""
    loc = edit_dxf.build_index(ezdxf.readfile(path))[edit_dxf.norm_handle(handle)]
    return expect_for(loc) or {}


def E(
    path: Path,
    handle: str,
    op: str,
    args: dict[str, Any] | None = None,
    *,
    id: str | None = None,
    expect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": id or f"{op}-{handle}",
        "op": op,
        "handle": handle,
        "expect": expect if expect is not None else expect_of(path, handle),
    }
    if args is not None:
        item["args"] = args
    return item


@pytest.fixture(scope="module")
def sheet_truth(truth: dict[str, Any]) -> dict[str, Any]:
    return truth["files"]["sheet_set.dxf"]  # type: ignore[no-any-return]


def occurrence(truth_sheet: dict[str, Any], key: str) -> str:
    return next(o["handle"] for o in truth_sheet["search"]["occurrences"] if o["id"] == key)


def parent_of(truth_sheet: dict[str, Any], key: str) -> str:
    return next(o["parent_handle"] for o in truth_sheet["search"]["occurrences"] if o["id"] == key)


def date_handles(truth_sheet: dict[str, Any]) -> list[str]:
    return [s["title_block"]["DATE"]["value_handle"] for s in truth_sheet["layouts"]]


def mtext_handle(truth: dict[str, Any], key: str) -> str:
    return next(m["handle"] for m in truth["files"]["mtext_cases.dxf"]["mtext"] if m["id"] == key)


def edited_doc(result: Result) -> Any:
    return ezdxf.readfile(result.outputs["edited"]["path"])


def entity_at(doc: Any, handle: str) -> Any:
    found = doc.entitydb.get(handle)
    return found if found is not None and found.is_alive else None


MTEXT_RAW = "Line one\\PTemp 21\\U+00B0C {\\fArial|b1;bold} then plain\\Pend"
DATE_EDIT = {"old": "2026-01-15", "new": "2026-10-01"}


# --------------------------------------------------------------------------------------
# 2. two passes: nothing is written unless everything checks out
# --------------------------------------------------------------------------------------


def test_base_changed_is_refused_before_anything_else(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    handle = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, handle, "replace-text", DATE_EDIT)])
    spec["base"]["sha1"] = "0" * 40
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.PRECONDITION_FAILED
    assert res.errors[0]["code"] == "BASE_CHANGED"
    assert "edited" not in res.outputs
    assert not list(runs.rglob("*_edited.*"))


def test_all_mismatches_are_reported_and_nothing_is_written(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    before = sha1_of(src)
    h_date, h_date_b = date_handles(sheet_truth)
    wrong_text = {**expect_of(src, h_date), "text": "1999-01-01"}
    wrong_layer = {**expect_of(src, h_date_b), "layer": "NOPE"}
    spec = make_spec(
        src,
        [
            E(src, h_date, "replace-text", DATE_EDIT, id="ok"),
            E(
                src,
                h_date,
                "replace-text",
                {"old": "2026", "new": "X"},
                id="bad-text",
                expect=wrong_text,
            ),
            E(
                src,
                h_date_b,
                "set-props",
                {"props": {"layer": "TB-FRAME"}},
                id="bad-layer",
                expect=wrong_layer,
            ),
            {"id": "ghost", "op": "delete", "handle": "FFFFF", "expect": {"type": "TEXT"}},
        ],
    )
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.PRECONDITION_FAILED
    assert res.errors[0]["code"] == "EXPECT_FAILED"
    report = read_report(res)
    assert {p["edit"] for p in report["problems"]} == {"bad-text", "bad-layer", "ghost"}
    fields = {p["edit"]: p.get("field") for p in report["problems"]}
    assert fields == {"bad-text": "text", "bad-layer": "layer", "ghost": "handle"}
    assert "edited" not in res.outputs
    assert not list(runs.rglob("*_edited.*"))
    assert sha1_of(src) == before


def test_dry_run_checks_and_writes_nothing(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    handle = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, handle, "replace-text", DATE_EDIT)])
    res = run_edit(spec, runs, "--dry-run")
    assert res.exit_code == ExitCode.OK
    assert res.summary["dry_run"] is True
    assert "edited" not in res.outputs
    assert not list(runs.rglob("*_edited.*"))
    lines = read_jsonl(res, "changes")
    assert [x["status"] for x in lines] == ["planned"]
    assert lines[0]["after"] == {"text": "2026-10-01"}


def test_second_run_is_idempotent(work: Path, runs: Path, sheet_truth: dict[str, Any]) -> None:
    src = work / "sheet_set.dxf"
    handles = date_handles(sheet_truth)
    text = occurrence(sheet_truth, "text_model_visible")
    vp = sheet_truth["layouts"][1]["viewports"][0]["handle"]
    edits = [
        E(src, handles[0], "replace-text", DATE_EDIT, id="t"),
        E(src, text, "set-props", {"props": {"layer": "A-FURN", "color": 5}}, id="p"),
        E(src, text, "move", {"vector": [100, 50]}, id="m"),
        E(src, vp, "pan-viewport", {"view_center": [5000, 4000]}, id="v"),
    ]
    first = run_edit(make_spec(src, edits), runs)
    assert first.exit_code == ExitCode.OK, (first.errors, first.summary)
    statuses = {x["id"]: x["status"] for x in read_jsonl(first, "changes")}
    assert statuses == dict.fromkeys("tpmv", "applied")
    edited = Path(first.outputs["edited"]["path"])
    # the same plan against the edited file (new base): all of it is already in place
    res = run_edit(make_spec(edited, edits), runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    statuses = {x["id"]: x["status"] for x in read_jsonl(res, "changes")}
    assert statuses == dict.fromkeys("tpmv", "already_applied")
    assert res.summary["already_applied"] == 4 and res.summary["applied"] == 0
    assert res.summary["unintended"] == 0


def test_delete_conflict_inside_a_plan_is_a_problem(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    handle = date_handles(sheet_truth)[0]
    spec = make_spec(
        src,
        [
            E(src, handle, "delete", id="d"),
            E(src, handle, "set-props", {"props": {"color": 3}}, id="p"),
        ],
    )
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.PRECONDITION_FAILED
    assert {p["edit"] for p in read_report(res)["problems"]} == {"p"}


# --------------------------------------------------------------------------------------
# 3. the six operations on the ezdxf backend
# --------------------------------------------------------------------------------------


def test_replace_text_on_title_block_text(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    h_a, h_b = date_handles(sheet_truth)
    spec = make_spec(
        src,
        [
            E(src, h_a, "replace-text", DATE_EDIT),
            E(src, h_b, "replace-text", {"old": "2026-01-16", "new": "2026-10-01"}),
        ],
    )
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, res.errors
    doc = edited_doc(res)
    assert entity_at(doc, h_a).dxf.text == "2026-10-01"
    assert entity_at(doc, h_b).dxf.text == "2026-10-01"
    assert res.summary["unintended"] == 0 and res.summary["verified"] is True


def test_replace_text_with_the_expect_that_find_emits(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    """An ``expect`` taken verbatim from a ``find`` hit is accepted (rounded insert, raw text)."""
    from cadlib import dxf

    src = work / "sheet_set.dxf"
    parser = argparse.ArgumentParser()
    dxf.COMMANDS["find"].add_arguments(parser)
    found = dxf.COMMANDS["find"].run(
        parser.parse_args([str(src), "--pattern", "FIRE (EXIT|RATING)", "--run-dir", str(runs)])
    )
    lines = Path(found.outputs["hits"]["path"]).read_text("utf-8").splitlines()
    hits = [json.loads(x) for x in lines if x]
    assert len(hits) >= 3
    edits = [
        {
            "id": f"h{i}",
            "op": "replace-text",
            "handle": hit["handle"],
            "expect": hit["expect"],
            "args": {"old": "FIRE", "new": "SMOKE"},
        }
        for i, hit in enumerate(hits)
    ]
    res = run_edit(make_spec(src, edits), runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    doc = edited_doc(res)
    for hit in hits:
        entity = entity_at(doc, hit["handle"])
        text = entity.text if entity.dxftype() == "MTEXT" else entity.dxf.text
        assert "SMOKE" in text and "FIRE" not in text


def test_replace_text_on_attrib_keeps_the_insert(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    note = occurrence(sheet_truth, "attrib_value")
    spec = make_spec(src, [E(src, note, "replace-text", {"old": "60", "new": "90"})])
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, res.errors
    attrib = entity_at(edited_doc(res), note)
    assert attrib.dxf.text == "FIRE RATING 90" and attrib.dxf.tag == "NOTE"
    assert res.summary["unintended"] == 0


def test_replace_text_in_mtext_preserves_formatting_codes(
    work: Path, runs: Path, truth: dict[str, Any]
) -> None:
    src = work / "mtext_cases.dxf"
    handle = mtext_handle(truth, "inline_codes")
    spec = make_spec(
        src,
        [E(src, handle, "replace-text", {"old": "then plain", "new": "then PLAIN"}, id="a")],
    )
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, res.errors
    assert entity_at(edited_doc(res), handle).text == MTEXT_RAW.replace("then plain", "then PLAIN")
    # the codes outside the match are byte-for-byte what they were
    for code in ("\\P", "\\U+00B0", "{\\fArial|b1;bold}"):
        assert code in entity_at(edited_doc(res), handle).text


def test_mtext_old_must_match_once_and_in_the_raw_string(
    work: Path, runs: Path, truth: dict[str, Any]
) -> None:
    src = work / "mtext_cases.dxf"
    handle = mtext_handle(truth, "inline_codes")
    cases = {
        "many": {"old": "e", "new": "E"},
        "absent": {"old": "nothing here", "new": "x"},
        "plain-only": {"old": "Temp 21°C", "new": "x"},
        "count-mismatch": {"old": "e", "new": "E", "count": 2},
    }
    spec = make_spec(src, [E(src, handle, "replace-text", a, id=k) for k, a in cases.items()])
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.PRECONDITION_FAILED
    problems = {p["edit"]: p for p in read_report(res)["problems"]}
    assert set(problems) == set(cases)
    assert "exactly once" in problems["many"]["message"]
    assert "plain text" in problems["plain-only"]["message"]
    # an explicit, correct count is accepted
    n = MTEXT_RAW.count("e")
    ok = make_spec(src, [E(src, handle, "replace-text", {"old": "e", "new": "E", "count": n})])
    res2 = run_edit(ok, runs)
    assert res2.exit_code == ExitCode.OK, res2.errors
    assert entity_at(edited_doc(res2), handle).text == MTEXT_RAW.replace("e", "E")


def test_mtext_formatting_code_inside_the_match_can_be_replaced(
    work: Path, runs: Path, truth: dict[str, Any]
) -> None:
    src = work / "mtext_cases.dxf"
    handle = mtext_handle(truth, "inline_codes")
    edit_args = {"old": "{\\fArial|b1;bold}", "new": "{\\fArial|b1;BOLD}"}
    res = run_edit(make_spec(src, [E(src, handle, "replace-text", edit_args)]), runs)
    assert res.exit_code == ExitCode.OK, res.errors
    assert "{\\fArial|b1;BOLD}" in entity_at(edited_doc(res), handle).text


def test_set_props_on_text_and_mtext(
    work: Path, runs: Path, sheet_truth: dict[str, Any], truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    text = occurrence(sheet_truth, "text_model_visible")
    props = {"layer": "A-FURN", "color": 5, "height": 250, "rotation": 15}
    res = run_edit(make_spec(src, [E(src, text, "set-props", {"props": props})]), runs)
    assert res.exit_code == ExitCode.OK, res.errors
    e = entity_at(edited_doc(res), text)
    assert (e.dxf.layer, e.dxf.color, e.dxf.height, e.dxf.rotation) == ("A-FURN", 5, 250.0, 15.0)
    assert res.summary["unintended"] == 0

    mt_src = work / "mtext_cases.dxf"
    handle = mtext_handle(truth, "inline_codes")
    mt_props = {"height": 300, "rotation": 30, "color": 1}
    res2 = run_edit(make_spec(mt_src, [E(mt_src, handle, "set-props", {"props": mt_props})]), runs)
    assert res2.exit_code == ExitCode.OK, res2.errors
    m = entity_at(edited_doc(res2), handle)
    assert m.dxf.char_height == 300 and m.dxf.color == 1
    assert m.get_rotation() == pytest.approx(30.0)


def test_set_props_rejects_unknown_names_and_missing_layers(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    text = occurrence(sheet_truth, "text_model_visible")

    def attempt(props: dict[str, Any]) -> Result:
        return run_edit(make_spec(src, [E(src, text, "set-props", {"props": props})]), runs)

    unknown = attempt({"bogus": 1})
    assert unknown.exit_code == ExitCode.BAD_ARGS and unknown.errors[0]["code"] == "UNSUPPORTED"
    assert attempt({"layer": "NO-SUCH"}).exit_code == ExitCode.PRECONDITION_FAILED
    assert attempt({"color": 999}).exit_code == ExitCode.PRECONDITION_FAILED
    assert attempt({"radius": 5}).errors[0]["code"] == "UNSUPPORTED"  # a TEXT has no radius


def test_delete_text_attrib_and_insert(work: Path, runs: Path, sheet_truth: dict[str, Any]) -> None:
    src = work / "sheet_set.dxf"
    text = occurrence(sheet_truth, "text_model_visible")
    note = occurrence(sheet_truth, "attrib_value")
    parent = parent_of(sheet_truth, "attrib_value")
    res = run_edit(make_spec(src, [E(src, text, "delete"), E(src, note, "delete")]), runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    doc = edited_doc(res)
    assert entity_at(doc, text) is None
    assert entity_at(doc, note) is None
    assert [a.dxf.tag for a in entity_at(doc, parent).attribs] == ["MARK"]
    assert res.summary["unintended"] == 0
    # deleting the INSERT removes its attributes with it
    res2 = run_edit(make_spec(src, [E(src, parent, "delete")]), runs)
    assert res2.exit_code == ExitCode.OK, (res2.errors, res2.summary)
    doc2 = edited_doc(res2)
    assert entity_at(doc2, parent) is None and entity_at(doc2, note) is None


def test_move_text_and_insert(work: Path, runs: Path, sheet_truth: dict[str, Any]) -> None:
    src = work / "sheet_set.dxf"
    text = occurrence(sheet_truth, "text_model_visible")
    parent = parent_of(sheet_truth, "attrib_value")
    note = occurrence(sheet_truth, "attrib_value")
    orig = ezdxf.readfile(src)
    t0 = tuple(entity_at(orig, text).dxf.insert)
    n0 = tuple(entity_at(orig, note).dxf.insert)
    edits = [
        E(src, text, "move", {"vector": [100, -50]}),
        E(src, parent, "move", {"vector": [10, 20, 0]}),
    ]
    res = run_edit(make_spec(src, edits), runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    doc = edited_doc(res)
    t1 = tuple(entity_at(doc, text).dxf.insert)
    assert (t1[0] - t0[0], t1[1] - t0[1]) == pytest.approx((100.0, -50.0))
    n1 = tuple(entity_at(doc, note).dxf.insert)  # attributes travel with their INSERT
    assert (n1[0] - n0[0], n1[1] - n0[1]) == pytest.approx((10.0, 20.0))
    assert res.summary["unintended"] == 0


def test_clone_reports_the_new_handle(work: Path, runs: Path, sheet_truth: dict[str, Any]) -> None:
    src = work / "sheet_set.dxf"
    text = occurrence(sheet_truth, "text_model_visible")
    spec = make_spec(
        src,
        [
            E(src, text, "clone", {"vector": [0, -400], "target_layer": "A-FURN"}, id="c1"),
            E(src, text, "clone", {"vector": [5, 5], "target_space": "Sheet-A"}, id="c2"),
        ],
    )
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    lines = {x["id"]: x for x in read_jsonl(res, "changes")}
    doc = edited_doc(res)
    c1 = entity_at(doc, lines["c1"]["new_handle"])
    assert c1.dxf.text == "FIRE EXIT" and c1.dxf.layer == "A-FURN"
    assert c1.dxf.insert.y == pytest.approx(entity_at(doc, text).dxf.insert.y - 400)
    c2 = entity_at(doc, lines["c2"]["new_handle"])
    assert c2.dxf.owner == doc.layouts.get("Sheet-A").layout_key
    assert entity_at(doc, text) is not None  # the source stays
    assert lines["c1"]["new_handle"] != lines["c2"]["new_handle"]
    assert res.summary["unintended"] == 0
    assert any("clone" in w for w in res.warnings)  # a rerun would clone again


@pytest.mark.parametrize("sheet", [0, 1], ids=["untwisted", "twisted"])
def test_pan_viewport_converts_the_centre_to_display_coordinates(
    work: Path, runs: Path, sheet_truth: dict[str, Any], sheet: int
) -> None:
    src = work / "sheet_set.dxf"
    vp = sheet_truth["layouts"][sheet]["viewports"][0]
    desired = (5200.0, 3900.0)
    spec = make_spec(src, [E(src, vp["handle"], "pan-viewport", {"view_center": list(desired)})])
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    twist = math.radians(vp["twist_deg"])
    expected_dcs = (
        desired[0] * math.cos(twist) - desired[1] * math.sin(twist),
        desired[0] * math.sin(twist) + desired[1] * math.cos(twist),
    )
    e = entity_at(edited_doc(res), vp["handle"])
    stored = (e.dxf.view_center_point.x, e.dxf.view_center_point.y)
    assert stored == pytest.approx(expected_dcs, abs=1e-6)
    # scale and size untouched
    assert e.dxf.view_height == vp["view_height"]
    assert (e.dxf.width, e.dxf.height) == tuple(vp["size"])
    # and the viewport model agrees that the window now looks at the desired point
    from cadlib.viewports import _viewport_info

    info = _viewport_info(e)
    assert info.view_center_wcs == pytest.approx(desired, abs=1e-6)
    assert info.contains_model_point(*desired)
    assert res.summary["unintended"] == 0


def test_pan_viewport_refuses_the_overall_viewport(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    doc = ezdxf.readfile(src)
    overall = next(v for v in doc.layouts.get("Sheet-A").query("VIEWPORT") if v.dxf.id == 1)
    spec = make_spec(src, [E(src, overall.dxf.handle, "pan-viewport", {"view_center": [1, 1]})])
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.BAD_ARGS and res.errors[0]["code"] == "UNSUPPORTED"


# --------------------------------------------------------------------------------------
# 4. verification: the file must differ from the original exactly as planned
# --------------------------------------------------------------------------------------


def test_verification_matches_the_plan(work: Path, runs: Path, sheet_truth: dict[str, Any]) -> None:
    src = work / "sheet_set.dxf"
    text = occurrence(sheet_truth, "text_model_visible")
    note = occurrence(sheet_truth, "attrib_value")
    h_a, h_b = date_handles(sheet_truth)
    res = run_edit(
        make_spec(
            src,
            [
                E(src, h_a, "replace-text", DATE_EDIT),
                E(src, note, "replace-text", {"old": "60", "new": "90"}),
                E(src, text, "move", {"vector": [100, 0]}),
                E(src, h_b, "delete"),
                E(src, text, "clone", {"vector": [0, -300]}, id="clone"),
            ],
        ),
        runs,
    )
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    verification = read_report(res)["verification"]
    assert verification["counts"] == {"changed": 2, "moved": 1, "removed": 1, "added": 1}
    assert verification["unintended"] == []
    assert res.summary["verified"] is True


class SloppyBackend(edit_dxf.DxfBackend):
    """Does what the plan says and also touches an entity nobody asked about."""

    mode = "text"

    def apply(self, items: list[Any], log: Any) -> list[Any]:
        outcomes = super().apply(items, log)
        planned = {edit_dxf.norm_handle(i.edit.handle) for i in items}
        for loc in list(self.index.values()):
            e = loc.entity
            if loc.space != "model" or e.dxftype() != "TEXT":
                continue
            if edit_dxf.norm_handle(str(e.dxf.handle)) in planned:
                continue
            if self.mode == "text":
                e.dxf.text = str(e.dxf.text) + " (changed)"
            elif self.mode == "delete":
                self.doc.modelspace().delete_entity(e)
            elif self.mode == "move":
                e.translate(7, 7, 0)
            break
        return outcomes


SLOPPY_KIND = {"text": "changed", "delete": "removed", "move": "moved"}


@pytest.mark.parametrize("mode", ["text", "delete", "move"])
def test_unintended_change_is_exit_7_and_listed(
    work: Path,
    runs: Path,
    sheet_truth: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    SloppyBackend.mode = mode
    monkeypatch.setattr(edit_dxf, "DxfBackend", SloppyBackend)
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    res = run_edit(make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)]), runs)
    assert res.exit_code == ExitCode.PARTIAL
    assert res.errors[0]["code"] == "UNINTENDED_CHANGE"
    assert res.summary["verified"] is False and res.summary["unintended"] == 1
    assert "edited" in res.outputs and Path(res.outputs["edited"]["path"]).is_file()
    unintended = read_report(res)["verification"]["unintended"]
    assert len(unintended) == 1 and unintended[0]["kind"] == SLOPPY_KIND[mode]
    assert any("NOT verified" in w for w in res.warnings)


def test_unintended_change_does_not_deliver_to_out(
    work: Path,
    runs: Path,
    sheet_truth: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    SloppyBackend.mode = "text"
    monkeypatch.setattr(edit_dxf, "DxfBackend", SloppyBackend)
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    out = tmp_path / "deliver" / "result.dxf"
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)])
    res = run_edit(spec, runs, "--out", str(out))
    assert res.exit_code == ExitCode.PARTIAL
    assert not out.exists()


def test_a_planned_change_that_did_not_happen_is_flagged(
    work: Path, runs: Path, sheet_truth: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend that claims success but changes nothing is caught by the post-conditions."""

    class Lazy(edit_dxf.DxfBackend):
        def _replace_text(self, item: Any) -> Any:  # reports success, writes nothing
            out = super()._replace_text(item)
            self._loc(item.edit.handle).entity.dxf.text = out.before["text"]
            return out

    monkeypatch.setattr(edit_dxf, "DxfBackend", Lazy)
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    res = run_edit(make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)]), runs)
    assert res.exit_code == ExitCode.PARTIAL
    assert read_report(res)["verification"]["not_as_planned"][0]["edit"] == f"replace-text-{h_a}"


# --------------------------------------------------------------------------------------
# 5. outputs
# --------------------------------------------------------------------------------------


def test_outputs_and_the_original_stays_untouched(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    before = sha1_of(src)
    h_a = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)], note="refresh")
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK
    assert sha1_of(src) == before
    run_dir = Path(res.run_dir or "")
    edited = Path(res.outputs["edited"]["path"])
    assert edited.name == "sheet_set_edited.dxf" and edited.parent == run_dir
    assert {"edited", "changes", "report"} <= set(res.outputs)
    (line,) = read_jsonl(res, "changes")
    assert line["id"] == f"replace-text-{h_a}" and line["op"] == "replace-text"
    assert line["handle"] == h_a and line["status"] == "applied"
    assert line["before"] == {"text": "2026-01-15"} and line["after"] == {"text": "2026-10-01"}
    report = read_report(res)
    assert report["base"]["sha1"] == before and report["backend"] == "ezdxf"
    assert report["note"] == "refresh"
    assert "new version" in report["handles"]
    assert res.backend == "ezdxf"
    assert len(res.to_json().encode()) < 4096


def test_out_is_an_atomic_copy_and_refuses_bad_targets(
    work: Path, runs: Path, sheet_truth: dict[str, Any], tmp_path: Path
) -> None:
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)])
    before = sha1_of(src)
    res = run_edit(spec, runs, "--out", str(src))  # the source itself
    assert res.exit_code == ExitCode.BAD_ARGS
    assert sha1_of(src) == before
    target = tmp_path / "taken.dxf"  # an existing target
    target.write_text("keep me", encoding="utf-8")
    res2 = run_edit(spec, runs, "--out", str(target))
    assert res2.exit_code == ExitCode.PRECONDITION_FAILED and res2.errors[0]["code"] == "EXISTS"
    assert target.read_text(encoding="utf-8") == "keep me"
    assert not list(runs.rglob("*_edited.*"))  # refused before any work
    res3 = run_edit(spec, runs, "--out", str(tmp_path / "x.dwg"))  # wrong suffix
    assert res3.exit_code == ExitCode.BAD_ARGS
    res4 = run_edit(spec, runs, "--out", str(target), "--overwrite")
    assert res4.exit_code == ExitCode.OK
    assert sha1_of(target) == res4.outputs["edited"]["sha1"]
    assert res4.outputs["out"]["path"] == str(target)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    fresh = tmp_path / "nested" / "fresh.dxf"  # a fresh target in a new folder
    res5 = run_edit(spec, runs, "--out", str(fresh))
    assert res5.exit_code == ExitCode.OK and fresh.is_file()


def test_spec_output_path_is_used_without_the_flag(
    work: Path, runs: Path, sheet_truth: dict[str, Any], tmp_path: Path
) -> None:
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    out = tmp_path / "from-spec.dxf"
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)], output={"path": str(out)})
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK and out.is_file()
    again = run_edit(spec, runs)
    assert again.errors[0]["code"] == "EXISTS"
    spec["output"]["overwrite"] = True
    assert run_edit(spec, runs).exit_code == ExitCode.OK


def test_every_run_gets_a_fresh_run_directory(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)])
    r1, r2 = run_edit(spec, runs), run_edit(spec, runs)
    assert r1.run_dir != r2.run_dir
    assert Path(r1.run_dir or "").parent == runs


def test_relative_base_path_resolves_next_to_the_plan(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)])
    spec["base"]["path"] = "sheet_set.dxf"
    spec_file = work / "plan.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    assert run_edit(spec_file, runs).exit_code == ExitCode.OK


def test_dwg_source_needs_consent(tmp_path: Path, runs: Path) -> None:
    dwg = tmp_path / "drawing.dwg"
    dwg.write_bytes(b"AC1032 not a real drawing")
    spec = make_spec(dwg, [{"id": "d", "op": "delete", "handle": "1F", "expect": {"type": "TEXT"}}])
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.MISSING_DEPENDENCY
    assert res.errors[0]["code"] == "NO_BACKEND" and "--allow-com" in res.errors[0]["hint"]


def test_unsupported_source_type(tmp_path: Path, runs: Path) -> None:
    other = tmp_path / "drawing.txt"
    other.write_text("x", encoding="utf-8")
    spec = make_spec(
        other, [{"id": "d", "op": "delete", "handle": "1F", "expect": {"type": "TEXT"}}]
    )
    assert run_edit(spec, runs).errors[0]["code"] == "UNSUPPORTED"


def test_cli_prints_only_json_on_stdout(
    work: Path, tmp_path: Path, sheet_truth: dict[str, Any]
) -> None:
    import os
    import subprocess

    src = work / "sheet_set.dxf"
    h_a = date_handles(sheet_truth)[0]
    spec = make_spec(src, [E(src, h_a, "replace-text", DATE_EDIT)])
    spec_file = tmp_path / "plan.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    env = {**os.environ, "CAD_DRAWINGS_RUNS": str(tmp_path / "cli-runs")}
    proc = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "cad.py"), "edit", "--spec", str(spec_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["command"] == "edit" and data["summary"]["applied"] == 1
    assert proc.stdout.strip().count("\n") == 0


# --------------------------------------------------------------------------------------
# 6. COM backend with fakes (no CAD application is started)
# --------------------------------------------------------------------------------------
import shutil

from cadlib import acad_edit, convert
from cadlib import runs as runs_mod
from cadlib.convert import DxfResult


class FakeComError(Exception):
    """Looks like a permanent ``pywintypes.com_error`` to ``acad.retry``."""

    hresult = -2147352567

    def __init__(self, text: str) -> None:
        super().__init__(-2147352567, text, (0, "fake", text, None, 0, -2147352567), None)


class FakeEntity:
    """What a COM entity looks like to the backend, backed by an ezdxf entity so that the
    result can be exported again and verified like a real CAD round trip."""

    def __init__(self, entity: Any, cad: FakeCad) -> None:
        object.__setattr__(self, "_e", entity)
        object.__setattr__(self, "_cad", cad)

    _NAMES: ClassVar[dict[str, str]] = {
        "TEXT": "AcDbText",
        "MTEXT": "AcDbMText",
        "ATTRIB": "AcDbAttribute",
        "INSERT": "AcDbBlockReference",
        "VIEWPORT": "AcDbViewport",
    }

    @property
    def ObjectName(self) -> str:
        if self._cad.wrong_class:
            return "AcDbLine"
        return self._NAMES.get(self._e.dxftype(), "AcDbOther")

    @property
    def Handle(self) -> str:
        return str(self._e.dxf.handle)

    @property
    def TextString(self) -> str:
        text = self._e.text if self._e.dxftype() == "MTEXT" else self._e.dxf.text
        return self._cad.text_view(str(text))

    @TextString.setter
    def TextString(self, value: str) -> None:
        self._cad.record("TextString", value)
        if self._cad.fail_on == "TextString":
            raise FakeComError("TextString refused")
        if self._e.dxftype() == "MTEXT":
            self._e.text = value
        else:
            self._e.dxf.text = value

    @property
    def Layer(self) -> str:
        return str(self._e.dxf.layer)

    @Layer.setter
    def Layer(self, value: str) -> None:
        self._cad.record("Layer", value)
        self._e.dxf.layer = value

    @property
    def color(self) -> int:
        return int(self._e.dxf.get("color", 256))

    @color.setter
    def color(self, value: int) -> None:
        self._cad.record("color", value)
        self._e.dxf.color = value

    @property
    def Height(self) -> float:
        key = "char_height" if self._e.dxftype() == "MTEXT" else "height"
        return float(self._e.dxf.get(key, 0.0))

    @Height.setter
    def Height(self, value: float) -> None:
        self._cad.record("Height", value)
        key = "char_height" if self._e.dxftype() == "MTEXT" else "height"
        self._e.dxf.set(key, value)

    @property
    def Rotation(self) -> float:
        return math.radians(float(self._e.dxf.get("rotation", 0.0)))

    @Rotation.setter
    def Rotation(self, value: float) -> None:
        self._cad.record("Rotation", value)
        self._e.dxf.rotation = math.degrees(value)

    def Move(self, start: Any, end: Any) -> None:
        self._cad.record("Move", (tuple(start), tuple(end)))
        if self._cad.fail_on == "Move":
            raise FakeComError("Move refused")
        self._e.translate(*(b - a for a, b in zip(start, end, strict=True)))

    def Copy(self) -> FakeEntity:
        self._cad.record("Copy", self.Handle)
        copy = self._e.copy()
        self._cad.doc.layouts.get_layout_for_entity(self._e).add_entity(copy)
        return FakeEntity(copy, self._cad)

    def Delete(self) -> None:
        self._cad.record("Delete", self.Handle)
        layout = self._cad.doc.layouts.get_layout_for_entity(self._e)
        layout.delete_entity(self._e)


class FakeRawDoc:
    def __init__(self, cad: FakeCad) -> None:
        self._cad = cad

    def HandleToObject(self, handle: str) -> FakeEntity:
        entity = self._cad.doc.entitydb.get(handle.upper())
        if entity is None:
            raise AttributeError(f"no object with handle {handle}")
        return FakeEntity(entity, self._cad)


class FakeDoc:
    def __init__(self, cad: FakeCad, path: Path) -> None:
        self.raw = FakeRawDoc(cad)
        self.path = path
        self.closed = False

    def close(self, save: bool = False) -> None:
        self.closed = True


class FakeCad:
    """A CAD session stand-in. The 'drawing' is an ezdxf document read from ``template``."""

    pid = 4242
    image = "fake-cad.exe"

    def __init__(self, template: Path, store: Path) -> None:
        self.template = template
        self.store = store
        self.doc: Any = None
        self.calls: list[tuple[str, Any]] = []
        self.opened: list[tuple[Path, bool]] = []
        self.saved: list[tuple[Path, str]] = []
        self.docs: list[FakeDoc] = []
        self.exports: list[Path] = []
        self.quit_called = False
        self.warnings: list[str] = []
        self.fail_on: str | None = None
        self.wrong_class = False
        self.text_view = lambda text: text

    def record(self, name: str, value: Any) -> None:
        self.calls.append((name, value))

    def open(self, path: Path, *, readonly: bool = True) -> FakeDoc:
        self.opened.append((Path(path), readonly))
        self.doc = ezdxf.readfile(self.template)
        doc = FakeDoc(self, Path(path))
        self.docs.append(doc)
        return doc

    def save_dwg(self, doc: FakeDoc, dst: Path, version: str = "2013") -> None:
        self.saved.append((Path(dst), version))
        Path(dst).write_bytes(b"AC1032 edited drawing")
        self.doc.saveas(self.store / "after-edit.dxf")
        doc.close()

    def export_dxf(self, src: Path, dst: Path, version: str = "2013") -> list[str]:
        self.exports.append(Path(src))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.store / "after-edit.dxf", dst)
        return []

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture
def cad(tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCad, Path, Path]:
    """A fake CAD, a dummy DWG and the DXF the fake 'exports' for it; hooks installed."""
    template = work / "sheet_set.dxf"
    dwg = tmp_path / "drawing.dwg"
    dwg.write_bytes(b"AC1032" + b"\0" * 64)
    fake = FakeCad(template, tmp_path)
    monkeypatch.setattr(acad_edit, "start_session", lambda timeout, log: fake)
    monkeypatch.setattr(acad_edit, "_point", lambda v: tuple(float(x) for x in v))
    started: list[Any] = []

    def fake_ensure(src: Path, ctx: Any, **kwargs: Any) -> DxfResult:
        assert kwargs.get("prefer") == "com" and kwargs.get("allow_com") is True
        started.append(kwargs.get("session"))
        dst = ctx.path("converted/base.dxf")
        shutil.copyfile(fake.template, dst)
        return DxfResult(dst, "com")

    monkeypatch.setattr(convert, "ensure_dxf", fake_ensure)
    monkeypatch.setattr(runs_mod, "cache_get", lambda *a, **k: None)
    return fake, dwg, template


def dwg_spec(dwg: Path, template: Path, edits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "base": {"path": str(dwg), "sha1": sha1_of(dwg)}, "edits": edits}


def test_com_replace_text_round_trip(
    cad: tuple[FakeCad, Path, Path], runs: Path, sheet_truth: dict[str, Any]
) -> None:
    fake, dwg, template = cad
    before = sha1_of(dwg)
    h_a, h_b = date_handles(sheet_truth)
    note = occurrence(sheet_truth, "attrib_value")
    spec = dwg_spec(
        dwg,
        template,
        [
            E(template, h_a, "replace-text", DATE_EDIT),
            E(template, h_b, "replace-text", {"old": "2026-01-16", "new": "2026-10-01"}),
            E(template, note, "replace-text", {"old": "60", "new": "90"}),
        ],
    )
    res = run_edit(spec, runs, "--allow-com")
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    assert res.backend == "com" and res.summary["verified"] is True
    assert sha1_of(dwg) == before  # the original is never opened for writing
    assert len(fake.opened) == 1
    opened, readonly = fake.opened[0]
    assert readonly is False and opened != dwg and opened.name == dwg.name  # a staged copy
    assert [v for _p, v in fake.saved] == ["2018"]  # the source's DWG version is kept
    edited = Path(res.outputs["edited"]["path"])
    assert edited.name == "drawing_edited.dwg" and edited.is_file()
    assert fake.exports == [edited]
    assert fake.quit_called
    assert [c[1] for c in fake.calls if c[0] == "TextString"] == [
        "2026-10-01",
        "2026-10-01",
        "FIRE RATING 90",
    ]
    assert any("version 2018" in w for w in res.warnings)
    assert fake.docs[0].closed


def test_com_set_props_move_clone_delete(
    cad: tuple[FakeCad, Path, Path], runs: Path, sheet_truth: dict[str, Any]
) -> None:
    fake, dwg, template = cad
    text = occurrence(sheet_truth, "text_model_visible")
    other = date_handles(sheet_truth)[1]
    spec = dwg_spec(
        dwg,
        template,
        [
            E(
                template,
                text,
                "set-props",
                {"props": {"layer": "A-FURN", "color": 5, "height": 250, "rotation": 90}},
                id="p",
            ),
            E(template, text, "clone", {"vector": [0, -500]}, id="c"),
            E(template, other, "delete", id="d"),
            E(
                template,
                occurrence(sheet_truth, "mtext_paper_space"),
                "move",
                {"vector": [10, 5]},
                id="m",
            ),
        ],
    )
    res = run_edit(spec, runs, "--allow-com")
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    by_name = {}
    for name, value in fake.calls:
        by_name.setdefault(name, []).append(value)
    assert by_name["Layer"] == ["A-FURN"]
    assert by_name["color"] == [5]
    assert by_name["Height"] == [250]
    assert by_name["Rotation"] == [pytest.approx(math.pi / 2)]  # COM rotation is in radians
    assert by_name["Delete"] == [other]
    assert by_name["Copy"] == [text]
    assert ((0.0, 0.0, 0.0), (10.0, 5.0, 0.0)) in by_name["Move"]
    lines = {x["id"]: x for x in read_jsonl(res, "changes")}
    assert lines["c"]["new_handle"] and lines["c"]["status"] == "applied"
    assert lines["p"]["after"]["rotation"] == pytest.approx(90.0)  # reported in degrees again
    assert res.summary["unintended"] == 0 and res.summary["verified"] is True


def test_com_pan_viewport_is_unsupported_with_a_hint(
    cad: tuple[FakeCad, Path, Path], runs: Path, sheet_truth: dict[str, Any]
) -> None:
    fake, dwg, template = cad
    vp = sheet_truth["layouts"][0]["viewports"][0]["handle"]
    spec = dwg_spec(dwg, template, [E(template, vp, "pan-viewport", {"view_center": [1, 2]})])
    res = run_edit(spec, runs, "--allow-com")
    assert res.exit_code == ExitCode.BAD_ARGS and res.errors[0]["code"] == "UNSUPPORTED"
    assert "DXF" in res.errors[0]["hint"]
    assert fake.opened == []  # nothing was opened for writing
    assert fake.quit_called  # the instance started for the baseline export is closed again


def test_com_clone_into_another_space_is_unsupported(
    cad: tuple[FakeCad, Path, Path], runs: Path, sheet_truth: dict[str, Any]
) -> None:
    fake, dwg, template = cad
    text = occurrence(sheet_truth, "text_model_visible")
    spec = dwg_spec(
        dwg, template, [E(template, text, "clone", {"vector": [1, 1], "target_space": "Sheet-A"})]
    )
    res = run_edit(spec, runs, "--allow-com")
    assert res.errors[0]["code"] == "UNSUPPORTED" and fake.opened == []


def test_com_failure_discards_everything(
    cad: tuple[FakeCad, Path, Path], runs: Path, sheet_truth: dict[str, Any]
) -> None:
    fake, dwg, template = cad
    fake.fail_on = "Move"
    spec = dwg_spec(
        dwg,
        template,
        [
            E(template, date_handles(sheet_truth)[0], "replace-text", DATE_EDIT, id="ok"),
            E(
                template,
                occurrence(sheet_truth, "text_model_visible"),
                "move",
                {"vector": [1, 1]},
                id="boom",
            ),
        ],
    )
    res = run_edit(spec, runs, "--allow-com")
    assert res.exit_code == ExitCode.ERROR and res.errors[0]["code"] == "COM_ERROR"
    assert fake.saved == [] and "edited" not in res.outputs
    assert fake.docs[0].closed and fake.quit_called
    statuses = {x["id"]: x["status"] for x in read_jsonl(res, "changes")}
    assert statuses == {"ok": "applied", "boom": "failed"}
    assert res.summary["applied"] == 0 and res.summary["discarded"] == 1


def test_com_checks_the_object_class_before_changing_anything(
    cad: tuple[FakeCad, Path, Path], runs: Path, sheet_truth: dict[str, Any]
) -> None:
    fake, dwg, template = cad
    fake.wrong_class = True
    spec = dwg_spec(
        dwg, template, [E(template, date_handles(sheet_truth)[0], "replace-text", DATE_EDIT)]
    )
    res = run_edit(spec, runs, "--allow-com")
    assert res.exit_code == ExitCode.ERROR and res.errors[0]["code"] == "COM_ERROR"
    assert [c for c in fake.calls if c[0] == "TextString"] == []
    assert fake.saved == []


def test_com_text_with_unicode_escapes_in_the_plan_matches_decoded_cad_text(
    cad: tuple[FakeCad, Path, Path], runs: Path, work: Path, truth: dict[str, Any]
) -> None:
    """COM returns characters where the DXF stores ``\\U+XXXX``; the plan carries the raw DXF text."""
    fake, dwg, _template = cad
    mt_template = work / "mtext_cases.dxf"
    fake.template = mt_template
    fake.text_view = lambda text: text.replace("\\U+00B0", "°")
    handle = mtext_handle(truth, "inline_codes")
    spec = dwg_spec(
        dwg,
        mt_template,
        [E(mt_template, handle, "replace-text", {"old": "\\U+00B0C", "new": "\\U+00B0F"})],
    )
    res = run_edit(spec, runs, "--allow-com")
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    sent = [c[1] for c in fake.calls if c[0] == "TextString"]
    assert sent and "°F" in sent[0]  # decoded form was used for the COM string


def test_com_helpers() -> None:
    assert acad_edit.com_value("rotation", 180) == pytest.approx(math.pi)
    assert acad_edit.from_com_value("rotation", math.pi / 2) == pytest.approx(90.0)
    assert acad_edit.com_value("height", 3.5) == 3.5
    assert acad_edit.com_member("MTEXT", "width") == "Width"
    assert acad_edit.com_member("TEXT", "width") == "ScaleFactor"
    assert acad_edit.com_member("TEXT", "color") == "color"


def test_dwg_save_version_follows_the_source(tmp_path: Path) -> None:
    for header, version in ((b"AC1027", "2013"), (b"AC1032", "2018"), (b"AC1015", "2000")):
        p = tmp_path / f"{version}.dwg"
        p.write_bytes(header + b"rest")
        assert acad_edit.dwg_save_version(p) == (version, True)
    odd = tmp_path / "odd.dwg"
    odd.write_bytes(b"XXXXXXrest")
    assert acad_edit.dwg_save_version(odd) == ("2013", False)
    assert acad_edit.dwg_save_version(tmp_path / "missing.dwg") == ("2013", False)


def test_modules_import_without_com_or_jsonschema() -> None:
    """The backends import on Linux/macOS: no win32 module and no jsonschema at import time."""
    import subprocess

    lines = [
        "import sys",
        f"sys.path.insert(0, {str(SKILL / 'scripts')!r})",
        "from cadlib import edit, edit_dxf, acad_edit",
        "bad = sorted({'win32com', 'pythoncom', 'pywintypes', 'jsonschema'} & set(sys.modules))",
        "assert not bad, bad",
        "print('ok')",
    ]
    script = "\n".join(lines)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


# --------------------------------------------------------------------------------------
# more ezdxf-backend cases: block definitions, column MTEXT, rotated inserts
# --------------------------------------------------------------------------------------


def test_replace_text_inside_a_block_definition(
    work: Path, runs: Path, sheet_truth: dict[str, Any]
) -> None:
    src = work / "sheet_set.dxf"
    handle = occurrence(sheet_truth, "text_in_unused_block")
    spec = make_spec(src, [E(src, handle, "replace-text", {"old": "FIRE", "new": "SMOKE"})])
    assert spec["edits"][0]["expect"]["space"] == "block:UNUSED_SYMBOL"
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    assert entity_at(edited_doc(res), handle).dxf.text == "SMOKE"
    assert res.summary["unintended"] == 0


def test_replace_text_in_a_column_mtext_keeps_both_columns(
    work: Path, runs: Path, truth: dict[str, Any]
) -> None:
    src = work / "mtext_cases.dxf"
    handle = mtext_handle(truth, "two_columns")
    spec = make_spec(src, [E(src, handle, "replace-text", {"old": "second", "new": "SECOND"})])
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    assert entity_at(edited_doc(res), handle).text == "first column text\\NSECOND column text"


def test_attribs_of_a_rotated_insert(work: Path, runs: Path, truth: dict[str, Any]) -> None:
    src = work / "blocks_attribs.dxf"
    insert = truth["files"]["blocks_attribs.dxf"]["inserts"][3]
    name = insert["attribs"]["ROOM_NAME"]["handle"]
    spec = make_spec(
        src,
        [
            E(src, name, "replace-text", {"old": "WC", "new": "TOILET"}),
            E(src, insert["handle"], "move", {"vector": [0, 300]}),
        ],
    )
    res = run_edit(spec, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary)
    assert entity_at(edited_doc(res), name).dxf.text == "TOILET"
