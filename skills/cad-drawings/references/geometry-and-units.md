# Geometry, units, and coordinate systems

Read this when you work with CAD drawings that may combine model and paper space, need to measure accurately, or appear to be at the wrong scale. Also read it before using `--join`, `--gap`, `register`, or baseline-based text checks.

## Spaces and coordinate systems

A drawing has multiple coordinate systems:

- **Model space**: the main drawing, referenced by layouts through viewports. All geometry exists here; model space coordinates are called WCS (World Coordinate System).
- **Paper space (layout)**: each layout (e.g., "Sheet-A") has its own paper-space coordinates. Paper space is drawn at 1:1 scale in sheet units (mm or inches).
- **Block definitions**: when a block is inserted (INSERT entity), its content exists in its own block coordinate system. An insertion with scale, rotation, or non-zero insert point transforms the block's coordinates into the space that holds the INSERT.
- **Viewport coordinate system (DCS)**: when a viewport in paper space shows model-space content, it applies a rotation (`twist`), scale, and translation. The transformation is `DCS = R(+twist) * (WCS − viewport_center)`. Entities in the viewport are rendered at this transformed location.

When measuring or comparing coordinates across different spaces or viewports, always account for:
1. Which space the entity lives in (model or paper)
2. Whether the viewport has a scale or rotation (use `dump --space <layout>` to see viewport details)
3. The block it may be nested in (use `find` to locate the parent INSERT)

Example: a line in model space at (1000, 2000) in a layout scaled 1:100 will appear at (10, 20) mm on the sheet.

## Units: $INSUNITS, $MEASUREMENT and paper units

**$INSUNITS** (integer, 0–28 in the DXF header) defines the drawing's model-space unit. Common codes:

- `0` = unitless (drawing has no inherent scale)
- `1` = inch
- `2` = foot
- `4` = millimetre
- `5` = centimetre
- `6` = metre

When `$INSUNITS = 0`, lengths cannot be converted safely: a dimension of 100 could mean 100 of anything. Use `--assume-unit` to tell the tool what unit to assume for that run (the output says "assumed").

