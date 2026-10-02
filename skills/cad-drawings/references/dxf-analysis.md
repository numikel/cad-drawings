# DXF analysis: reading and querying with ezdxf

Read this when you write custom ezdxf code or encounter a DXFAttributeError, need to understand how ezdxf represents DXF concepts, or want to query entities by attributes that are not obvious in the DXF file.

## Loading a DXF file

```python
import ezdxf
from cadlib.dxf import load_dxf

# Bundled helper (handles DWG→DXF conversion internally)
dwg = load_dxf(path, run_context)

# Direct ezdxf with recovery
dwg = ezdxf.readfile(path, recover=True)
```

Always use `recover=True` for files that may be damaged or partially written. The recovery mode attempts to skip corrupted sections and rebuild the file. For a file that opens fine in AutoCAD but ezdxf rejects, check the file's AutoCAD version and explicit save format.

## Iterating over entities

**Never assume model space is flat.** Entities live in:
- **Model space**: `dwg.modelspace()` — the main drawing area
- **Paper space (layouts)**: `dwg.paperspace(name)` or iterate `dwg.paperspace_layouts()`
- **Block definitions**: `dwg.blocks[block_name]` — entities inside block definitions (not instances)
- **XREFs**: External files; handled specially (see "External references" below)

Iterate carefully:

```python
# Model space
for entity in dwg.modelspace():
    print(entity.dxf.layer)

# All paper-space layouts
for layout_name in dwg.layouts:
    for entity in dwg.paperspace(layout_name):
        print(entity)

# Block definitions
for entity in dwg.blocks[block_name]:
    print(entity)
```

## Entity attributes and DXFAttributeError

Entities have two layers of attributes:

```python
entity.dxf.layer  # DXF attribute (always safe to read)
entity.ezdxf_layer  # ezdxf computed property (may not exist for all types)
```

If an attribute is not set or not valid for an entity type:

```python
value = entity.dxf.get("unknown_attr", default_value)
# Safer than entity.dxf.unknown_attr, which raises DXFAttributeError
```

Check if an attribute is supported:

```python
if "layer" in entity.dxf:  # Supported
    layer = entity.dxf.layer
```

## Plain text vs raw text in MTEXT

MTEXT (multiline text) stores raw, formatted strings with control codes:

```
{\fAutocad|b0|i0|c1|p0;Calibri|b0|i0|c0|p0;Symbol}\P
Paragraph 1\P
{\C1;Red text}
```

ezdxf provides two views:

```python
import ezdxf.text

plain = ezdxf.text.plain_text(mtext_entity.dxf.text)
# 'Paragraph 1\nRed text'

raw = mtext_entity.dxf.text
# Full formatted string with control codes
```

When editing MTEXT:
1. Extract plain text: `plain_text(raw_value)`
2. Match on the plain version
3. Replace in the raw string
4. Assign back to `entity.dxf.text`

After editing, MTEXT with columns may overflow into hidden columns; if the rendered result looks wrong, recreate the entity with `add_mtext()` and copy properties from the original.

## Block attributes (ATTRIB and ATTDEF)

Block attributes are separate entities (ATTRIB for block insertions, ATTDEF for the block definition):

```python
# In a block definition
for entity in dwg.blocks["BlockName"]:
    if entity.dxf.type == "ATTDEF":
        print(f"Attribute tag: {entity.dxf.tag}, value: {entity.dxf.default}")

# In an insertion (block reference)
for entity in dwg.modelspace():
    if entity.dxf.type == "INSERT":
        if entity.has_attribs:
            for attrib in entity.attribs:
                print(f"Attribute: {attrib.dxf.tag} = {attrib.dxf.text}")
```

## Handle and version limits

Handles are opaque identifiers valid only within a single file and version. In DXF, handles are stored as hex strings (e.g., `"2A5"`). ezdxf transparently converts them:

```python
entity_id = entity.handle  # ezdxf integer
dwg.entitydb.get(entity_id)  # Look up by handle
```

