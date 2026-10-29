# COM automation: connecting to and controlling AutoCAD

Read this when you are writing custom COM code or a COM operation returns an error (HRESULT code, AttributeError, timeout, or hung process). This file documents the most common issues and how the bundled retry and session management handle them.

## Recipe: add geometry to a DWG

The `edit` command changes existing entities. To ADD new geometry to a DWG (a route, a marker, a label), write a short script on top of `cadlib.acad`, work on a staged copy, and save under a new name. Do this only after the user agreed to use a CAD application and no CAD work of theirs is in progress.

1. Put the script in a scratch or run directory (not in the skill directory). Its file name must not shadow a standard-library module (`inspect.py`, `types.py`, `select.py`, `code.py` break imports elsewhere).
2. Put the skill's `scripts` directory on `sys.path` by its absolute path (`doctor` reports the skill directory as `summary.skill_dir`), then import `cadlib`.
3. `RunContext.create` gives a fresh run directory; `stage_copy` copies the original into it, so the original is never opened for writing.

```python
import sys
from pathlib import Path

SKILL_DIR = Path("<absolute path of the skill directory>")
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from cadlib.acad import AcadSession, ensure_layer, point_array, retry_expr
from cadlib.runs import RunContext, stage_copy

original = Path("<path of the drawing>.dwg")
ctx = RunContext.create("add-geometry")  # fresh run directory with a log and a manifest
copy = stage_copy(original, ctx)  # work here; the original stays untouched
dst = ctx.path("<name of the new drawing>.dwg")  # a new name inside the run directory
warnings: list[str] = []  # every substitution below ends up here; report them to the user

with AcadSession.start() as session:  # own CAD process; quit when the block ends, even on error
    doc = session.open(copy, readonly=False)
    # Layer: created when missing, never modified when it exists. The linetype goes through
    # load_linetype, so a localized CAD that does not know "DASHED" gets a substitute plus a warning.
    warnings += ensure_layer(doc, "<layer>", color=1, linetype="DASHED")
    space = retry_expr(lambda: doc.raw.ModelSpace)  # drawing units: check them with `info` first
    route = retry_expr(
        lambda: space.AddLightWeightPolyline(point_array([(0, 0), (5000, 0), (5000, 3000)]))
    )
    retry_expr(lambda: setattr(route, "Layer", "<layer>"))
    label = retry_expr(
        lambda: space.AddMText(point_array([(5100, 3000, 0)], dims=3), 2000.0, "<text>")
    )
    retry_expr(lambda: setattr(label, "Layer", "<layer>"))
    session.save_dwg(doc, dst)  # saves under the new name and closes the document

warnings += session.warnings  # also what happened while the session quit
ctx.finish()
print("\n".join(warnings))
```

Then, outside the `with` block, plot the result:

`python scripts/cad.py plot <dst> --layout <name> --allow-com`

Leave the `with` block BEFORE plotting. The COM lock is machine-wide and is held until the session quits, so `plot` started inside the block (or from another process while the block runs) fails with `LOCKED`.

Rules for the recipe:

- Use `load_linetype(doc, name, mapping=...)` directly when you need a linetype for an entity rather than a layer; pass the user's linetype mapping from `CAD_CONVENTIONS.md` as `mapping` (requested name to names to try). It never raises because a linetype is unavailable: the last resort is `Continuous` with a warning. Pass every warning on to the user.
- `point_array(points, dims=2)` is for `AddLightWeightPolyline`; `dims=3` for `AddLine`, `AddPolyline`, `AddMText` and other methods that take 3D points. Never build the array by hand.
- `save_dwg` closes the document; do not touch `doc` afterwards. Open the saved file again with `session.open(dst)` if you need to inspect it.
- Wrap every COM call in `retry_expr(lambda: ...)`: entity creation, collection reads, and property assignments through `setattr`. A transient rejection means CAD did not run the call, so repeating it is safe. A misspelt member name looks like a busy proxy (`AttributeError: ModelSpace.AddLinee`); it is retried for a few seconds and then raised as the `AttributeError` it is.
- Never use `ActiveDocument`, `taskkill /IM`, or a second CAD client in parallel.
- Check the result by plotting or rendering it and looking at the image; a successful save does not prove the drawing looks right.

Pitfalls when adding geometry:

