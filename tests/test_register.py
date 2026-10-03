"""register: least-squares transform from control points, residuals, independent check points."""

from __future__ import annotations

import argparse
import cmath
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import register
from cadlib.result import ERROR_CODES, CadError, ExitCode, Result


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    monkeypatch.setenv("CAD_DRAWINGS_RUNS", str(base))
    return base


def run_register(argv: list[str], runs: Path) -> Result:
    parser = argparse.ArgumentParser()
    command = register.COMMANDS["register"]
    command.add_arguments(parser)
    return command.run(parser.parse_args([*argv, "--run-dir", str(runs)]))


def report(result: Result) -> dict[str, Any]:
    return json.loads(Path(result.outputs["registration"]["path"]).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def move(
    points: list[tuple[float, float]], scale: float, deg: float, tx: float, ty: float
) -> list[tuple[float, float]]:
    a = scale * cmath.exp(1j * math.radians(deg))
    out = []
    for x, y in points:
        w = a * complex(x, y) + complex(tx, ty)
        out.append((w.real, w.imag))
    return out


def pair_args(
    src: list[tuple[float, float]], dst: list[tuple[float, float]], flag: str = "--pair"
) -> list[str]:
    args: list[str] = []
    for (x, y), (u, v) in zip(src, dst, strict=True):
        args += [flag, f"{x!r},{y!r}:{u!r},{v!r}"]
    return args


SRC = [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 7.0)]


# --------------------------------------------------------------------------------------
# the fit
# --------------------------------------------------------------------------------------


def test_translation_only() -> None:
    fit = register.fit([(0, 0), (10, 0)], [(5, 2), (15, 2)], "translation")
    assert fit.scale == 1.0 and fit.rotation_deg == 0.0
    assert (fit.tx, fit.ty) == pytest.approx((5.0, 2.0))
    assert fit.max_residual == pytest.approx(0.0, abs=1e-12)


def test_similarity_recovers_scale_rotation_and_translation() -> None:
    dst = move(SRC, 2.5, 90.0, 100.0, -40.0)
    fit = register.fit(SRC, dst, "similarity")
    assert fit.scale == pytest.approx(2.5, rel=1e-12)
    assert fit.rotation_deg == pytest.approx(90.0, abs=1e-9)
    assert (fit.tx, fit.ty) == pytest.approx((100.0, -40.0), abs=1e-9)
    assert fit.rms == pytest.approx(0.0, abs=1e-9)


def test_scale_translation_keeps_the_rotation_at_zero() -> None:
    dst = move(SRC, 1000.0, 0.0, 5.0, 6.0)
    fit = register.fit(SRC, dst, "scale-translation")
    assert fit.scale == pytest.approx(1000.0, rel=1e-12) and fit.rotation_deg == 0.0
    assert fit.max_residual == pytest.approx(0.0, abs=1e-9)


def test_noisy_points_leave_residuals_and_a_least_squares_fit() -> None:
    dst = move(SRC, 1.0, 30.0, 10.0, 10.0)
    noisy = [
        (x + dx, y + dy)
        for (x, y), (dx, dy) in zip(
            dst, [(0.01, 0), (0, -0.01), (-0.01, 0), (0, 0.01)], strict=True
        )
    ]
    fit = register.fit(SRC, noisy, "similarity")
    assert 0.0 < fit.rms < 0.02
    assert fit.scale == pytest.approx(1.0, abs=0.01) and fit.rotation_deg == pytest.approx(
        30.0, abs=0.2
    )
    assert len(fit.residuals) == 4


def test_apply_maps_points() -> None:
    fit = register.fit(SRC, move(SRC, 2.0, 0.0, 1.0, 1.0), "similarity")
    assert register.apply(fit, [(3.0, 4.0)])[0] == pytest.approx((7.0, 9.0))


def test_coincident_source_points_cannot_define_a_transform() -> None:
    with pytest.raises(CadError) as err:
        register.fit([(1, 1), (1, 1)], [(0, 0), (5, 5)], "similarity")
    assert err.value.code == "BAD_ARGS"


def test_too_few_points_are_refused() -> None:
    with pytest.raises(CadError) as err:
        register.fit([(0, 0)], [(1, 1)], "similarity")
    assert err.value.code == "BAD_ARGS"
    register.fit([(0, 0)], [(1, 1)], "translation")  # one point is enough for a shift


def test_a_mirrored_target_is_recognised() -> None:
    mirrored = [(x, -y) for x, y in SRC]
    fit = register.fit(SRC, mirrored, "similarity")
    assert fit.mirror_better is True
    assert register.fit(SRC, move(SRC, 1.0, 20.0, 0.0, 0.0), "similarity").mirror_better is False


