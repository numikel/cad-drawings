# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version number covers the skill
(`SKILL.md` metadata), the Python library (`cadlib`) and the plugin manifest.

## [Unreleased]

## [0.4.0] - 2026-10-29

Fixes from a field report: an agent drew new geometry on a DWG with its own raw COM script, because `doctor` reported working packages as missing and the skill had no documented way to add content to a DWG. The scenario is now an eval (id 7); the before and after runs are in [docs/measurements.md](docs/measurements.md).

### Added

- **`conventions`:** samples DXF/DWG files or a folder (newest first, `--max-files 20`, `--recursive`) and aggregates units, paper sizes, text styles and heights, layer names and prefixes, block prefixes, title-block blocks and layout name patterns, each with `count` and `of` for the `Source` column of `CAD_CONVENTIONS.md`; layouts without a paper size are counted separately (`layouts_without_paper_size`). With `--allow-com` one CAD instance serves every DWG of the run, started only when a DWG needs converting. A file that cannot be read or converted is skipped with a warning (exit 7). `info` is unchanged.
- **Drawing helpers in `cadlib.acad`:** `point_array`, `ensure_layer` (creates a missing layer, never changes an existing one) and `load_linetype`, which falls back to language-neutral ISO linetypes (for example `DASHED` to `ACAD_ISO02W100`) and finally to `Continuous`, always with a warning. A project can map its own linetypes in `CAD_CONVENTIONS.md` (new §7 key).
- **Recipe "add geometry to a DWG"** in `references/com-automation.md`, built only on the public session library; `SKILL.md` routes such tasks there and rules out raw `win32com` start-up code and private helpers.
- **`doctor`:** `summary.skill_version` and `summary.skill_dir`; an optional `upgrade` field on capabilities that work with an older package.
- **`plot`:** error code `DEST_LOCKED` (exit 4) when a `--dest` PDF is held by another program, typically the PDF viewer on Windows. The PDFs stay in the run directory and are listed in `outputs`.
- CI job `min-deps` runs the test suite with the oldest supported package versions (`tests/min-deps.txt`).

### Changed

- **`doctor` version checks:** a package below the recommended version but at or above the required one now counts as working. Required: ezdxf 1.4.0, pywin32 307; recommended: ezdxf 1.4.4, pywin32 312. Before, ezdxf 1.4.3 turned DWG reading, conversion and rendering into `missing`, and pywin32 311 hid COM and proposed installing a CAD application that was already there.
- **`plot` with a locked `--dest`** ends with `DEST_LOCKED` (exit 4) instead of the catch-all `UNEXPECTED` (exit 1), which was never a documented result. On Linux and macOS a permission error stays a plain error.
- **COM retries** also treat late-bound `AttributeError: Open.X`, `AttributeError: <ProgID>.X` and the setter form `Property '<name>.X' can not be set.` as transient, not only `<unknown>.X`; such errors failed CAD sessions at random. Because a misspelt member name gives the same message, a named proxy is retried for about 6 seconds and then raised as the original `AttributeError`.
- `references/com-automation.md`: a measured table of `PlotToFile` with and without the device name and with `RefreshPlotDeviceInfo` (the refresh, not the device name, resets the page setup); examples use the session's document instead of `ActiveDocument`.
- `references/drafting-standards.md`: text masks must not hide the geometry the sheet exists to show.

### Known limitations

- `conventions` does not collect title-block field labels; it lists the blocks repeated on layouts.

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
