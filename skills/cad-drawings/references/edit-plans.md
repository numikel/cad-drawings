# Edit plans: safe DXF and DWG editing

The `edit` command executes a *plan* of changes written as JSON (conforming to `assets/edit-spec.schema.json`). Every edit targets one entity by handle and is validated in a two-pass process: pass 1 checks everything against the unchanged drawing and writes nothing; pass 2 applies changes and verifies the result.

## Getting started

### Step 1: Find entities by handle

Use `find` or `dump` to locate the entities you want to change:

```sh
python scripts/cad.py find <file> --pattern "old text"
python scripts/cad.py dump <file> --space model --type TEXT --limit 50
```

The output includes entity handles, types, layers, text content, and insertion points. Note down the handles.

### Step 2: Build a plan

Write a JSON file conforming to `assets/edit-spec.schema.json`. Example:

```json
{
  "version": 1,
  "base": {
    "path": "/path/to/drawing.dxf",
    "sha1": "a1b2c3d4e5f6..."
  },
  "note": "Update title blocks and layer assignments",
  "edits": [
    {
      "id": "title_1",
      "op": "replace-text",
      "handle": "15A",
      "expect": {
        "type": "TEXT",
        "layer": "A-TEXT",
        "text": "DRAFT"
      },
      "args": {
        "old": "DRAFT",
        "new": "FINAL"
      }
    },
    {
      "id": "fix_layer",
      "op": "set-props",
      "handle": "1B2",
      "expect": {
        "type": "LWPOLYLINE",
        "layer": "Wrong-Layer"
      },
      "args": {
        "props": {"layer": "A-WALL"}
      }
    }
  ]
}
```

### Step 3: Validate the plan

Check the plan before applying:

```sh
python scripts/cad.py edit --spec edits.json --dry-run
```

The output shows which edits would be applied (status `planned`) and which are already in the target state (`already_applied`). The report file lists any precondition failures.

### Step 4: Apply

On a DXF:

```sh
python scripts/cad.py edit --spec edits.json
```

On a DWG (needs consent):

```sh
python scripts/cad.py edit --spec edits.json --allow-com
```

The edited file lands in the run directory: `<stem>_edited.dxf` or `<stem>_edited.dwg`. The report `edit-report.json` and the changeset `changes.jsonl` document every change.

### Step 5: Verify

Render the edited file and compare with the original:

```sh
python scripts/cad.py render <edited-file>
python scripts/cad.py fingerprint <original> --out fp1.json
python scripts/cad.py fingerprint <edited-file> --out fp2.json
python scripts/cad.py diff fp1.json fp2.json
```

If the verification report shows unintended changes, do NOT deliver the edited file. Fix the plan and re-run.

## Edit operations

Each `edit` object names an operation (`op`), the target entity (`handle`), and preconditions (`expect`). The `args` object holds operation-specific arguments.

### replace-text

Replace a substring in TEXT, MTEXT, or ATTRIB:

```json
{
  "op": "replace-text",
  "handle": "1A5",
  "expect": {"type": "TEXT", "text": "old"},
  "args": {
    "old": "old",
    "new": "new",
    "count": 1
  }
}
```

- `old` is matched exactly in the raw string (for MTEXT, formatting codes are included; match on plain text first, then build your `old` string).
- `new` is the replacement; for MTEXT, codes outside the match region are preserved.
- `count` (optional): if the substring appears more than once, specify how many times to replace. If not given, `old` must occur exactly once.

### set-props

Change DXF properties (layer, color, height, rotation, etc.):

```json
{
  "op": "set-props",
  "handle": "2B3",
  "expect": {"type": "TEXT", "layer": "Old-Layer"},
  "args": {
    "props": {
      "layer": "New-Layer",
      "color": 1,
      "height": 2.5
    }
  }
}
```

Supported properties (DXF-style names):

| Type | Properties |
|---|---|
| All entities | `layer`, `color`, `linetype`, `lineweight`, `ltscale` |
| TEXT, MTEXT, ATTRIB, ATTDEF | `height`, `rotation`, `style`, `width` |
| INSERT | `rotation`, `xscale`, `yscale`, `zscale` |
| CIRCLE, ARC | `radius` |

Unknown properties or unsupported entity types fail with `UNSUPPORTED` (exit 2).

### delete

Remove the entity from the drawing:

```json
{
  "op": "delete",
  "handle": "3C4",
  "expect": {"type": "LWPOLYLINE"}
}
```

- ATTRIB entities become "changed" (attributes are part of the INSERT block), not "removed".
- Deletion is immediate; there is no undo.

### move

Displace the entity by a vector:

```json
{
  "op": "move",
  "handle": "4D5",
  "expect": {"type": "CIRCLE", "insert": [100, 200]},
  "args": {
    "vector": [10, 0, 0]
  }
}
```

- The vector is `[dx, dy]` or `[dx, dy, dz]` in model units.
- Works on all geometry types. Paper-space viewports can be moved; use `pan-viewport` to move the view inside a viewport.

### clone

Create a copy of the entity:

```json
{
  "op": "clone",
  "handle": "5E6",
  "expect": {"type": "TEXT", "text": "Original"},
  "args": {
    "vector": [5, 5],
    "target_layer": "Copy-Layer",
    "target_space": "Layout1"
  }
}
```