- **Localized linetype names.** On a localized CAD, `Linetypes.Load("DASHED", "acadiso.lin")` can fail while the language-neutral `ACAD_ISO02W100` loads. Do not call `Load` yourself: `load_linetype` and `ensure_layer` try both `.lin` files, then the user's mapping, then the ISO names (`LINETYPE_FALLBACKS`), and finally `Continuous`, and tell you which substitution they made. Ask the user to confirm a substitution before you deliver, and record the agreed mapping in `CAD_CONVENTIONS.md`.
- **MText background mask.** Turning on `BackgroundFill` for an MText label hides the linework behind it, including the route the label explains. Prefer no mask; place labels beside the line instead.
- **Existing layers are left alone.** `ensure_layer` does not change the colour or linetype of a layer that already exists (that would change the drawing beyond what was asked); it warns when they differ. Draw on the existing layer or ask the user.

## Transient errors and retry strategy

COM calls to AutoCAD (and compatible hosts: BricsCAD, ZWCAD, GstarCAD) can fail transiently when the application is busy processing. The bundled `cadlib.acad` module retries with backoff and classifies errors:

**Transient (retried with exponential backoff, base 0.4 s, max 5 s, up to 12 attempts):**
- `HRESULT -2147418111` (RPC_E_CALL_REJECTED): "Call was rejected by the callee." CAD is busy.
- `HRESULT -2147417846`: Similar; less common.
- Late-binding `AttributeError: <name>.X`, where `<name>` is `<unknown>`, the method that returned the object (`Open.Activate` for a document from `Documents.Open`) or the ProgID (`AutoCAD.Application.24.3.Quit`): the member was not available yet (right after start, after loading a document, or while CAD was busy) or the proxy went stale. Python's own `AttributeError` messages (`'X' object has no attribute 'y'`) are not retried.

**Permanent (reported immediately as CadError):**
- `HRESULT -2145386296`: Invalid execution context (usually parallel clients interfering; see "One client per instance" below).
- `DISP_E_EXCEPTION` with an AutoCAD error code: Use the code number; do not rely on the localized message.

The bundled retry wraps the entire expression, not individual property accesses. For example:
```python
retry(doc.Blocks.Item, index)  # Evaluates Blocks before retry starts; does not help.
retry_expr(lambda: doc.Blocks.Item(index))  # Entire lookup is retried; correct.
```

After a transient error is resolved, the module waits for `GetAcadState().IsQuiescent` before returning, ensuring the application is ready for the next operation.

## One client per CAD instance

Each running instance of AutoCAD, BricsCAD, ZWCAD or GstarCAD should have at most one automation client (the scripts). Parallel clients interfere with each other and trigger "invalid execution context" errors. The bundled `cadlib.runs` module holds a per-instance lock file (with PID and command name) in the user's temp directory. A second client detects the lock and refuses to start, reporting the PID of the owner.

If a previous run crashed and left a lock, the lock holder's PID is checked: if the process is gone, the lock is taken over; if it is still running, the new client refuses with BUSY exit code 4.

## Starting a CAD session

Always start CAD with `AcadSession.start()`; do not write raw COM start-up code. The session library creates a NEW process (a plain attach would connect to whatever CAD instance the user already has open and drive their documents), finds the newest AutoCAD version from the registry, uses dynamic late binding, and compares the process list before and after to attribute exactly one new PID to the session. That PID is recorded for `cleanup`, tied to the Python process with a kill-on-close job object where Windows allows it, and guarded by a watchdog. Raw start-up code in a script has none of this, which is how orphan `acad.exe` processes get left behind.

Starting a CAD application needs the user's consent (see the main instructions); ask before you run any code from this file.

## Late binding (dynamic dispatch) vs `gen_py`

The library uses dynamic late binding (it imports `win32com.client.dynamic` and generates no type stubs). Do NOT use `makepy` (the `gen_py` cache); it breaks across AutoCAD versions and complicates distribution.

## Quitting cleanly

`AcadSession.quit()` must:
1. Re-fetch the app object (a late-binding proxy may be stale after closing documents; `AttributeError: <unknown>.Quit` or `AttributeError: AutoCAD.Application.<version>.Quit` otherwise)
2. Call `Quit()`
3. Wait for the own PID to disappear (with timeout; `Quit` is asynchronous and can hang)
4. If the PID is still running after timeout, terminate it directly
5. Remove `.dwl` and `.dwl2` lock files the session created

If `Quit()` hangs (timeout after 90 seconds), escalate to a graceful close of the PID, then forced termination if that fails.