After editing (particularly after many insertions/deletions), the DXF file's `$HANDSEED` variable may need updating to avoid future handle collisions. ezdxf handles this automatically on save.

If importing entities from an older DXF version, verify compatibility; some entity types or attributes do not exist in older versions.

## Layout and viewport representation

DXF separates model space (not bound to a layout) from paper space (tied to layouts). Layouts are a DXF 2000+ concept:

```python
# Iterate layout names in tab order
for layout_name in dwg.layouts:
    layout = dwg.layouts[layout_name]
    print(layout.name, layout.dxf.paper_height, layout.dxf.paper_width)

# Viewports are entities in paper space
for entity in layout.paperspace():
    if entity.dxf.type == "VIEWPORT":
        print(entity.dxf.center, entity.dxf.width, entity.dxf.height)
```

Each layout has a default overall viewport. Viewports have status flags (e.g., `status == 1` for the primary viewport; `status == 0` for off, `status < 0` for frozen). The ezdxf normalisation step (applied in render) handles viewport status inconsistencies from COM-exported DXF.

## External references (XREFs)

XREFs are block insertions pointing to external files. Their paths may be relative, archived in compound names (`$0$name` prefix), or unresolved:

```python
for entity in dwg.modelspace():
    if entity.dxf.type == "INSERT":
        name = entity.dxf.name
        if name.startswith("$0$"):
            # Archived/renamed XREF
            real_name = name[3:]
        block_def = dwg.blocks[name]
        if hasattr(block_def, "xref_filename"):
            print(f"XREF: {block_def.xref_filename} (path: {block_def.xref_path})")
```

Unresolved XREFs (files not found) appear in the block table but have an empty or invalid file path. The `info` command lists all XREFs with attachment type (attach/overlay), resolution status, and path.

## Querying by spatial extent

ezdxf provides bounding-box queries:

```python
# All entities within a rectangle
msp = dwg.modelspace()
for entity in msp.query("*").bbox.inside((x1, y1), (x2, y2)):
    print(entity)

# Specific type
for entity in msp.query("TEXT").bbox.inside((x1, y1), (x2, y2)):
    print(entity.dxf.text)
```

The `dump` command uses this internally to filter by window.

## Paper space vs model space in rendering

Layouts display two spaces:
- **Model space**: referenced through a viewport with scale and rotation
- **Paper space**: drawn at 1:1 (sheet units)

When rendering, both are included. If a layout shows no content, check:
1. Are all viewports frozen or off?
2. Are all layers in the viewport's view window frozen or off?
3. Is the viewport's clip boundary (if set) occluding the content?

The `render` command automatically applies workarounds for ezdxf rendering (viewport normalisation, layer exclusion, CTB application).

## Dictionary entries and XData

DXF maintains non-graphical data in dictionaries and extended data (XData). Most queries ignore these:

```python
# Extended data on an entity
if entity.has_xdata("ACAD_REACTORS"):
    xdata = entity.get_xdata("ACAD_REACTORS")
```

Do not rely on dictionary or XData for search; they are not indexed and hidden from `find` queries. For finding, search graphical entities only.

## SHX fonts and substitution

SHX (shape files) are font files used by AutoCAD for special characters and shapes. If the DXF references a missing SHX:

```python
if entity.dxf.type in ("TEXT", "MTEXT"):
    font_name = entity.dxf.style
    # Check if font_name points to an SHX file
```

The `info` command reports missing SHX fonts. Substitute fonts must be available on the system or embedded in the PDF. For rendering without the SHX, substitute a TTF font with `support_dirs` parameter.

## Checking for support and entity versions

Not all DXF entity types are fully supported by ezdxf. Check:

```python
if entity.dxf.is_supported:
    # Safe to access all properties
else:
    # Use .dxf.get() to safely read properties
    value = entity.dxf.get('attribute_name')
```

For older DXF versions, some entities or attributes may not exist. Always use `get()` when reading optional attributes.