**$MEASUREMENT** (0 or 1, integer) is a display setting: it tells AutoCAD whether to show text heights, linetype scales, and other measurements in decimal (0 = scientific/decimal notation) or fractional (1 = fractional) units in the UI dialogs. It does **not** affect how the drawing is stored, measured, or plotted. It does not alter any coordinate values or entity attributes. For actual measurement, always rely on `$INSUNITS` (the drawing's unit).

**Paper units** are separate from model units. Each layout has a `plot_paper_units` setting (0 = inches, 1 = millimetres). Paper space is always drawn at 1:1 in this unit: a line of 25.4 mm on a layout always measures 25.4 mm on paper when plotted 1:1. The `measure` command handles this automatically.

## Paper, printable area and plot origin

**The paper:** in a layout, `get_paper_limits()` returns the edge of the paper (e.g., an A4 sheet is roughly 210 × 297 mm).

**The printable area and origin:** margins around the paper are defined by the page setup. The point (0, 0) in paper-space coordinates is **not** the corner of the paper; it is the corner of the **printable area** (the area where the plotter can actually print). Specifically:
- `limmin = paper_limits[0]`, the lower-left corner of the paper in absolute units
- Printable lower-left = `limmin + (left_margin, bottom_margin)` + plot origin offset (x, y)
- Printable upper-right = `limmax − (right_margin, top_margin)` + plot origin offset
- Paper-space coordinates start at (0, 0) which is placed at the lower-left corner of the printable area

Example: an A4 in portrait (210 × 297 mm) with 10 mm margins all around and no offset:
- Paper corner: (0, 0) in absolute space
- Printable corner: (10, 10) in absolute space
- Paper-space origin (0, 0): placed at (10, 10)
- In paper-space coordinates, point (100, 50) is at (110, 60) in absolute paper space

The **plot origin offset** (a page-setup parameter) shifts where (0, 0) lands: it adjusts the position of the printable area on the physical page. This is used for fine-tuning plotter alignment.

For a layout in inches with margins in millimetres, convert: `margin_mm / 25.4 = margin_inches`.

**Rotated and scaled plots:** some plots are printed with a custom rotation (90°, 180°) or scale (e.g., fit-to-page, scale to 50%), or with centering enabled. When rotation or centering is applied, the checks for frame position on the PDF (`PDF_SHIFTED`) are skipped, because the transformation is not a simple 1:1 translation. The `qa` command reports `FRAME_CHECK_SKIPPED` in this case. However, the frame-outside-printable-area check (`FRAME_OUTSIDE_PAPER`) still runs on the drawing itself.

## Measuring: tolerance, hatches, sum rule, and joining segments

When measuring lengths or areas, the tool flattens curves (arcs, bulges, splines, ellipses) to a straight-line path within a tolerance that scales with the entity:

**Flattening tolerance** = 1e-6 × max(entity width or height)

This ensures that a 1000 mm arc is flattened to within 1 micrometre, while a small 0.1 mm detail is flattened to 0.1 nanometres. The resulting length and area are thus measured accurately for the size and detail of the entity.

**Hatches:** a HATCH entity is its boundary (closed outline) minus its islands (holes). The reported length includes both the boundary and the islands; the area subtracts the islands. A note on the result says so.

**Sum rule:** when you measure multiple entities in one command, totals are given only when all results are the same type. For example, measuring both a HATCH and the LINE outline it fills reports each separately with a warning, because a HATCH is its outline minus islands, and adding them would double-count the area. Measure each type separately to be certain.

**Joining segments with `--join`:** LINE, ARC, ELLIPSE, SPLINE, and open LWPOLYLINE/POLYLINE entities can be joined into closed contours when their endpoints touch (within a gap tolerance). Only simple loops are joined: networks with branching nodes (degree ≥ 3) and open chains are not closed and are reported as separate records with a warning. The resulting contour record has a new `type: "CONTOUR"` and a `members` array listing the handles of the segment entities that form it.

**Gap tolerance with `--join`:**
- Default: `1e-6 × max(bbox width, bbox height)` of the selected entities, in drawing units. For degenerate selections (a point or a line), a minimum of 1e-9 units applies.
- To override: `--gap 0.5` sets the tolerance to 0.5 drawing units
- If `--gap` is given without `--join`, or `--gap ≤ 0`, the command exits with exit 2 (bad arguments)
- When the gap is large (≥ 50% of the shortest segment length), a warning is issued: the joined contour may not be physically meaningful

**Choosing a gap:** when you need to join segments with visible gaps, run `measure <file> --type LINE [--layer <layer>] --join` first, without `--gap`. Every chain that stays open is reported as "open chain; ends are D apart (--gap D would close it)", with D in drawing units. Use `dump` with `--window` to look at the end points of a chain you doubt. If every reported D is a drafting slip and not a real opening, pass `--gap` with the largest D; a larger value joins more than intended.

## Degree-2 rule for joining

When joining segments, the tool searches for simple chains (sequences of segments) where every node (endpoint) has exactly degree 2 (exactly two segments meet at each point). Only these simple loops are joined into contours. If a network has a node with degree 1 (a chain end) or degree ≥ 3 (a branching point), those segments are not joined and a warning is issued:
- For branching networks: "N segments form a branching network; contour ambiguous"
- For open chains: "open chain; ends are D apart (--gap D would close it)" — the distance D shown can guide your choice of `--gap`

This rule ensures that joined contours are unambiguous closed shapes. Ambiguous networks (multiple ways to trace a contour, or open chains) are left to you to resolve.

## Large coordinates and precision

Survey drawings and imported georeferenced data often sit at very large coordinates (e.g., 1000000, 2500000). Shoelace-based area calculations (the standard method) lose precision when coordinates are in the billions: the intermediate sums grow very large while the final area is small, causing cancellation and rounding error.

To work around this, area calculations move all vertices relative to the first vertex before computing the area:
```
area = shoelace([v - first_vertex for v in vertices])
```
This shifts the coordinate system to start near the origin, where floating-point precision is better. The area value is thus accurate even for large absolute coordinates.

When reporting numbers, the tool uses **nine significant digits** (e.g., `1.23456789e9`), not a fixed decimal place. This gives consistent precision across small and large values. Example:
- Tiny area: `1.23456789e-12 m²`
- Large area: `1.23456789e6 m²`

## Registration: alignment without opening drawings

The `register` command transforms (shift, scale, rotation) between two drawings using control points (the same feature visible in both drawings). It does not open or modify any file; it only computes the transformation matrix and residuals.

**Fitting models:**
- `similarity` (default): fits scale, rotation (degrees), and shift from 2 control points. Residuals of additional check points show whether the transform holds elsewhere. A scale close to a simple ratio (1000, 25.4, 100) is flagged: it usually means the drawings have different units, not that one is scaled up.
- `scale-translation`: fixes rotation at zero, fits scale and shift from 2 points
- `translation`: fixes scale at 1.0, fits only shift from 1 point (useful when one drawing is a simple moved copy)

**Mirrored drawings:** if the best fit requires a negative scale (mirrored), the command flags it and does not report a transform, because geometric data from a scan or an image often looks mirrored by chance. Do not use geometry from an unscaled PDF or raster image as control points; use model coordinates instead.

**Per-layer registration:** shifts between groups of layers (e.g., floor plan coordinates on layer A, roof plan on layer B) should be registered separately. Each layer pair gets its own `--pair` and `--check` points. After fitting, you can see whether different parts of the drawing have different transformations, which may indicate inconsistent source data.

Example: measuring control points to align a survey overlay:
```
python scripts/cad.py register \
  --pair "100000,200000:100.0,200.0" \
  --pair "101000,201000:101.0,201.0" \
  --check "100500,200500:100.5,200.5"
```

The result is the scale, rotation and shift that maps the first drawing's points to the second's. With `--apply "150000,250000"`, the command outputs where that point lands in the second drawing's coordinates.

## Text sizes are estimates

Text height in CAD is often stored but not always accurate after editing, especially for MTEXT:

**TEXT and ATTRIB:** height is stored in the `height` attribute and is reliable.

**MTEXT:** height and width are stored in `char_height` and `width` attributes, but after editing with `edit`, these bounds become stale and are not automatically recalculated. The tool measures MTEXT manually:
1. Get plain text: `plain_text = entity.plain_text(split=False)`
2. Wrap to lines using the MTEXT's `width` setting and a system font matching its style
3. Measure each line and find the widest: `bounding_width = max(font.text_width(line) for line in wrapped_lines)`
4. Calculate height with line spacing: `bounding_height = char_height + (num_lines - 1) × char_height × line_spacing_factor`

If the MTEXT wraps to multiple lines, the height grows. The result is an **estimate** with a tolerance of ±10% due to font metrics differences across platforms and rasterization effects.

**SHX fonts:** are shape files, not TrueType or OpenType fonts. When a drawing references a missing SHX font (e.g., "SIMPLEX"), the tool substitutes a system font for measurement. The metrics may differ; treat text-size checks as estimates.

**Tolerance:** text-outside-frame checks use a 10% tolerance: if a text bounding box is more than 10% of its own size outside a frame, it is reported; smaller overhangs are ignored.

## Sheet frame detection and `--frame-layer`

The `qa` command searches for a sheet frame (the outline of the printable area on a layout) to check whether content is clipped or shifted. The frame is found heuristically by looking for the largest closed rectangle that covers at least 60% of the paper.

**Candidates for a frame:**
- A closed LWPOLYLINE or POLYLINE with 4 vertices, no bulges, and axis-aligned geometry (within 0.5 mm tolerance)
- Four LINE entities that form a rectangle (each pair of opposite sides at the same position within 0.5 mm)
- A BLOCK inserted in paper space that contains such geometry (the frame's location is the INSERT handle)

**When no frame is found:**
- If no layout content exists, the result is clean (no frame to find)
- If content exists but no candidate is found, a `FRAME_NOT_FOUND` info finding is issued (not an error; it means the layout has no obvious frame, which may be intentional)

**`--frame-layer NAME` override:**
Pass a layer name to override the heuristic search. Only candidates on that layer are considered.
- If the layer does not exist, the command exits with exit 2 (bad arguments) and lists available layer names.
- If the layer exists but holds no frame-like geometry, a `FRAME_NOT_FOUND` info finding is issued (exit 0), not an error.

Example:
```
python scripts/cad.py qa drawing.dxf --layout "Sheet-A" --frame-layer "0-Frame"
```

Use `dump --type LWPOLYLINE --type POLYLINE` to see which layers hold candidate rectangles.

## Traps and common gotchas

| Trap | Symptom | Prevention |
|---|---|---|
| Unitless drawing ($INSUNITS = 0) | `measure` refuses the command; user asks "what unit is this?" | Use `info <file>` to check `$INSUNITS` before measuring. If zero, ask the user and pass `--assume-unit unit_name` |
| Rotated viewport (twist ≠ 0) | Coordinates in paper space don't line up with model space | `dump --space <layout>` to see viewport center and twist; the rendered plot is rotated but the coordinates on paper are not |
| Block reference at a large scale | Geometry inside the block is tiny in the drawing but draws large | Block coordinates are transformed by insertion scale; measure inside the block definition with `--space block_name` |
| MTEXT without explicit width | Width and height are hard to predict | Edit MTEXT carefully: set an explicit width before changing text content; use `render` to visually verify the result |
| SHX font missing | Text height estimate is off; font substitution occurs silently | `info <file> --conventions` lists missing fonts; supply them or accept the estimate tolerance (±10%) |
| Large survey coordinates (> 1e6) | Area values look wrong or are zero | The tool shifts coordinates relative to the first vertex before computing area; the result is accurate |
| Segments that almost touch (gap 1e-12) | `--join` doesn't close them; gap tolerance too small | Use `measure --type LINE --window <area>` to find the largest gap; set `--gap` to match |
| Plot rotated or scaled to fit | Frame shift check skipped | This is normal (rotation/scale transformation is not simple translation). Verify the plot visually with `render` and a crop. |
| Paper units ≠ model units | Paper-space measure gives mm, model-space gives metres | `measure` applies the correct unit conversion automatically; always use the `--unit` flag to name the output unit |
