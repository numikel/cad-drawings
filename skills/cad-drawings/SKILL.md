---
name: cad-drawings
description: >-
  Use this skill when the user wants to inspect, compare, render, measure or edit CAD drawings
  in DWG or DXF format, even if they only mention "show me this plan", "what changed between
  these revisions", "update the title blocks", "export the sheets to PDF", or "how big is
  this area". Handles layers, texts, blocks and attributes, layouts and viewports, units and
  underlay alignment, and failing CAD automation (RPC rejections, hung processes). Works on
  Windows, macOS and Linux without AutoCAD, and prefers AutoCAD or a compatible CAD via COM
  on Windows for native DWG access and measurements. For deliverable PDFs or editing, write
  custom code on top of the bundled COM library. Not for Revit, SketchUp, 3D models, or
  PDF or raster image editing.
license: MIT
compatibility: >-
  Python 3.10+ with ezdxf 1.4.4+ and pypdfium2. DWG files require AutoCAD or a compatible CAD
  via COM (Windows, pywin32 312+), ODA File Converter, or LibreDWG. Run scripts/doctor.py first
  to check what is installed and get install options per platform.
metadata:
  version: "2.0.0"
---

## Overview

Read CAD drawings through ezdxf (fast, works everywhere), get precise measurements through COM when available on Windows. Every script call is safe to repeat, works on a copy of the source, and ends with a JSON summary that reports what happened and where the results landed.

All command paths are relative to the skill directory.

## First run: Check what you have

```sh
python scripts/doctor.py
```

This reports your system's capabilities (read DXF, read DWG, render, measure), missing components with install options per platform, and never installs anything by itself. If something is missing, ask the user to install it (the tool lists the exact commands) or choose a fallback backend.

## Talking to the user

Ask the user — through the host's interactive question tool when one is available, otherwise as a plain question, then wait — before:

- Starting a CAD session or connecting to one the user may be using (always own an instance unless they say attach)
- Writing results over existing files or into the user's folders
- Deleting files (show the list first)
- Starting an operation estimated to take more than a few minutes (large DWG conversions, rendering many layouts)
- Choosing a template or convention the project does not document
- Stating the intent behind a detected change (never guess why something was removed)

Put the recommended option first and explain why. Do not ask about anything the scripts can determine themselves.

## Backends and availability

| Task | Preferred | Fallback | If nothing works |
|---|---|---|---|
| Convert DWG to DXF | AutoCAD via COM | ODA File Converter | LibreDWG (with quality warning) |
| Convert DXF to DWG | AutoCAD via COM | ODA File Converter → LibreDWG | Result stays in DXF, explicitly reported |
| Read DXF | ezdxf (always, even with COM) | — | — |
| Read DWG | Convert to DXF first, then ezdxf | — | — |
| Render preview PNG | COM plot per layout (Windows) | ezdxf drawing (approximate, with warning) | Refuse |
| PDF to PNG raster | pypdfium2 | PyMuPDF (AGPL, optional) | Poppler (GPL, external tool, if installed) |

"Available" means the tool is installed and, for CAD hosts, that the user agreed or already uses that host. Detecting without running CAD is fast; the full check (`doctor --probe-com`) starts a CAD session and may claim a licence.

## Available commands

Run `python scripts/cad.py --help` for the full list. Each command can be called with `--help`:

- `doctor` — Check capabilities, missing components, CAD hosts detected
- `info` — Units, extents, layouts, page setups, layers, blocks, XREFs, missing SHX fonts, lock files
- `find` — Search for text, attributes, layer names, block names across all layouts and block definitions
- `dump` — Export entities as JSONL with filters (layout, type, layer, spatial window, handle, count limit)
- `render` — PNG of one layout, with crop and tile support; from COM plot or ezdxf approximation
- `fingerprint` — JSON signature of graphic entities (handles, layer, bbox, text hash, viewport table)
- `diff` — Semantic diff of two fingerprints or drawings, ignoring save noise
- `convert` — DWG ↔ DXF
- `cleanup` — List and delete run directories and cache, with dry-run preview

All commands write to a fresh run directory (never next to the source). Large results go to files; the JSON summary stays under ~4 KB.

## Workflows

### Inspect a drawing

```
python scripts/cad.py info <file>
```

Units, layout names, page sizes, layer count.

```
python scripts/cad.py info <file> --conventions
```

