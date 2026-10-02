# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (F1 features)

- **Core infrastructure**
  - `doctor` command: detect platform, Python version, installed packages, converters, CAD hosts, free disk space, with per-OS install suggestions
  - `runs` module: isolated run directories with manifest, cache (by SHA1), lock file, JSON status heartbeat
  - `result.py` and `command.py` contracts: exit codes, error classification, JSON output schema (< 4 KB cap)
  - UTF-8 output enforced across all platforms

- **Read commands**
  - `info`: units, extents, layouts with page setup and viewports (center, size, scale, twist, frozen layers, status), layers, blocks, XREFs (attach/overlay/unresolved/bound with `$0$` prefixes), missing SHX fonts, `.dwl` lock files; optional `--conventions` to infer text styles and title block
  - `find`: regex search for TEXT, MTEXT (plain and raw), ATTRIB/ATTDEF, layer names, block names across all spaces and block definitions; filter by visibility (`prints_on` = layouts whose active viewport contains the object and does not freeze its layer); multiple files in one call
  - `dump`: export entities as JSONL with filters (space, layout, type, layer, spatial window, handle, limit); full report to file

- **Compare and analyze**
  - `fingerprint`: JSON signature of graphic entities (handle, type, layout/block, layer, bbox, text hash, insertion point) + viewport table; removes save noise
  - `diff`: semantic diff of two fingerprints ignoring anonymous block names, paper-space IDs, viewport IDs, `$HANDSEED`, dictionaries; reports added/removed/changed/moved with old/new values; "unmatched" as confirmation targets

- **Render**
  - `render`: PNG per layout from fresh run directory (never reuse old artifacts)
    - COM backend (Windows): plot each layout from freshly opened document via `AcadSession`, convert PDF to PNG with pypdfium2
    - ezdxf backend (fallback): `drawing` addon with seven workarounds (viewport normalisation, explicit CTB, SHX/support dirs, 1:1 scale, layer exclusion, PyMuPDF detection, XREF embedding)
    - Crop and tile support with full-resolution separate PNGs
    - `--max-px` cap on longest side

- **Convert**
  - `convert`: DWG ↔ DXF with automatic backend selection (COM → ODA → LibreDWG), with quality warnings for degraded paths
  - Input validation and output existence check (success = file exists, newer than source, opens with `ezdxf.readfile(..., recover=True)`)
  - Caching by `sha1(source) + converter id`

- **Cleanup**
  - `cleanup`: list run directories and sizes, delete only files created by the skill (tracked in manifest), file-by-file (no recursive helpers), `--list` / `--older-than` / `--run` / `--cache` / `--orphans` (reports `.dwl` files owned by dead processes), `--dry-run`, `--yes` confirmation

- **COM automation (Windows)**
  - `AcadSession`: own instance via `DispatchEx` with PID detection, connect to user instance with guard (read-mostly, no SaveAs, no close-without-consent)
  - `retry()` with exponential backoff and HRESULT classification: transient (`-2147418111`, `-2147417846`, late-binding `AttributeError`) vs permanent (raise immediately)
  - System variables context manager: `BACKGROUNDPLOT=0`, `ISAVEBAK=0`, `ISAVEPERCENT=0` (never `FILEDIA`)
  - Lock file per instance (PID + command + start time); dead-owner detection and takeover
  - Viewport status reliability: activate layout before `SaveAs` DXF; validate with `viewport_report`
  - Document registry: `Close(False)` only on documents the session opened

- **DXF analysis (ezdxf)**
  - Load DWG through automatic conversion and caching
  - Query: paper space per layout, blocks and attributes, XREF attachment type, missing files
  - Plain text extraction from MTEXT (ezdxf's `plain_text` function)
  - Handle and `$HANDSEED` management
  - Recovery mode for corrupted files

- **Documentation**
  - `SKILL.md` (< 500 lines): overview, first run, user interaction, backends table, available commands, workflows (checklists), project conventions, gotchas, output/long jobs, when to read references
  - `references/`: com-automation.md (HRESULT table, late-binding, `DispatchEx` vs `Dispatch`, Quit cleanup, `HandleToObject`, sysvars), dxf-analysis.md (entity queries, MTEXT, block/space mapping, handles), backends-and-install.md (per-OS install commands, license notes), visual-qa.md (crop/tile assumptions, DPI vs printed height, QA checklist), drafting-standards.md (ISO sheet sizes, scales, lines, text, dimensions, layers, revisions, based on ISO 128/129/5455/7200 and ASME Y14), cad-conventions-template.md (project-specific CAD rules, interview workflow), CAD_CONVENTIONS.default.md (pre-filled standard default)
  - `README.md`: what it is, requirements per platform, quick start, exit codes, safety model, license table, trademarks
  - `THIRD_PARTY_NOTICES.md`: components and their licenses (permissive stack + optional AGPL + GPL external tools)

- **Project infrastructure**
  - `AGENTS.md`: publication profile (generic, cross-platform, agent-neutral text, permissive licences, no client data, no vendor files)
  - Synthetic fixtures (DXF): models in cm and mm with unit offset, two layouts with viewports (twist 90°, frozen layer), MTEXT with codes, blocks with attributes, XREF, title block, hatch, large variant for performance
  - Ground-truth files: `truth.json` (visibility and `prints_on` per entity), `changes.json` (semantic diff reference)
  - CI: `check_forbidden.py` (scans for client names, Polish project jargon, user paths; allow-lists AGENTS.md)

### Planned (F2–F4, not in F1)

- `plot`: PDF with explicit page setup (device, media, scale, rotation), deterministic output, full validation
- `edit`: execute edit plan with precondition assertions, dry-run preview, change log
- `measure`: areas, lengths, contours, corner registration
- `register`: multi-point alignment between drawings with residual validation
- `qa`: margin checks, legible lineweights, symbol completeness
- Plugin manifest for Claude Code

---

## Notes

- **Baseline (RED F0)**: Measured on synthetic fixtures; old-skill runs left 4 of 4 orphan `acad.exe` instances, produced 1 of 5 correct complete answers; no-skill runs answered correctly but spent ~200 throwaway ad-hoc CAD scripts. F1 removes all P0 session-hygiene issues and bundles reusable logic.
- **Version 2.0.0**: Breaking change from old `dwg` skill (read-only pipeline); new skill has editor, plotter, and native DWG edit on Windows.
