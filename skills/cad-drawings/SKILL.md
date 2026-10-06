---
name: cad-drawings
description: >-
  Use this skill when the user wants to inspect, compare, render, measure, edit, or plot CAD drawings
  in DWG or DXF format, even if they only mention "show me this plan", "what changed between
  these revisions", "update the title blocks", "export the sheets to PDF", "I need PDFs", or "how big is
  this area". Handles layers, texts, blocks and attributes, layouts and viewports, units and
  underlay alignment, and failing CAD automation (RPC rejections, hung processes). Edits DXF directly and DWG
  through the CAD application. Works on Windows, macOS and Linux without AutoCAD; uses AutoCAD or
  a compatible CAD via COM on Windows only when the user explicitly agrees. Not for Revit, SketchUp,
  3D models, or PDF or raster image editing.
license: MIT
compatibility: >-
  Python 3.10+ with ezdxf 1.4.4+ and pypdfium2. DWG files require AutoCAD or a compatible CAD
  via COM (Windows, pywin32 312+), ODA File Converter, or LibreDWG. Run scripts/doctor.py first
  to check what is installed and get install options per platform.
metadata:
  version: "0.3.0-rc.1"
---

## Overview

Read CAD drawings through ezdxf (fast, works everywhere); use a CAD host through COM on Windows only when the user agrees. Commands never modify the source file: results go to a fresh run directory, and every command ends with a JSON summary that reports what happened and where the results landed.

All command paths are relative to the skill directory. Use `python3` where `python` is not available (Linux, macOS).

## First run: Check what you have

```sh
python scripts/doctor.py
```

This reports your system's capabilities (read DXF, read DWG, convert, render, PDF to PNG), missing components with install options per platform, and never installs anything by itself. If something is missing, ask the user to install it (the tool lists the exact commands) or choose a fallback backend.

## Talking to the user

Ask the user — through the host's interactive question tool when one is available, otherwise as a plain question, then wait — before:

- Starting a CAD session or connecting to one the user may be using (always own an instance unless they say attach). Commands never start CAD on their own: a DWG is converted and rendered without CAD when ODA or LibreDWG is installed; otherwise the command exits with 3 (`NO_BACKEND`), you ask, and only then rerun it with `--allow-com`
- Writing results over existing files or into the user's folders
- Deleting files (show the list first)
- Starting an operation estimated to take more than a few minutes (large DWG conversions, rendering many layouts)
- Choosing a template or convention the project does not document
- Stating the intent behind a detected change (never guess why something was removed)

Put the recommended option first and explain why. Do not ask about anything the scripts can determine themselves.

## Backends and availability