- `vector` is the offset from the source (`[dx, dy]` or `[dx, dy, dz]`).
- `target_layer` (optional): the copy's layer (default: source layer).
- `target_space` (optional): destination (default: source space, which can be model, a layout, or a block definition).
- The response includes the new handle of the copy.
- `clone` is not idempotent: running the plan twice creates two copies.

### pan-viewport

Move the view centre inside a paper-space viewport:

```json
{
  "op": "pan-viewport",
  "handle": "6F7",
  "expect": {"type": "VIEWPORT"},
  "args": {
    "view_center": [1000, 2000]
  }
}
```

- `view_center` is the model-space point that should appear at the viewport's centre.
- Scale and size are unchanged.
- Works on DXF sources. On DWG (COM), this operation is `UNSUPPORTED` (panning needs the viewport to be activated and a CAD command to run; this has not been verified on real hardware).

## Preconditions: the `expect` block

Every edit carries an `expect` object that must match the entity *before* the edit. The command checks `expect` in pass 1 and refuses the entire plan if any precondition fails.

Supported checks:

| Field | Type | Effect |
|---|---|---|
| `type` | string | Entity type (TEXT, MTEXT, INSERT, etc.) — required |
| `layer` | string | Current layer name |
| `space` | string | Location: `model`, layout name, or `block:<name>` |
| `text` | string | TEXT/MTEXT/ATTRIB content (raw string for MTEXT) |
| `text_is_plain` | boolean | If true, `text` is plain (for MTEXT with formatting) |
| `attrib` | object | For INSERT: tag → current value pairs |
| `insert` | array | Insertion point `[x, y]` or `[x, y, z]` |
| `tolerance` | number | Numeric tolerance (default `1e-6`) |

Example with tolerance:

```json
{
  "expect": {
    "type": "TEXT",
    "insert": [100.0, 200.0],
    "tolerance": 0.1
  }
}
```

## Two-pass semantics

**Pass 1** (no changes):
1. Read the unchanged drawing.
2. Check that the base drawing's SHA-1 matches the plan (file version).
3. Check that every `handle` exists.
4. Check that every `expect` block matches the entity.
5. Check that the edits are sensible (e.g., `old` occurs exactly `count` times).
6. Report all problems at once.

If `--dry-run`: stop here and report which edits would be applied.

**Pass 2** (apply):
1. Work on a copy (never the original).
2. Apply each edit in order.
3. Save the edited file to the run directory.
4. Export a DXF (for DWG sources) and fingerprint both original and edited.
5. Verify the changes match the plan (see below).

## Verification

After applying, the command diffs the original and edited drawings and compares the diff with the plan:

- Every change the diff shows must belong to an applied edit.
- Every applied edit must be visible in the diff (except `clone`, which adds a new entity).
- The state of each edited entity, read back from the file, must match the plan's `after` predictions.

If verification fails:

- The edited file is still written (to `<stem>_edited.dxf/dwg` in the run directory) for inspection.
- The exit code is 7 (`UNINTENDED_CHANGE`), and the report details every discrepancy.
- The edited file is NOT copied to `--out` (do not deliver it).

Unintended changes can mean:
- An edit had a side effect (e.g., moving an entity affected a dependent object).
- A property change was silently clamped by the CAD application.
- The DWG backend (COM) has limitations the DXF backend does not.

## Handle persistence

**Handles are version-specific.** After a file is saved (by `edit`, by the user, or by any CAD operation), handles can change:

- New entities get new handles.
- The `$HANDSEED` variable is incremented.
- Anonymous blocks are renamed.

If the user wants to make more edits, re-read the edited file with `find` or `dump` and rebuild the plan with fresh handles.

Example workflow:

```sh
# Edit 1: change text in two entities
python scripts/cad.py find plan.dxf --pattern "old"
# [write plan_v1.json with handles from this output]
python scripts/cad.py edit --spec plan_v1.json
# plan_edited.dxf is the result

# Edit 2: cannot reuse handles from Edit 1; must re-read
python scripts/cad.py find plan_edited.dxf --pattern "next change"
# [write plan_v2.json with fresh handles]
python scripts/cad.py edit --spec plan_v2.json
```

## Idempotence

Edit plans are idempotent for all operations *except* `clone`:

- If you apply the same plan twice to file v1, the second run reports every edit as `already_applied`.
- The edited file is not modified.
- No error is raised.

`clone` is the exception: each run adds another copy.

## Backends

### DXF source

Uses ezdxf to edit the DXF directly. All operations are supported. The edited file is written to `<stem>_edited.dxf` in the run directory.

### DWG source

Uses COM (the CAD application) to edit the DWG. The original is never opened for writing. The edited file is saved as a new version in the run directory to `<stem>_edited.dwg`.

Limitations:

- `pan-viewport` is not supported (would need to activate the viewport and run a CAD command).
- The edited file is always saved in the original file's version (detected from the DWG header) to avoid unwanted upgrades.
- Properties like `width` (width factor) may be clamped or ignored by COM if they are out of range.

## Exit codes

- 0: applied and verified
- 2: invalid plan or unsupported edit
- 3: DWG without `--allow-com`
- 6: base changed (SHA-1 mismatch), expect failed, or precondition not met (nothing written)
- 7: applied but not verified (unintended change detected; edited file written for inspection, not delivered)
