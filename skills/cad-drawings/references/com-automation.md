# COM automation: connecting to and controlling AutoCAD

Read this when you are writing custom COM code or a COM operation returns an error (HRESULT code, AttributeError, timeout, or hung process). This file documents the most common issues and how the bundled retry and session management handle them.

## Transient errors and retry strategy

COM calls to AutoCAD (and compatible hosts: BricsCAD, ZWCAD, GstarCAD) can fail transiently when the application is busy processing. The bundled `cadlib.acad` module retries with backoff and classifies errors:

**Transient (retried with exponential backoff, base 0.4 s, max 5 s, up to 12 attempts):**
- `HRESULT -2147418111` (RPC_E_CALL_REJECTED): "Call was rejected by the callee." CAD is busy.
- `HRESULT -2147417846`: Similar; less common.
- Late-binding `AttributeError: <unknown>.X`: Attribute was not yet available (usually right after start or after loading a document).

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

## Starting a CAD session: `DispatchEx` vs `Dispatch`

**Use `DispatchEx` to start a new instance:**
```python
from win32com.client import DispatchEx

app = DispatchEx("AutoCAD.Application.24.3")  # Always starts a new process
```

**Avoid `Dispatch`; it connects to an existing instance:**
```python
from win32com.client import Dispatch

app = Dispatch(
    "AutoCAD.Application"
)  # Connects to first running instance, creates new only if none exist
```

The bundled session manager uses `DispatchEx` with dynamic late binding (no `gen_py` cache), finds the newest AutoCAD version from the registry, and checks the PID before and after to confirm a new process was created.

## Late binding (dynamic dispatch) vs `gen_py`

Use dynamic late binding (the module imports `win32com.client.dynamic` and does not generate type stubs):

```python
from win32com.client import GetObject, dynamic
# Explicit late binding; never fails with import errors
```

Do NOT use `makepy` (the `gen_py` cache); it breaks across AutoCAD versions and complicates distribution.

## Quitting cleanly

`AcadSession.quit()` must:
1. Re-fetch the app object (a late-binding proxy may be stale after closing documents; `AttributeError: <unknown>.Quit` otherwise)
2. Call `Quit()`
3. Wait for the own PID to disappear (with timeout; `Quit` is asynchronous and can hang)
4. If the PID is still running after timeout, terminate it directly
5. Remove `.dwl` and `.dwl2` lock files the session created

If `Quit()` hangs (timeout after 90 seconds), escalate to a graceful close of the PID, then forced termination if that fails.

## Document selection

Never use `ActiveDocument`. Always select documents by their full normalized path:

```python
doc = app.Documents.Open(str(path), ReadOnly=readonly)
```

The bundled session manager maintains a registry of documents it opened, so `Close()` is called only on those, never on user's open files.

## Exporting DXF and viewport status

When exporting a DWG to DXF via COM (`SaveAs` with format "DXF"), activate each layout before export:

```python
for layout_name in app.ActiveDocument.Layouts:
    layout = app.ActiveDocument.Layouts(layout_name)
    app.ActiveDocument.ActiveLayout = layout
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

## Window selection and rectangular regions

SelectionSet filtering by window requires the document's `ActiveLayout` to be set and often requires `ZoomWindow` first:

```python
doc.ActiveLayout = doc.Layouts("Layout1")
doc.Utility.GetDistance(sp1)  # Or other view setup
ss = app.ActiveDocument.SelectionSets.Add("filter_set")
ss.SelectByWindow(sp1, sp2)
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
