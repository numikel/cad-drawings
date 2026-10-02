# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). One version number covers the skill
(`SKILL.md` metadata), the Python library (`cadlib`) and the plugin manifest.

## [Unreleased]

First public version (0.1.0 once released). Measured baseline and method: [docs/measurements.md](docs/measurements.md).

### Added

- **Commands** (`python scripts/cad.py <command>`): `doctor`, `info`, `find`, `dump`, `fingerprint`, `diff`, `render`, `convert`, `cleanup`. Every command prints one short JSON summary (under about 4 KB) validated against `assets/output.schema.json`; large results go to files in a fresh run directory.
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

- No `plot` or `edit` command yet; the session library can be used from custom scripts.
- `find --pattern` limits input length but cannot stop a pathological regular expression.
- Plotting through the CAD application may open the user's default PDF viewer; the result carries a warning.
- Verified on synthetic drawings and one CAD version (AutoCAD 2024 on Windows 11); macOS and Linux are covered by CI only.

### Planned

- `plot` and `edit` (next), then `measure`, `register`, `qa`.
- Claude Code plugin manifest and marketplace entry.