| Task | Default (no consent needed) | With the user's agreement | If nothing works |
|---|---|---|---|
| Convert DWG to DXF | ODA File Converter, then LibreDWG (approximate, flagged) | AutoCAD or compatible CAD via COM (`--allow-com`, best fidelity) | Exit 3 with install options |
| Convert DXF to DWG | ODA File Converter, then LibreDWG (r2004 only) | COM (`--allow-com`) | Exit 3 with install options |
| Read DXF | ezdxf | — | — |
| Read DWG | Convert to DXF first (row above), then ezdxf | same | Exit 3 |
| Render preview PNG | ezdxf drawing (approximate, flagged) | COM plot per layout (`--allow-com`; may open the user's PDF viewer) | Exit 3 if a required package is missing |
| PDF to PNG raster | pypdfium2 | PyMuPDF (AGPL, optional, `--raster pymupdf`) | — |
| Edit a DXF | ezdxf (DXF only) | — | — |
| Edit a DWG | — | COM (`--allow-com`) | Exit 3 if no CAD |
| Plot sheets to PDF | — | COM (`--allow-com`; deliverable, not preview) | Exit 3 if no CAD |

"Available" means the tool is installed and, for CAD hosts, that the user agreed or already uses that host. Detecting without running CAD is fast; the full check (`doctor --probe-com`) starts a CAD session and may claim a licence.

## Available commands

Run `python scripts/cad.py --help` for the full list. Each command can be called with `--help`:

- `doctor` — Check capabilities, missing components, CAD hosts detected
- `info` — Units, extents, layouts, page setups, layers, blocks, XREFs, missing SHX fonts, lock files
- `find` — Search for text, attributes, layer names, block names across all layouts and block definitions
- `dump` — Export entities as JSONL with filters (layout, type, layer, spatial window, handle, count limit)
- `render` — PNG per paper layout (default: those with content), with crop and tile support; ezdxf approximation by default, COM plot with `--allow-com`; `--convert-with` picks the DWG converter
- `fingerprint` — JSON signature of graphic entities (handles, layer, bbox, text hash, viewport table)
- `diff` — Semantic diff of two fingerprints or drawings, ignoring save noise
- `convert` — DWG ↔ DXF (`--to dxf|dwg`, `--out`, `--overwrite`, `--dry-run`)
- `edit` — Apply a plan of edits to a DXF or DWG (two-pass validation, verified against the original)
- `plot` — Deliverable PDFs per layout, plotted by the CAD application (`--allow-com` required)
- `measure` — Lengths and areas of selected entities in a chosen unit, from `$INSUNITS`; hatch minus islands; no total across entity types
- `register` — Transform (shift, scale, rotation) between two drawings from control points, with residuals and independent check points; does not open any drawing
- `qa` — Mechanical checks with severity on a drawing (units, empty layouts, viewport scale, missing xrefs, sheet frame inside the printable area, text sticking out of the frame; `--frame-layer` names the frame layer, `--baseline OLD` reports texts that grew or wrapped since an earlier version) and on a plotted PDF (page count and size, empty content, required or forbidden text, content clipped at the page edge, plot shifted from where the layout puts the frame); exit 7 only for errors
- `cleanup` — List and delete run directories and cache, with dry-run preview

All commands write to a fresh run directory (never next to the source). Large results go to files; the JSON summary stays under ~4 KB. Commands that can read a DWG accept `--allow-com` (the user agreed to start their CAD application) and `--backend auto|com|oda|libredwg`; `render` uses `--backend auto|com|ezdxf` for the render engine. Failed commands still report `run_dir` and `log`.

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

Narrow the selection first (`dump` shows what is on a layer), then measure one entity type:

```
python scripts/cad.py measure <file> --layer <layer> --type HATCH --unit m
```

- Units come from `$INSUNITS`. A drawing without a unit is refused (`NO_UNITS`): ask the user which unit it is, then pass `--assume-unit mm|cm|m|in|ft`. The result says that the unit was assumed.
- Results are in the unit asked for (`--unit`, default `m`; areas in its square). Lengths and areas of arcs, polyline bulges, splines and ellipses are measured, not taken from control points. Closed shapes also have an area.
- A hatch is its outline minus its islands; its length is the boundary including the islands. A note on the entity says so.
- There is a total only when one entity type matched. A hatch and the outline it fills are the same area, so with several types the result lists them separately and warns instead of adding them. Report which entity the number comes from.
- The default space is `model`. A layout (`--space <layout>`) is measured in its paper units (millimetres or inches), not in the model's.
- Block references, text, dimensions and other types are counted under `skipped`, not measured. Measure inside the block definition (`--space <block>`) when you need it.
- `--join` merges touching LINE, ARC and open polyline, ellipse and spline segments into closed contours (record `type: CONTOUR`, with `members`; the summary has `joined`). Only unambiguous loops are joined: a branching network or an open chain stays as separate segments and the result warns. `--gap DIST` (drawing units, needs `--join`) sets the largest gap to close; the default is tiny, so loose ends that visibly miss each other are reported, not closed. See `references/geometry-and-units.md`.
- Per-entity values are in `measurements.json`; the summary has the unit, count, per-type and per-layer totals.
- Never read dimensions off a rendered PNG.

### Align two drawings

When drawings from different sources must be combined or compared (an underlay, a survey, a scan of the same plan), find the transform from control points that appear in both. Read the coordinates of the same feature in each drawing with `find` or `dump`, then:

```
python scripts/cad.py register --pair "x,y:X,Y" --pair "x,y:X,Y" --pair "x,y:X,Y" --check "x,y:X,Y" [--model similarity|scale-translation|translation] [--tolerance 5] [--apply "x,y"]
```

- Each `--pair` is a point in drawing A and the same point in drawing B. A similarity (default) needs two points and fits scale, rotation and shift; `scale-translation` fixes the rotation at zero; `translation` needs one point and keeps the scale at 1.
- With exactly the minimum number of points the fit is exact: the residuals are zero and prove nothing. Give at least one more point with `--check`; it is not used in the fit and shows whether the transform holds elsewhere. Choose it far from the others.
- The result has the scale, rotation (degrees), shift, the matrix, and the residual of every control and check point, in drawing B units. With `--tolerance` a larger residual ends the run with exit 7.
- A scale close to a unit ratio (1000, 25.4, ...) is flagged: it usually means the drawings have different units, not that one is drawn at a different scale. Check `$INSUNITS` before accepting it.
- A mirrored drawing is flagged but not fitted. Do not take geometry from an unscaled PDF or an image; use control points in model coordinates.
- `--apply "x,y"` maps further points of drawing A into drawing B. The command does not open or change any drawing.

### Edit a drawing

Get the handles of the entities to change using `find` or `dump`:

```sh
python scripts/cad.py find <file> --pattern "old text"
python scripts/cad.py dump <file> --space model --type TEXT --limit 20
```

Every `find` and `dump` hit carries an `expect` object (type, layer, space, current text...). Paste it unchanged into the plan next to the handle: the executor refuses to touch an entity that no longer matches it. Build the plan as JSON conforming to `assets/edit-spec.schema.json` (it also records the SHA-1 of the file the handles came from); see `references/edit-plans.md` for the format and a worked example.

Check the plan:

```sh
python scripts/cad.py edit --spec edits.json --dry-run
```

Apply to a DXF:

```sh
python scripts/cad.py edit --spec edits.json
```

Apply to a DWG (needs consent):

```sh
python scripts/cad.py edit --spec edits.json --allow-com
```

The edited file lands in the run directory (with an `_edited` suffix); the original is never written. The command checks its own work: it compares the edited file with the original and exits with 7 and `UNINTENDED_CHANGE` if anything changed that the plan did not ask for, so read `verified` and `unintended` in the summary rather than assuming success. A plan that was already applied reports `already_applied` instead of failing. After editing texts, run `qa --baseline <file before the edit>` to catch text that grew or wrapped to more lines. Confirm visually with `render` when the change affects how a sheet looks. Ask the user before copying the result over an original (`--out ... --overwrite`). Read `references/edit-plans.md` for handle persistence and idempotence: handles belong to one version of one file, so build a new plan from fresh `find` output after every save.

### Plot sheets for delivery

Ask the user for consent first: a CAD application will start, and the PDF viewer may open.

Get the available paper layouts and page setups:

```sh
python scripts/cad.py info <file> --conventions
```

Build a command with desired options (device, media, scale, rotation, area):

```sh
python scripts/cad.py plot <file> --allow-com --layout "Sheet-A" --scale fit --dest ./pdfs
```

The command plots one layout per PDF and optionally copies them to a destination. See `references/plotting.md` for page-setup options and the exact plotter behaviour.

For custom edits or plots beyond these commands, for more control, read `references/com-automation.md` and write a short script on top of the bundled session library.

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

- **COM-exported DXF may report viewport status 0.** The scripts activate every layout before export and warn when any viewport has status ≤ 0 in the written file. The `find` command reports `prints_on` (which layouts print the object) from geometry only: it is not a plot preview, so say "the geometry lands inside the viewport window" when answering "will it print". It is `null` (unknown) for a layout whose viewport status is 0 or negative. Viewport centres in DXF are stored in display coordinates, which a twisted viewport rotates; the scripts handle this.

- **After every CAD run check that no CAD process of yours is left.** The library quits and escalates on its own PID; never kill by image name (`taskkill /IM acad.exe`). If the library's quit hangs, the watchdog terminates its own PID; that works only when the library knows which PID it started.

- **Plotting to PDF may start the user's PDF viewer** unless the library disabled that (it is an option of the plotter configuration, and COM cannot switch it off). Tell the user if a PDF opens unexpectedly.

- **Never install tools or packages without asking the user first**, even when a task seems to need them. The `doctor` command lists options; the user decides.

- **DXF files are 5–8× larger than DWG.** Do not keep DXF copies around. Use `fingerprint` (small JSON) and `cleanup` to remove old runs.

- **Do not name helper scripts after standard-library modules.** `inspect.py` and other stdlib names break imports.

- **Handles are valid only for the exact file version they were read from.** After an edit (or any CAD save), handles change: a plan made from file v1 will not work on the edited file. Rebuild the plan from a fresh `find` or `dump` after each edit, or edit multiple entities in one run using the same handles.

- **Edit plans are idempotent.** Running the same plan twice on the edited file reports every edit as already applied (second time around). The only exception is `clone`, which adds another copy.

- **Never edit the original file.** The `edit` command works on a copy in the run directory. The output goes to a new file (`<stem>_edited.dxf` or `.dwg`) or a user-specified path. Verify the result before using it.

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

Commands that can reach CAD stop their own work after `--timeout` seconds (default 100, below typical shell-command limits). For longer jobs (large DWG conversions, many layouts), start the command in the background if the host allows it, then poll `status.json` in the run directory (`state` is `running`, `done` or `failed`) and read the JSON summary when it finishes; otherwise split the work per layout or file. Killing a running command can leave a CAD process behind: the library ties its CAD process to the command, but check `python scripts/doctor.py` (field `orphans`) afterwards. The run directory is never deleted by the commands; use `cleanup` to remove old runs.

Exit codes: 0 = success, 1 = error, 2 = bad arguments, 3 = missing dependency or backend, 4 = resource busy (file locked, document open elsewhere), 5 = timeout, 6 = precondition not met (target already exists, empty layout, confirmation missing), 7 = partial success. A missing input file is exit 2.

## When to read which reference file

| Read this… | When you… |
|---|---|
| `references/plotting.md` | Plot with `plot` (device, media, area, scale, rotation, page setup fallbacks) |
| `references/edit-plans.md` | Edit with `edit` (plan format, handle persistence, operations, verification, idempotence) |
| `references/com-automation.md` | Write custom code using the COM library or edit through the CAD application (HRESULT table, late-binding traps, `HandleToObject`, retry patterns, document lifecycle, editing traps) |
| `references/dxf-analysis.md` | Write ezdxf code or understand `edit` on a DXF (entity queries, MTEXT, plain vs raw text, block/space mapping, handle limits) |
| `references/backends-and-install.md` | `doctor` reports missing components and you need per-OS install commands or license notes |
| `references/geometry-and-units.md` | Measure, align or check geometry (spaces and coordinate systems, units, printable area and plot origin, `measure --join` and `--gap`, large coordinates, `register`, text size estimates, sheet frame detection) |
| `references/visual-qa.md` | Verify a rendered or printed sheet (crop assumptions, DPI vs text height, QA checklist) |
| `references/drafting-standards.md` | Add, edit or delete drawing content without a project `CAD_CONVENTIONS.md` (sheet sizes, title blocks, layers, text heights, units) |
| `references/cad-conventions-template.md` | Interview the user and create a project's `CAD_CONVENTIONS.md` |
| `references/CAD_CONVENTIONS.default.md` | Use the standard default conventions (copy into project and adapt) |