Also infer title-block fields, text styles and heights, naming patterns.

```
python scripts/cad.py find <file> --pattern <regex>
```

Search all text, attributes, layer names across all sheets.

```
python scripts/cad.py dump <file> --space model --limit 100
```

Sample entities in model space.

### Compare two revisions

```
python scripts/cad.py fingerprint <file1> --out fp1.json
python scripts/cad.py fingerprint <file2> --out fp2.json
python scripts/cad.py diff fp1.json fp2.json
```

Semantic diff (or pass drawings directly to `diff` instead of fingerprints); summary shows ~10 first changes, full list in `diff.json`.

For visual comparison: render each file separately and crop areas of interest.

### Render a layout for visual check or short-term sharing

```
python scripts/cad.py render <file> [--layout NAME ...] [--max-px 2000] [--crop X1,Y1,X2,Y2]
```

PNG in a fresh run directory. The agent controls nothing about colors, layers or CTB (plot style); those come from the drawing. If the plot looks wrong, check the page setup and viewport properties in the drawing, not the script.

### Measure areas and lengths

```
python scripts/cad.py info <file>
```

Check `$INSUNITS` to learn the model unit (e.g., 4 = mm, 6 = m).

```
python scripts/cad.py dump <file> --pattern boundary|hatch-name [--window ...]
```

Export the boundary or hatch entity. Parse the result with ezdxf: compute area or length in model units, convert to the requested unit, state which entity the number came from. When the hatch has islands the area of the boundary differs from the hatch area; always report which one and why. Ignore closed shapes that are not filled or dimensioned (they may be guides or orphaned geometry). For closed polylines or splines use `shapely.Polygon` to compute area.

### Editing and plotting (no command yet)

Commands for editing DWG files and producing deliverable PDFs do not ship in this version. With a CAD host on Windows, write a short script on top of the bundled session library instead of driving COM by hand:

```python
import sys
from pathlib import Path

SKILL_DIR = "<path of the folder that contains this SKILL.md>"
sys.path.insert(0, SKILL_DIR + "/scripts")

from cadlib.acad import AcadSession
from cadlib.runs import RunContext, stage_copy

ctx = RunContext.create("custom-plot")  # fresh run directory, never next to the source
working = stage_copy(Path("<drawing>.dwg"), ctx)  # work on a copy, never on the original
with AcadSession.start() as session:  # own CAD instance; quits and cleans up on exit
    warnings = session.plot_layout_pdf(working, "<layout name>", ctx.path("sheet.pdf"))
print(warnings)  # read them: fallbacks and side effects are listed here
```

- `plot_layout_pdf` writes straight to a path that must not exist yet, opens a fresh document per call, and uses a built-in PDF device when the layout has no plotter (it says so in the warnings). The PDF plotter may open the user's default PDF viewer; COM cannot switch that off, so tell the user.
- For edits: find handles with `find`/`dump` first, open the copy with `session.open(path, readonly=False)`, change those handles through the document's COM object, write the result with `session.save_dwg(doc, ctx.path("edited.dwg"))`, then verify with `fingerprint` and `diff`.
- Do not reimplement connection, retry, document lookup, plotting, locking or cleanup; read the `cadlib.acad` docstrings for the full API. Ask the user before writing to their files or over an existing output.

## Project conventions

Before editing or plotting, locate the project's `CAD_CONVENTIONS.md` file (usually next to the drawings). If it exists, read it; it takes precedence over this skill. If it does not exist, run:

```
python scripts/cad.py info <any-sheet> --conventions
```

This lists units, layout sizes, text styles, title-block fields, layer names. Ask the user to confirm or correct each, then save the answers to `CAD_CONVENTIONS.md` using the template in `references/cad-conventions-template.md`, or copy `references/CAD_CONVENTIONS.default.md` into the project and adapt each field.

Conventions capture what the drawings teach: sheet sizes, units, layer naming, title block field meanings, text heights, scales. With them recorded, the agent does not have to infer or ask again.

## Gotchas

- **The CAD session the user works in is not yours.** Do not connect to the user's running AutoCAD unless they explicitly say to attach. Never pick documents via `ActiveDocument`, never close or save documents the scripts did not open, never change `Visible` on an instance you did not start. Work on copies.

