"""register: the transform between two drawings from control points, with residuals.

Pure arithmetic on coordinates the caller supplies (no drawing is opened): the least-squares
fit of a translation, a scale and shift, or a similarity (scale, rotation, shift), using complex
numbers so no linear-algebra package is needed. The residuals say how well the control points
agree; points given with ``--check`` are not used in the fit and validate it independently.
"""

from __future__ import annotations

import argparse
import cmath
import math
import re
from dataclasses import dataclass
from typing import Any

from .command import Command
from .result import CadError, ExitCode, Result
from .util import _add_common, _finish, _new_run, _write_json

Point = tuple[float, float]
MODELS = ("translation", "scale-translation", "similarity")
MIN_POINTS = {"translation": 1, "scale-translation": 2, "similarity": 2}
SCALE_HINTS = {
    1000.0: "m to mm",
    100.0: "m to cm",
    10.0: "cm to mm",
    25.4: "in to mm",
    304.8: "ft to mm",
    0.001: "mm to m",
    0.01: "cm to m",
    0.1: "mm to cm",
    1 / 25.4: "mm to in",
    1 / 304.8: "mm to ft",
}
HINT_TOLERANCE = 0.01  # relative
_NUMBER = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
_PAIR = re.compile(rf"^\s*{_NUMBER}\s*,\s*{_NUMBER}\s*:\s*{_NUMBER}\s*,\s*{_NUMBER}\s*$")
_POINT = re.compile(rf"^\s*{_NUMBER}\s*,\s*{_NUMBER}\s*$")


@dataclass(frozen=True)
class Fit:
    model: str
    scale: float
    rotation_deg: float
    tx: float
    ty: float
    residuals: list[float]
    rms: float
    max_residual: float
    mirror_better: bool = False

    @property
    def a(self) -> complex:
        return self.scale * cmath.exp(1j * math.radians(self.rotation_deg))


def _solve(src: list[complex], dst: list[complex], model: str) -> tuple[complex, complex]:
    zbar = sum(src) / len(src)
    wbar = sum(dst) / len(dst)
    zc = [z - zbar for z in src]
    wc = [w - wbar for w in dst]
    if model == "translation":
        return 1 + 0j, wbar - zbar
    norm = sum(abs(z) ** 2 for z in zc)
    if norm < 1e-18:
        raise CadError(
            "BAD_ARGS",
            "the source points coincide: they cannot define a scale or a rotation",
            hint="pick control points that are far apart in the first drawing",
        )
    cross = sum(z.conjugate() * w for z, w in zip(zc, wc, strict=True))
    a = complex(cross.real / norm, 0.0) if model == "scale-translation" else cross / norm
    return a, wbar - a * zbar


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


def fit(src: list[Point], dst: list[Point], model: str = "similarity") -> Fit:
    """Least-squares transform ``dst = a * src + t`` (points as complex numbers)."""
    if model not in MODELS:
        raise CadError(
            "BAD_ARGS", f"unknown model {model!r}", hint="use one of: " + ", ".join(MODELS)
        )
    if len(src) != len(dst):
        raise CadError("BAD_ARGS", "every source point needs a target point")
    if len(src) < MIN_POINTS[model]:
        raise CadError(
            "BAD_ARGS",
            f"{model} needs at least {MIN_POINTS[model]} control point(s), got {len(src)}",
        )
    z = [complex(*p) for p in src]
    w = [complex(*p) for p in dst]
    a, t = _solve(z, w, model)
    residuals = [abs(a * zi + t - wi) for zi, wi in zip(z, w, strict=True)]
    rms = _rms(residuals)
    mirror_better = False
    if model == "similarity" and rms > 1e-9:
        zm = [zi.conjugate() for zi in z]
        am, tm = _solve(zm, w, "similarity")
        mirrored = _rms([abs(am * zi + tm - wi) for zi, wi in zip(zm, w, strict=True)])
        mirror_better = mirrored < rms / 2
    return Fit(
        model=model,
        scale=abs(a),
        rotation_deg=math.degrees(cmath.phase(a)) if abs(a.imag) > 1e-15 else 0.0,
        tx=t.real,
        ty=t.imag,
        residuals=residuals,
        rms=rms,
        max_residual=max(residuals),
        mirror_better=mirror_better,
    )


def apply(f: Fit, points: list[Point]) -> list[Point]:
    out = []
    for x, y in points:
        w = f.a * complex(x, y) + complex(f.tx, f.ty)
        out.append((w.real, w.imag))
    return out


def scale_hint(scale: float) -> str | None:
    for ratio, text in SCALE_HINTS.items():
        if abs(scale / ratio - 1) <= HINT_TOLERANCE:
            return f"scale {scale:g} looks like a change of unit ({text})"
    return None


# --------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------