def test_scale_hint_names_a_unit_change() -> None:
    assert "m to mm" in (register.scale_hint(1000.0) or "")
    assert "in to mm" in (register.scale_hint(25.4) or "")
    assert register.scale_hint(1.0) is None
    assert register.scale_hint(3.7) is None


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def test_command_reports_transform_and_residuals(runs: Path) -> None:
    dst = move(SRC, 2.0, 0.0, 10.0, 20.0)
    result = run_register(pair_args(SRC, dst), runs)
    assert result.exit_code == ExitCode.OK
    s = result.summary
    assert s["scale"] == pytest.approx(2.0) and s["translation"] == pytest.approx([10.0, 20.0])
    assert s["points"] == 4 and s["rms"] == pytest.approx(0.0, abs=1e-9)
    rep = report(result)
    assert len(rep["residuals"]) == 4 and rep["matrix"][0][2] == pytest.approx(10.0)


def test_two_points_fit_exactly_and_the_command_says_so(runs: Path) -> None:
    dst = move(SRC[:2], 1.0, 10.0, 0.0, 0.0)
    result = run_register(pair_args(SRC[:2], dst), runs)
    assert any("exact" in w.lower() and "--check" in w for w in result.warnings)
    three = run_register(pair_args(SRC[:3], move(SRC[:3], 1.0, 10.0, 0.0, 0.0)), runs)
    assert not any("exact" in w.lower() for w in three.warnings)


def test_check_points_are_validated_against_the_fit(runs: Path) -> None:
    control = SRC[:2]
    extra = (3.0, 9.0)
    good = move([*control, extra], 1.0, 15.0, 4.0, 4.0)
    ok = run_register(
        [*pair_args(control, good[:2]), *pair_args([extra], [good[2]], "--check")], runs
    )
    assert ok.summary["check"]["max"] == pytest.approx(0.0, abs=1e-9)
    bad_target = (good[2][0] + 0.5, good[2][1])
    off = run_register(
        [*pair_args(control, good[:2]), *pair_args([extra], [bad_target], "--check")], runs
    )
    assert off.summary["check"]["max"] == pytest.approx(0.5, abs=1e-9)
    assert off.exit_code == ExitCode.OK  # no tolerance given: numbers only


def test_tolerance_turns_a_bad_check_into_exit_7(runs: Path) -> None:
    control = SRC[:2]
    extra = (3.0, 9.0)
    good = move([*control, extra], 1.0, 15.0, 4.0, 4.0)
    args = [
        *pair_args(control, good[:2]),
        *pair_args([extra], [(good[2][0] + 0.5, good[2][1])], "--check"),
        "--tolerance",
        "0.1",
    ]
    result = run_register(args, runs)
    assert result.exit_code == ExitCode.PARTIAL
    assert [e["code"] for e in result.errors] == ["REGISTRATION_POOR"]
    assert ERROR_CODES["REGISTRATION_POOR"] == ExitCode.PARTIAL
    loose = run_register([*args[:-1], "5"], runs)
    assert loose.exit_code == ExitCode.OK


def test_tolerance_also_applies_to_control_point_residuals(runs: Path) -> None:
    dst = move(SRC, 1.0, 0.0, 0.0, 0.0)
    dst[3] = (dst[3][0] + 1.0, dst[3][1])
    result = run_register([*pair_args(SRC, dst), "--tolerance", "0.1"], runs)
    assert result.exit_code == ExitCode.PARTIAL


def test_apply_points_are_mapped_in_the_report(runs: Path) -> None:
    dst = move(SRC, 2.0, 0.0, 1.0, 1.0)
    result = run_register([*pair_args(SRC, dst), "--apply", "3,4"], runs)
    assert report(result)["applied"][0]["to"] == pytest.approx([7.0, 9.0])


def test_bad_pair_text_is_bad_args(runs: Path) -> None:
    with pytest.raises(CadError) as err:
        run_register(["--pair", "1,2-3,4", "--pair", "5,6:7,8"], runs)
    assert err.value.code == "BAD_ARGS"


def test_no_pairs_is_bad_args(runs: Path) -> None:
    with pytest.raises(CadError) as err:
        run_register([], runs)
    assert err.value.code == "BAD_ARGS"


def test_scale_hint_and_mirror_reach_the_warnings(runs: Path) -> None:
    result = run_register(pair_args(SRC, move(SRC, 1000.0, 0.0, 0.0, 0.0)), runs)
    assert any("m to mm" in w for w in result.warnings)
    mirrored = run_register(
        pair_args(SRC, [(x, -y) for x, y in SRC]) + ["--tolerance", "0.1"], runs
    )
    assert any("mirror" in w.lower() for w in mirrored.warnings)


def test_summary_stays_small(runs: Path) -> None:
    result = run_register(pair_args(SRC, move(SRC, 1.0, 5.0, 1.0, 1.0)), runs)
    assert len(json.dumps(result.summary)) < 1500
