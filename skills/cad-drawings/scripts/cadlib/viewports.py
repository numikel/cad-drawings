"""Viewport model: the display-coordinate maths and what a window contains."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .util import _EPS


@dataclass(frozen=True)
class ViewportInfo:
    handle: str
    vp_id: int
    status: int
    center: tuple[float, float]
    size: tuple[float, float]
    view_center: tuple[float, float]  # as stored (display coordinate system)
    view_height: float
    twist: float
    frozen_layers: tuple[str, ...]
    top_view: bool
    clipped: bool  # non-rectangular clipping boundary (the rectangle is only an approximation)
    overall: bool = False
    target: tuple[float, float] = (0.0, 0.0)  # view target point (origin for plan views)

    @property
    def scale(self) -> float:
        """View height per paper height: model units per paper unit (0.0 when undefined)."""
        return self.view_height / self.size[1] if self.size[1] else 0.0

    @property
    def status_reliable(self) -> bool:
        return self.status > 0

    @property
    def usable(self) -> bool:
        return self.top_view and self.size[0] > 0 and self.size[1] > 0 and self.view_height > 0

    @property
    def view_center_wcs(self) -> tuple[float, float]:
        """Model-space point at the middle of the window.

        The stored view centre is in the display coordinate system:
        ``DCS = R(+twist) * (WCS - target)`` (verified against CAD plots).
        """
        a = math.radians(self.twist)
        ca, sa = math.cos(a), math.sin(a)
        cx, cy = self.view_center
        return (self.target[0] + cx * ca + cy * sa, self.target[1] - cx * sa + cy * ca)

    def model_to_paper(self, x: float, y: float) -> tuple[float, float]:
        """Paper-space position of a model-space point seen through this viewport."""
        s = self.size[1] / self.view_height  # paper units per model unit
        a = math.radians(self.twist)
        ca, sa = math.cos(a), math.sin(a)
        px, py = x - self.target[0], y - self.target[1]
        dx = s * (px * ca - py * sa - self.view_center[0])
        dy = s * (px * sa + py * ca - self.view_center[1])
        return (self.center[0] + dx, self.center[1] + dy)

    def contains_model_point(self, x: float, y: float) -> bool:
        px, py = self.model_to_paper(x, y)
        tol = _EPS * max(1.0, self.size[0], self.size[1])
        return (
            abs(px - self.center[0]) <= self.size[0] / 2 + tol
            and abs(py - self.center[1]) <= self.size[1] / 2 + tol
        )

    def intersects_model_extent(self, extent: tuple[float, float, float, float]) -> bool:
        """Whether a model-space rectangle (x1, y1, x2, y2; may be a point) meets the window.

        Separating-axis test between the viewport rectangle on paper and the image of the
        rectangle under the (rotating) model-to-paper map.
        """
        x1, y1, x2, y2 = extent
        poly = [self.model_to_paper(x, y) for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
        hw, hh = self.size[0] / 2, self.size[1] / 2
        tol = _EPS * max(1.0, self.size[0], self.size[1])
        rect = [
            (self.center[0] + sx * hw, self.center[1] + sy * hh)
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
        ]
        axes = [(1.0, 0.0), (0.0, 1.0)]
        for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1], strict=True):
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy)
            if norm > 1e-12:
                axes.append((-dy / norm, dx / norm))
        for ux, uy in axes:
            pa = [ux * x + uy * y for x, y in poly]
            pb = [ux * x + uy * y for x, y in rect]
            if max(pa) < min(pb) - tol or max(pb) < min(pa) - tol:
                return False
        return True

    def freezes(self, layer: str) -> bool:
        low = layer.lower()
        return any(name.lower() == low for name in self.frozen_layers)


def _viewport_info(vp: Any) -> ViewportInfo:
    d = vp.dxf
    return ViewportInfo(
        handle=str(d.handle),
        vp_id=int(d.get("id", 0)),
        status=int(d.get("status", 0)),
        center=(float(d.center.x), float(d.center.y)),
        size=(float(d.width), float(d.height)),
        view_center=(float(d.view_center_point.x), float(d.view_center_point.y)),
        view_height=float(d.view_height),
        twist=float(d.get("view_twist_angle", 0.0)),
        frozen_layers=tuple(vp.frozen_layers),
        top_view=bool(vp.is_top_view),
        clipped=bool(vp.has_extended_clipping_path),
        target=(float(d.view_target_point.x), float(d.view_target_point.y)),
    )


def layout_viewports(layout: Any) -> list[ViewportInfo]:
    """All viewports of a paper layout in file order, the overall one flagged ``overall``."""
    infos = [_viewport_info(vp) for vp in layout.query("VIEWPORT")]
    overall: int | None = None
    for i, vp in enumerate(infos):
        if vp.vp_id == 1:
            overall = i
            break
    if overall is None:
        for i, vp in enumerate(infos):
            same_center = (
                abs(vp.view_center[0] - vp.center[0]) < _EPS
                and abs(vp.view_center[1] - vp.center[1]) < _EPS
            )
            if same_center and abs(vp.view_height - vp.size[1]) < _EPS * max(1.0, vp.size[1]):
                overall = i
                break
    if overall is not None:
        infos[overall] = ViewportInfo(**{**infos[overall].__dict__, "overall": True})
    return infos