- **COM calls fail transiently while CAD is busy** (`-2147418111` "Call was rejected by callee", or a late-bound `AttributeError: <unknown>.X`). Retries are built in, classified by HRESULT. But do not loop over tens of thousands of entities through COM; every property access is a cross-process call. Find targets in ezdxf (instant), edit them by handle through COM.

- **Old outputs look like new ones.** `PlotToFile` does not overwrite an existing file. A stale PDF, PNG or DXF from an earlier run passes an "exists" check. All scripts write to a fresh run directory; artifacts are never reused if they are older than their source.

- **Text lives in more places than model space.** Paper space, block definitions, attributes, frozen or off layers, XREFs, unused blocks. Use `find` to search everywhere.

- **MTEXT carries formatting codes** (`\P`, `{\f…;…}`, `^I` for tab). Match on plain text, replace in the raw string, assert the result.

- **Units and orientation differ between drawings.** Always check `$INSUNITS`, viewport twist and north-arrow orientation before combining or measuring.

- **Saving changes things nobody edited** (anonymous block names, paper-space IDs, viewport IDs, handle seed). Use the semantic `diff`; never state that something was removed without confirming with the user and evidence.

- **One automation client per CAD instance.** Parallel clients fail with "invalid execution context". The scripts hold a lock internally; parallel read-only work uses the DXF path (no lock needed).

- **COM-exported DXF may report viewport status 0.** The scripts activate every layout before export and warn when any viewport has status ≤ 0 in the written file. The `find` command reports `prints_on` (which layouts print the object) based on geometry—it is not a plot preview, so say "the geometry lands inside the viewport window" when answering "will it print".

- **After every CAD run check that no CAD process of yours is left.** The library quits and escalates on its own PID; never kill by image name (`taskkill /IM acad.exe`). If the library's quit hangs, the watchdog terminates its own PID; that works only when the library knows which PID it started.

- **Plotting to PDF may start the user's PDF viewer** unless the library disabled that (it is an option of the plotter configuration, and COM cannot switch it off). Tell the user if a PDF opens unexpectedly.

- **Never install tools or packages without asking the user first**, even when a task seems to need them. The `doctor` command lists options; the user decides.

- **DXF files are 5–8× larger than DWG.** Do not keep DXF copies around. Use `fingerprint` (small JSON) and `cleanup` to remove old runs.

- **Do not name helper scripts after standard-library modules.** `inspect.py` and other stdlib names break imports.

## Output and long jobs

Every command prints a JSON summary on stdout:

```json
{
  "status": "ok",
  "command": "info",
  "exit_code": 0,
  "summary": { "units": "mm", "layouts": 3, "layers": 24 },
  "outputs": { "info": { "path": "/tmp/cad-drawings-runs/.../info.json" } },
  "run_dir": "/tmp/cad-drawings-runs/..."
}
```

The JSON is always compact (under ~4 KB). Large data (full entity lists, diffs, logs) goes into files in the run directory. **Do not pipe script output through `tail`, `head` or `grep`; read the summary and open the files it points to.**

For jobs that take more than a few seconds (large DWG conversions, rendering), the script writes progress to `status.json` in the run directory. You can check status while the job runs in the background. The run directory is never deleted by the script; use `cleanup` to remove old runs.

Exit codes: 0 = success, 1 = error, 2 = bad arguments, 3 = missing dependency or backend, 4 = resource busy (file locked, document open elsewhere), 5 = timeout, 7 = partial success.

## When to read which reference file

| Read this… | When you… |
|---|---|
| `references/com-automation.md` | Write custom code using the COM library (HRESULT table, late-binding traps, `HandleToObject`, retry patterns, document lifecycle) |
| `references/dxf-analysis.md` | Write ezdxf code (entity queries, MTEXT, plain vs raw text, block/space mapping, handle limits) |
| `references/backends-and-install.md` | `doctor` reports missing components and you need per-OS install commands or license notes |
| `references/visual-qa.md` | Verify a rendered or printed sheet (crop assumptions, DPI vs text height, QA checklist) |
| `references/drafting-standards.md` | Add, edit or delete drawing content without a project `CAD_CONVENTIONS.md` (sheet sizes, title blocks, layers, text heights, units) |
| `references/cad-conventions-template.md` | Interview the user and create a project's `CAD_CONVENTIONS.md` |
| `references/CAD_CONVENTIONS.default.md` | Use the standard default conventions (copy into project and adapt) |
