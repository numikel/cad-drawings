# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version number covers the skill
(`SKILL.md` metadata), the Python library (`cadlib`) and the plugin manifest.

## [Unreleased]

## [0.3.0] - 2026-10-06

`qa` checks the sheet frame, the text against the frame and the position of a plotted PDF; `measure` joins loose segments into contours. The checks were run on synthetic drawings and on converted sample drawings with their plots; see [docs/measurements.md](docs/measurements.md).

### Added

- **`qa` sheet frame checks:** the frame is the largest closed rectangle in a layout (polyline, four lines, or inside a block insert) covering at least 60% of the paper; `--frame-layer NAME` overrides the search. New findings: `FRAME_NOT_FOUND`, `FRAME_OUTSIDE_PAPER` (frame beyond the printable area), `FRAME_CHECK_SKIPPED` (the position cannot be derived, with the reason).
- **`qa` text checks:** `TEXT_OUTSIDE_FRAME` (a text crosses the frame edge and its estimated box sticks out by more than 10%; a text entirely outside the frame, such as a title block beside the drawing border, is ignored) and, with `--baseline OLD`, `TEXT_GREW` and `TEXT_WRAPPED` (same handle, grown by more than 10% or wraps to more lines). `BASELINE_MISMATCH` says when the two drawings share too few handles. Sizes are estimates; see `references/geometry-and-units.md`.
- **`qa` PDF checks:** `PDF_CLIPPED` (error: ink at the page edge along at least 5 mm of it, or a stroke from the edge running at least 50 mm into the page; shorter marks give the info finding `PDF_EDGE_MARKS`), `PDF_SHIFTED` (frame more than 2 mm from where the layout puts it, for 1:1 plots without rotation or centring) and `PDF_TRIM_OUTLINE` (a format outline on the page edge is ignored). The page is rasterised, so backgrounds and clip paths do not count as content.
- **`measure --join` and `--gap`:** touching LINE, ARC and open polyline, ellipse and spline segments are joined into closed contours (`type: CONTOUR`, with `members`; summary field `joined`). Only unambiguous loops are joined; branching networks and open chains stay separate with a warning.
- **`references/geometry-and-units.md`:** spaces and coordinate systems, units, printable area and plot origin, measuring, large coordinates, registration, text size estimates, frame detection and a table of traps.

### Changed

- **`qa` on a plotted PDF** can now exit 7 for a PDF that used to pass, because `PDF_CLIPPED` is an error.
- **`qa`** reports at most 50 findings per id in `findings.json`; the rest is counted in `truncated` and in the summary field `not_listed`.
- **`qa`** searches block inserts for the sheet frame only up to 50 000 expanded entities per layout; beyond that it stops and reports `FRAME_CHECK_SKIPPED` instead of running until the timeout.
- `references/visual-qa.md` lists every finding id with its meaning and the action to take.

## [0.2.0] - 2026-10-03

Three commands that work on coordinates and findings. `measure` and `qa` were checked on real drawings from a CAD installation; see [docs/measurements.md](docs/measurements.md).

### Added

- **`qa`:** findings with severity (error, warning, info) on a drawing (unset units, layouts holding only viewports, viewports without a usable scale, missing external references) and on a plotted PDF (page count and size, almost no content, required or forbidden text); exit 7 only for errors.
- **`measure`:** lengths and areas of selected entities (LINE, ARC, CIRCLE, ELLIPSE, SPLINE, polylines, hatches) in a chosen unit taken from `$INSUNITS`; hatch area is the outline minus its islands; no total across entity types; unitless drawings are refused unless `--assume-unit` is given.
- **`register`:** the transform between two drawings (shift, scale, rotation) from control points given as coordinates, with residuals, independent check points, a warning when the fit is exact (it proves nothing), a unit-ratio hint and a mirror warning; exit 7 only with `--tolerance`.

## [0.1.0] - 2026-10-03

First public version. Measured baseline and method: [docs/measurements.md](docs/measurements.md).

### Added

- **`plot`:** deliverable PDFs through the user's CAD application (`--allow-com`), one fresh document per layout, explicit page setup (device, media, area, scale, rotation, plot style), verification of every PDF, safe `--dest` copy that never overwrites silently.
- **`edit`:** executes an edit plan in two passes (validate everything first, then apply) on DXF through ezdxf and on DWG through the user's CAD; six operations (`replace-text`, `set-props`, `delete`, `move`, `clone`, `pan-viewport`); idempotent; checks its own result against the original and reports unintended changes; never touches the original.
- **`plot` with layouts on another device:** a layout that uses a DWF plotter or a printer keeps its own plot setup; the PDF device is named only in the plot call. A PDF with almost no drawing content fails with `PLOT_BAD_OUTPUT` instead of being reported as success.
- **`edit` and fields:** fields that CAD re-evaluates on save (for example `FILENAME`) are listed as `field_updates` and no longer make verification fail with exit 7.
- **References:** `references/plotting.md`, `references/edit-plans.md`.
- **Read-side commands** (`python scripts/cad.py <command>`): `doctor`, `info`, `find`, `dump`, `fingerprint`, `diff`, `render`, `convert`, `cleanup`. Every command prints one short JSON summary (under about 4 KB) validated against `assets/output.schema.json`; large results go to files in a fresh run directory.
- **Reading:** DXF through ezdxf; DWG through a converter (ODA File Converter, LibreDWG) or, only with `--allow-com`, the user's own CAD application on Windows.
- **Search** across model space, every layout, block definitions, attributes, layer and block names, with `visible_in_space` and a geometry-based `prints_on`.
- **Semantic diff** that ignores re-save noise (renumbered handles, anonymous block names, viewport ids, `$HANDSEED`) and never states why something changed.
- **Render** to PNG per layout: ezdxf approximation (flagged `approximate`) by default, CAD plot with `--allow-com`; crop, tiles, pixel cap.
- **Session library** `cadlib.acad` for custom scripts: own CAD instance with a recorded PID, retry classified by error code, kill-on-close Job Object, per-call watchdog, system variables restored, plot to PDF with page-setup fallbacks.
- **Skill documentation:** `SKILL.md`, references (COM automation, DXF analysis, backends and installation, visual QA, drafting standards, CAD conventions template and default).
- **Tests and CI:** pytest suite on synthetic fixtures with ground truth, tests that drive a real CAD application (excluded by default), a scanner for client data and vendor files, a GitHub Actions workflow for three operating systems.

### Safety

- Commands never modify the source file and never start a CAD application on their own.
- Outputs go to a fresh run directory (per-user on POSIX), are written atomically and refuse to overwrite an input.
- A CAD process started by a command cannot outlive it, and only that process is ever terminated.

### Known limitations

- `pan-viewport` is not supported on DWG edits; `clone` into another space is not supported on DWG edits (both work on DXF).
- Some edit operations are covered only by tests with a fake CAD (see `references/edit-plans.md`).
- `find --pattern` limits input length but cannot stop a pathological regular expression.
- Plotting through the CAD application may open the user's default PDF viewer; the result carries a warning.
- Verified on synthetic drawings and one CAD version (AutoCAD 2024 on Windows 11); macOS and Linux are covered by CI only.

### Planned

- `measure`, `register`, `qa`.
- Claude Code plugin manifest and marketplace entry.
