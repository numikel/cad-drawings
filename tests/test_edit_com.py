"""edit through a real CAD application (COM): a synthetic DWG, a plan built from ``find`` output.

Marked ``com``: run manually, one at a time, only when no CAD work is in progress:

    .venv/Scripts/python.exe -m pytest tests/test_edit_com.py -o addopts="" -m com -q

The conftest queues these tests machine-wide. The drawings are the synthetic fixtures; the DWG
is made from the sheet-set DXF by the test itself.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import acad, dxf, edit
from cadlib.result import ExitCode, Result

pytestmark = [
    pytest.mark.com,
    pytest.mark.skipif(sys.platform != "win32", reason="COM automation is Windows only"),
]

NEW_DATE = "2026-10-01"


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def cad_pids() -> set[int]:
    """PIDs of running ``acad.exe`` processes (empty when tasklist cannot be read)."""
    proc = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq acad.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: set[int] = set()
    for row in csv.reader(io.StringIO(proc.stdout)):
        if len(row) > 1 and row[0].lower() == "acad.exe":
            pids.add(int(row[1]))
    return pids


def run(module: Any, name: str, argv: list[str]) -> Result:
    parser = argparse.ArgumentParser()
    module.COMMANDS[name].add_arguments(parser)
    return module.COMMANDS[name].run(parser.parse_args(argv))


def jsonl(result: Result, key: str) -> list[dict[str, Any]]:
    text = Path(result.outputs[key]["path"]).read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x]


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


@pytest.fixture
def dwg(tmp_path: Path, fixtures_dir: Path) -> Path:
    """The synthetic sheet set as a DWG, saved by our own CAD session (SaveAs)."""
    staged = tmp_path / "staged" / "sheet_set.dxf"
    staged.parent.mkdir()
    shutil.copyfile(fixtures_dir / "sheet_set.dxf", staged)
    target = tmp_path / "sheet_set.dwg"
    with acad.AcadSession.start() as session:
        doc = session.open(staged, readonly=True)
        session.save_dwg(doc, target, "2018")
    assert target.is_file() and target.read_bytes()[:6] == b"AC1032"
    return target


def find_hits(dwg: Path, runs: Path, pattern: str) -> list[dict[str, Any]]:
    res = run(
        dxf,
        "find",
        [str(dwg), "--pattern", pattern, "--allow-com", "--backend", "com", "--run-dir", str(runs)],
    )
    assert res.exit_code == ExitCode.OK, res.errors
    return jsonl(res, "hits")


def run_edit(plan: dict[str, Any], tmp_path: Path, runs: Path, *flags: str) -> Result:
    spec = tmp_path / "plan.json"
    spec.write_text(json.dumps(plan), encoding="utf-8")
    return run(
        edit,
        "edit",
        ["--spec", str(spec), "--allow-com", "--run-dir", str(runs), "--timeout", "180", *flags],
    )


def plan_for(dwg: Path, edits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "base": {"path": str(dwg), "sha1": sha1_of(dwg)}, "edits": edits}


def edit_from_hit(hit: dict[str, Any], op: str, args: dict[str, Any], key: str) -> dict[str, Any]:
    return {"id": key, "op": op, "handle": hit["handle"], "expect": hit["expect"], "args": args}


def test_edit_dwg_changes_both_title_block_dates_and_nothing_else(
    dwg: Path, tmp_path: Path, runs: Path
) -> None:
    before_pids = cad_pids()
    original_hash = sha1_of(dwg)

    hits = find_hits(dwg, runs, r"^2026-01-1[56]$")
    dates = [h for h in hits if h["type"] == "TEXT" and h["space"] == "paper"]
    assert len(dates) == 2, [(h["layout"], h["text"]) for h in hits]
    assert {h["layout"] for h in dates} == {"Sheet-A", "Sheet-B"}
    plan = plan_for(
        dwg,
        [
            edit_from_hit(h, "replace-text", {"old": h["raw"], "new": NEW_DATE}, f"date-{i}")
            for i, h in enumerate(dates)
        ],
    )

    res = run_edit(plan, tmp_path, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary, res.warnings)
    assert res.backend == "com"
    assert res.summary["applied"] == 2 and res.summary["unintended"] == 0
    assert res.summary["verified"] is True

    # the original is untouched; the edited DWG is a new file in the run directory
    assert sha1_of(dwg) == original_hash
    edited = Path(res.outputs["edited"]["path"])
    assert edited.name == "sheet_set_edited.dwg" and edited.is_file()
    assert edited.parent == Path(res.run_dir or "") and edited != dwg
    assert edited.read_bytes()[:6] == b"AC1032"  # saved in the source's version

    lines = jsonl(res, "changes")
    assert [x["status"] for x in lines] == ["applied", "applied"]
    assert {x["before"]["text"] for x in lines} == {"2026-01-15", "2026-01-16"}
    assert {x["after"]["text"] for x in lines} == {NEW_DATE}

    # read the edited DWG back through the normal pipeline: both fields changed ...
    assert len(find_hits(edited, runs, rf"^{NEW_DATE}$")) == 2
    assert find_hits(edited, runs, r"^2026-01-1[56]$") == []
    # ... and the independent diff shows exactly those two changes
    diff = run(
        dxf,
        "diff",
        [
            str(dwg),
            str(edited),
            "--allow-com",
            "--backend",
            "com",
            "--run-dir",
            str(runs),
            "--full",
        ],
    )
    assert diff.exit_code == ExitCode.OK, diff.errors
    counts = {k: diff.summary[k] for k in ("changed", "moved", "removed", "added")}
    assert counts == {"changed": 2, "moved": 0, "removed": 0, "added": 0}, diff.summary
    changed = json.loads(Path(diff.outputs["diff"]["path"]).read_text(encoding="utf-8"))["changes"]
    assert {c["handle"] for c in changed} == {h["handle"] for h in dates}

    # no CAD process of ours remains
    assert cad_pids() <= before_pids


def test_edit_dwg_set_props_move_clone_delete_and_attrib(
    dwg: Path, tmp_path: Path, runs: Path
) -> None:
    before_pids = cad_pids()
    original_hash = sha1_of(dwg)
    hits = find_hits(dwg, runs, "FIRE")
    by_text = {h["text"]: h for h in hits if h["type"] in ("TEXT", "ATTRIB", "MTEXT")}
    exit_text = by_text["FIRE EXIT"]
    schedule = by_text["FIRE DOOR SCHEDULE"]
    rating = by_text["FIRE RATING 60"]
    notes = next(h for h in hits if h["type"] == "MTEXT")
    plan = plan_for(
        dwg,
        [
            edit_from_hit(rating, "replace-text", {"old": "60", "new": "90"}, "attrib"),
            edit_from_hit(exit_text, "set-props", {"props": {"color": 5, "height": 250}}, "props"),
            edit_from_hit(exit_text, "clone", {"vector": [0, -500]}, "clone"),
            edit_from_hit(schedule, "delete", {}, "delete"),
            edit_from_hit(notes, "move", {"vector": [10, 5]}, "move"),
        ],
    )
    res = run_edit(plan, tmp_path, runs)
    assert res.exit_code == ExitCode.OK, (res.errors, res.summary, res.warnings)
    assert sha1_of(dwg) == original_hash
    lines = {x["id"]: x for x in jsonl(res, "changes")}
    assert {k: v["status"] for k, v in lines.items()} == dict.fromkeys(plan_ids(plan), "applied")
    assert lines["clone"]["new_handle"]
    verification = json.loads(Path(res.outputs["report"]["path"]).read_text("utf-8"))[
        "verification"
    ]
    assert verification["unintended"] == [] and verification["not_as_planned"] == []
    assert cad_pids() <= before_pids


def plan_ids(plan: dict[str, Any]) -> list[str]:
    return [e["id"] for e in plan["edits"]]
