# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version number covers the skill
(`SKILL.md` metadata), the Python library (`cadlib`) and the plugin manifest.

## [Unreleased]

## [0.1.0] - 2026-10-03

First public version. Measured baseline and method: [docs/measurements.md](docs/measurements.md).

### Added

- **`plot`:** deliverable PDFs through the user's CAD application (`--allow-com`), one fresh document per layout, explicit page setup (device, media, area, scale, rotation, plot style), verification of every PDF, safe `--dest` copy that never overwrites silently.
- **`edit`:** executes an edit plan in two passes (validate everything first, then apply) on DXF through ezdxf and on DWG through the user's CAD; six operations (`replace-text`, `set-props`, `delete`, `move`, `clone`, `pan-viewport`); idempotent; checks its own result against the original and reports unintended changes; never touches the original.
- **`plot` with layouts on another device:** a layout that uses a DWF plotter or a printer keeps its own plot setup; the PDF device is named only in the plot call. A PDF with almost no drawing content fails with `PLOT_BAD_OUTPUT` instead of being reported as success.
- **`edit` and fields:** fields that CAD re-evaluates on save (for example `FILENAME`) are listed as `field_updates` and no longer make verification fail with exit 7.
- **`register`:** the transform between two drawings (shift, scale, rotation) from control points given as coordinates, with residuals, independent check points, a warning when the fit is exact (it proves nothing), a unit-ratio hint and a mirror warning; exit 7 only with `--tolerance`.
- **`measure`:** lengths and areas of selected entities (LINE, ARC, CIRCLE, ELLIPSE, SPLINE, polylines, hatches) in a chosen unit taken from `$INSUNITS`; hatch area is the outline minus its islands; no total across entity types; unitless drawings are refused unless `--assume-unit` is given.
- **`qa`:** findings with severity (error, warning, info) on a drawing (unset units, layouts holding only viewports, viewports without a usable scale, missing external references) and on a plotted PDF (page count and size, almost no content, required or forbidden text); exit 7 only for errors.
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
