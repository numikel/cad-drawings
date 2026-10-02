# cad-drawings

Inspect, compare, convert and render CAD drawings in DWG and DXF format on Windows, macOS and Linux—without AutoCAD, or with native CAD automation when you agree to it. Works offline, never modifies the source file, and outputs JSON summaries for automation. Editing, plotting and measuring commands are planned.

## What it is

A reusable agent skill (with CLI tools) that knows how to:
- **Read** DWG and DXF drawings using open-source libraries (works everywhere)
- **Find** text, attributes, layer names, block names across all layouts and block definitions
- **Render** layouts to PNG for visual inspection (from COM plots or ezdxf approximation)
- **Compare** two revisions semantically, ignoring save noise
- **Edit** drawings by handle with atomic validation and change logs (planned: F2; today a CAD session library, `cadlib.acad`, is available for custom scripts)
- **Plot** sheets to PDF with explicit page setup (planned: F2)
- **Measure** areas, lengths and coordinates in drawing units (planned: F3; today use `info`, `dump` and ezdxf)
- **Convert** between DWG and DXF, with automatic backend selection
- **Clean up** run directories and caches

All operations run in a fresh isolated directory, never modify the source file, and exit with meaningful codes.

## Requirements

### Minimum (read-only)
- **Python 3.10+**
- **ezdxf 1.4.4+** (read DXF via `pip install ezdxf`)

### For DWG support
Pick one per platform:
- **Windows**: AutoCAD 2020+ (or BricsCAD, ZWCAD) via COM, or install ODA File Converter or LibreDWG
- **macOS / Linux**: ODA File Converter or LibreDWG

### For PNG rendering
- **pypdfium2** (default, included) — fastest, Apache-2.0 / BSD-3
- **PyMuPDF** (optional, AGPL — not recommended) — faster, but copyleft
- **Poppler** (optional, external, GPL) — slowest, if installed

Run `python skills/cad-drawings/scripts/doctor.py` to check what is installed and get install commands.

## Installation

Copy the `skills/cad-drawings` folder into your agent host's skills directory:

```bash
# For Claude Code
cp -r skills/cad-drawings ~/.claude/skills/

# For other hosts, check their skills location
```

The skill requires no installation; `doctor.py` runs with stdlib only.

## Quick start

```bash
# Check what you have (required before first use)
python skills/cad-drawings/scripts/doctor.py

# Inspect a drawing
python skills/cad-drawings/scripts/cad.py info example.dwg

# Find all occurrences of a text pattern
python skills/cad-drawings/scripts/cad.py find example.dwg --pattern "REVISION"

# Render all layouts to PNG
python skills/cad-drawings/scripts/cad.py render example.dwg

# Compare two revisions
python skills/cad-drawings/scripts/cad.py fingerprint v1.dwg --out fp1.json
python skills/cad-drawings/scripts/cad.py fingerprint v2.dwg --out fp2.json
python skills/cad-drawings/scripts/cad.py diff fp1.json fp2.json

# Get help for any command
python skills/cad-drawings/scripts/cad.py --help
python skills/cad-drawings/scripts/cad.py info --help
```

All output goes to a fresh run directory (e.g., `/tmp/cad-drawings-runs/20261002-120530-info-a1b2/`); large results are in files there, and the JSON summary is on stdout.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error (check the error message in the JSON) |
| 2 | Bad arguments (check `--help`) |
| 3 | Missing backend or dependency (run `doctor` to see what to install) |
| 4 | Resource busy (file is locked, document open elsewhere) |
| 5 | Timeout |
| 6 | Precondition not met (target exists, empty layout, confirmation missing) |
| 7 | Partial success (some outputs produced, some failed) |

## Safety model

- **Your files are never modified.** The skill works on copies in isolated run directories.
- **One CAD instance per automation session.** Parallel executions use a lock to prevent conflicts.
- **No interactive prompts.** All parameters are command-line flags or files.
- **UTF-8 output** regardless of platform encoding.
- **Meaningful error codes** so automation can branch on failures.

The run directory is NOT deleted; use `cad.py cleanup` to remove old runs.

## Platform support

| Function | Windows | macOS | Linux |
|---|---|---|---|
| Read DXF | ✓ | ✓ | ✓ |
| Read DWG | COM / ODA / LibreDWG | ODA / LibreDWG | ODA / LibreDWG |
| Render preview | COM / ezdxf | ezdxf | ezdxf |
| Edit DWG native (planned: F2) | COM (AutoCAD/BricsCAD) | — | — |
| Plot native (planned: F2; `render --allow-com` plots previews today) | COM (AutoCAD) | — | — |
| Convert DWG ↔ DXF | COM / ODA / LibreDWG | ODA / LibreDWG | ODA / LibreDWG |

## Dependencies and licenses

All required dependencies are permissive open-source licenses. Optional dependencies are noted.

| Component | License | Use | Notes |
|---|---|---|---|
| ezdxf | MIT | Read/analyze DXF | Always present |
| pypdfium2 | Apache-2.0 / BSD-3 | PDF → PNG rasterization | Default backend |
| Pillow | HPND-like | Direct (image resize/crop) | Permissive |
| numpy, fonttools, pyparsing | BSD / MIT | Transitive (ezdxf, matplotlib) | Permissive |
| pywin32 | PSF / BSD | COM on Windows | Windows only, optional |
| matplotlib | PSF-based | Required (raster backend of the approximate render) | Permissive |
| PyMuPDF | **AGPL-3.0** | Optional PDF → PNG (faster) | Copyleft; imported only with `render --raster pymupdf` |
| Poppler | **GPL** | Optional PDF → PNG (last resort) | GPL; external tool, not bundled |
| LibreDWG | **GPL-3.0+** | DWG ↔ DXF conversion | External tool, fallback, not bundled |
| ODA File Converter | Proprietary freeware | DWG ↔ DXF conversion | Users download directly, not bundled |
| AutoCAD | Proprietary | Native DWG edit/plot (Windows) | User's license required |

## Trademarks

Autodesk, AutoCAD and DWG are registered trademarks or trademarks of Autodesk, Inc. This project is not affiliated with or endorsed by Autodesk.

DXF is a file format and is not a registered trademark.

## Contributing

This skill follows the publication profile in `AGENTS.md`. Before contributing:
1. Check `AGENTS.md` for publication rules (generic, cross-platform, agent-neutral text, permissive licences)
2. Run `python skills/cad-drawings/tests/check_forbidden.py` to ensure no client data or paths are present
3. Add a test before editing the skill (see `AGENTS.md` "No skill edit without a failing test first")
4. Verify on all three platforms (Windows, macOS, Linux) if possible

## License

MIT. See `LICENSE` and `THIRD_PARTY_NOTICES.md` for full terms and dependencies.