def _parse_pairs(texts: list[str] | None, flag: str) -> tuple[list[Point], list[Point]]:
    src: list[Point] = []
    dst: list[Point] = []
    for text in texts or []:
        m = _PAIR.match(text)
        if not m:
            raise CadError("BAD_ARGS", f"{flag} must look like 'x,y:X,Y', got {text!r}")
        x, y, u, v = (float(g) for g in m.groups())
        src.append((x, y))
        dst.append((u, v))
    return src, dst


def _parse_points(texts: list[str] | None) -> list[Point]:
    out: list[Point] = []
    for text in texts or []:
        m = _POINT.match(text)
        if not m:
            raise CadError("BAD_ARGS", f"--apply must look like 'x,y', got {text!r}")
        out.append((float(m.group(1)), float(m.group(2))))
    return out


def _r(value: float) -> float:
    return round(value, 9)


def _run_register(args: argparse.Namespace) -> Result:
    src, dst = _parse_pairs(args.pair, "--pair")
    if not src:
        raise CadError("BAD_ARGS", "give control points with --pair 'x,y:X,Y' (repeatable)")
    check_src, check_dst = _parse_pairs(args.check, "--check")
    apply_points = _parse_points(args.apply)
    ctx = _new_run("register", args)
    result = Result("register", backend="none")
    f = fit(src, dst, args.model)
    check_res = [math.dist(p, q) for p, q in zip(apply(f, check_src), check_dst, strict=True)]
    report: dict[str, Any] = {
        "model": f.model,
        "scale": _r(f.scale),
        "rotation_deg": _r(f.rotation_deg),
        "translation": [_r(f.tx), _r(f.ty)],
        "matrix": [
            [_r(f.a.real), _r(-f.a.imag), _r(f.tx)],
            [_r(f.a.imag), _r(f.a.real), _r(f.ty)],
        ],
        "residuals": [
            {"from": list(s), "to": list(d), "residual": _r(r)}
            for s, d, r in zip(src, dst, f.residuals, strict=True)
        ],
        "check": [
            {"from": list(s), "to": list(d), "residual": _r(r)}
            for s, d, r in zip(check_src, check_dst, check_res, strict=True)
        ],
        "applied": [
            {"from": list(p), "to": [_r(q[0]), _r(q[1])]}
            for p, q in zip(apply_points, apply(f, apply_points), strict=True)
        ],
    }
    path = ctx.path("registration.json")
    _write_json(path, report)
    ctx.add_output(result, "registration", path)
    result.summary = {
        "model": f.model,
        "points": len(src),
        "scale": _r(f.scale),
        "rotation_deg": _r(f.rotation_deg),
        "translation": [_r(f.tx), _r(f.ty)],
        "rms": _r(f.rms),
        "max": _r(f.max_residual),
    }
    if check_res:
        result.summary["check"] = {
            "points": len(check_res),
            "rms": _r(_rms(check_res)),
            "max": _r(max(check_res)),
        }
    if len(src) == MIN_POINTS[f.model]:
        result.warn(
            f"{len(src)} control point(s) determine the {f.model} fit exactly: the residuals are 0 "
            "and prove nothing; add an independent point with --check to validate it"
        )
    hint = scale_hint(f.scale) if f.model != "translation" else None
    if hint:
        result.warn(hint)
    if f.mirror_better:
        result.warn(
            "a mirrored fit matches the points better: check the direction of the axes "
            "(one drawing may be flipped); this command does not fit reflections"
        )
    worst = max([f.max_residual, *check_res])
    if args.tolerance is not None and worst > args.tolerance:
        result.exit_code = ExitCode.PARTIAL
        result.add_error(
            CadError(
                "REGISTRATION_POOR",
                f"largest residual {worst:.6g} exceeds the tolerance {args.tolerance:g}",
                hint="re-measure the control points, drop the outlier, or check for a mirrored or twisted drawing",
            )
        )
    return _finish(result, ctx)


def _add_register_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--pair",
        action="append",
        help="control point 'x,y:X,Y' (drawing A : drawing B), repeatable",
    )
    p.add_argument(
        "--check",
        action="append",
        help="independent point 'x,y:X,Y' to validate the fit, repeatable",
    )
    p.add_argument(
        "--apply",
        action="append",
        help="point 'x,y' of drawing A to map into drawing B, repeatable",
    )
    p.add_argument(
        "--model",
        choices=MODELS,
        default="similarity",
        help="similarity (default) = scale, rotation, shift",
    )
    p.add_argument(
        "--tolerance", type=float, help="exit 7 when a residual exceeds this (drawing B units)"
    )
    _add_common(p)


COMMANDS = {
    "register": Command(
        help="transform between two drawings from control points, with residuals and a check",
        add_arguments=_add_register_args,
        run=_run_register,
        epilog="example: cad.py register --pair 0,0:5000,2000 --pair 100,0:5100,2000 --pair 0,50:5000,2050",
    ),
}