Leaving the `with AcadSession.start() as session:` block calls `quit()` for you, also after an error. Never end a script with the CAD process still running, and never stop CAD by image name (`taskkill /IM acad.exe` would also kill the user's own sessions); the library only ever stops the PID it started.

## Document selection

Never use `ActiveDocument`. Always open documents through the session, which addresses them by their full normalized path:

```python
doc = session.open(path, readonly=True)  # readonly=False only for a staged copy you will edit
```

The session keeps a registry of the documents it opened, so `Close()` is called only on those, never on the user's open files. A file the user already has open is refused with `DOC_OPEN_BY_USER`.

## Exporting DXF and viewport status

When exporting a DWG to DXF via COM (`SaveAs` with format "DXF"), activate each layout before export. `session.export_dxf(src, dst)` (and `python scripts/cad.py convert`) does this for you; in your own code, work on the document the session opened:

```python
raw = doc.raw  # doc = session.open(copy, readonly=True)
for index in range(retry_expr(lambda: raw.Layouts.Count)):
    layout = retry_expr(lambda i=index: raw.Layouts.Item(i))
    retry_expr(lambda: setattr(raw, "ActiveLayout", layout))
# Now SaveAs captures viewport status correctly
```

Without this step, viewport `status` fields may be unreliable, especially the `status == 1` flag that marks the "primary" viewport. This is a COM export artifact, not a DXF issue.

## System variables and automatic runs

Inside automatic runs (not user sessions), set system variables via the bundled context manager:

```python
with session.sysvars(doc, BACKGROUNDPLOT=0, ISAVEBAK=0, ISAVEPERCENT=0):
    # Operations inside the context have these values set
    # After the context, they are restored
    pass
```

Use this for:
- `BACKGROUNDPLOT=0`: Plot synchronously, not in the background
- `ISAVEBAK=0`, `ISAVEPERCENT=0`: Disable automatic backups during batch export

**Never touch `FILEDIA`** (File Dialog Enabled); changing it persists in the user's profile and can disable Open/Save dialogs.

## Viewport selection and operations

Viewport operations (zoom, pan, lock) require the layout to be active:

```python
doc.ActiveLayout = doc.Layouts("Layout1")
viewport = doc.PaperSpace(0)  # Now access viewport properties
```

Some operations (e.g., setting `ClipBoundary`) require `ZoomWindow` on the viewport first. Refer to the layout's `.Viewports` collection, not `doc.Viewports` (which is model space).

## Handling MTEXT and special characters

MTEXT can contain formatting codes (`\P` for paragraph, `{\f…}` for font, `^I` for tab) and Unicode sequences. Always:
1. Extract via `dxf.get_mtext_text('plain_text')` in ezdxf (yields plain text without codes)
2. Perform the match/replace on plain text
3. Apply the replacement to the raw property (which preserves codes)

Example:
```python
plain = entity.get_mtext_text()
if "old" in plain:
    raw_value = entity.dxf.text
    raw_value = raw_value.replace("old", "new")
    entity.dxf.text = raw_value
```

## Plotting: do not refresh a layout's plot device

`Layout.RefreshPlotDeviceInfo()` is needed after you assign a different plotter (`ConfigName`) so that the list of paper sizes is current. Calling it on a layout that already has a working PDF plotter resets the layout's plot setup to the device defaults, and the PDF can come out almost empty (a few strokes instead of the sheet). A real 45 000-object plan plotted completely without the call and as 3 objects with it. Change the device only when the layout has none or a non-PDF one, refresh only then, and always check the output: `plot` fails with `PLOT_BAD_OUTPUT` when the PDF has almost no drawing content, because AutoCAD reports success for an empty plot.

Measured on AutoCAD 2024 (one fresh session per case; a synthetic A3 sheet and a real A1 plan):

| Layout plotter | `PlotToFile(path)` | `PlotToFile(path, "DWG To PDF.pc3")` | `ConfigName = PDF` + `RefreshPlotDeviceInfo()` first |
|---|---|---|---|
| none | returns `False`, writes nothing | PDF at the layout's paper size | media reset to the device default (ANSI A on a metric sheet), wrong page |
| DWF or a printer | writes that device's format, not a PDF | PDF with the layout's own page setup | page setup reset |
| PDF | PDF, correct | PDF, correct | page setup reset; the real A1 plan came out as an empty ANSI A page |

So naming the PDF device in the `PlotToFile` call is safe and keeps the page setup; reassigning the plotter or refreshing the device is what loses it. `plot` and `plot_layout_pdf` follow exactly this table, so prefer them over your own plot code.

## Window selection and rectangular regions

SelectionSet filtering by window requires the document's `ActiveLayout` to be set and often requires `ZoomWindow` first:

```python
raw = doc.raw  # the document the session opened, never ActiveDocument
retry_expr(lambda: setattr(raw, "ActiveLayout", raw.Layouts.Item("Layout1")))
ss = retry_expr(lambda: raw.SelectionSets.Add("filter_set"))
retry_expr(lambda: ss.Select(0, point_array([p1], dims=3), point_array([p2], dims=3)))  # 0 = window
```

## Copying objects and array transfers

When copying objects between documents, use `CopyObjects` with the array as a `VARIANT`:

```python
source_ids = win32com.server.util.wrap(entity_ids)
dest_ids = doc.ModelSpace.CopyObjects(source_ids, doc.ModelSpace)
```

Coordinate arrays and variant transfers can trigger memory issues or HRESULT errors if not properly typed.

## Recognizing other CAD hosts via registry

Besides AutoCAD, the following hosts expose COM automation:
- **BricsCAD**: ProgID `BricscadApp.AcadApplication`; API largely compatible with AutoCAD
- **ZWCAD**: ProgID `ZWCAD.Application`; often lags in API coverage
- **GstarCAD**: ProgID `GStarCAD.Application[.NN]` (version-specific); experimental support

The bundled `doctor` command lists all detected hosts by querying the Windows registry. Support for non-Autodesk hosts is marked experimental; test on actual drawings before relying on it.

## Editing DWG through COM via `edit` command

The `edit` command edits DWG files through the CAD application using COM. The process is:

1. **Pass 1 (validation)**: The original DWG is converted to DXF via COM (using the `export_dxf` method with all layouts activated), and all preconditions are checked against the DXF. Nothing is written.

2. **Pass 2 (apply)**: A staged copy of the DWG is opened with `AcadSession.open(path, readonly=False)`, and each edit target is modified via `HandleToObject`, then the document is saved with `AcadSession.save_dwg`.

3. **Verification**: The edited DWG is exported to a temporary DXF and fingerprinted, then compared with the original's fingerprint to confirm the changes match the plan.

### Handling entity types and property mapping

When setting properties on a DWG entity via COM, property names are different from DXF names. The `edit` command maps DXF-style property names to COM members automatically (see `cadlib/acad_edit.py`). For example:

- DXF `layer` → COM `Layer`
- DXF `color` → COM `color`
- DXF `height` → COM `Height`
- DXF `rotation` → COM `Rotation` (note: COM uses radians)

If you write custom code, use the COM names directly:

```python
entity.Layer = "New-Layer"
entity.Height = 2.5
entity.Rotation = math.radians(45)
```

For TEXT, MTEXT, and ATTRIB entities, the width factor (`width` in DXF) maps to:

- TEXT, ATTRIB: `ScaleFactor`
- MTEXT: `Width`

### Unsupported operations on DWG

The `edit` command does not support `pan-viewport` on DWG sources. Panning the model view of a paper-space viewport would require the viewport to be activated and a CAD command (like `pan` or viewport scroll) to be issued, which has not been verified on real hardware. For now, ask the user to export a DXF, edit that, and convert back if viewport panning is needed.

### Attribute editing

Block references (INSERT) can have attributes (ATTRIB). To edit an attribute, the plan must reference the INSERT entity's handle and use `replace-text` or `set-props` on the attribute's text or properties.

The `expect` block can check attributes:

```json
{
  "expect": {
    "type": "INSERT",
    "attrib": {"TAG1": "old value", "TAG2": "another value"}
  }
}
```

### Verification of DWG edits

After editing a DWG, the edited file is re-exported to DXF for verification. The report notes the DWG version the file was saved in (detected from the file header). Always read the edited file back with `find` or `dump` to get fresh handles before planning further edits.

### Performance: never loop through entities via COM

Every property access on a COM object is a cross-process RPC call. Looping over thousands of entities to find targets is extremely slow. Instead:

1. Use `find` or `dump` to locate targets by handle (fast, in-process).
2. Use `HandleToObject` to get the COM object for that single handle.
3. Modify the object and move on.

Example (wrong):

```python
# SLOW: hundreds of RPC calls
for entity in doc.ModelSpace:
    if "target text" in entity.TextString:
        entity.TextString = "new text"
```

Example (right):

```python
# FAST: one find call + one property access
handle = "1A5"  # from find output
entity = doc.HandleToObject(handle)
entity.TextString = "new text"
```
