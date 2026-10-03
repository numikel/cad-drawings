# CAD_CONVENTIONS — standard default (adapt before use)

DEFAULTS, NOT PROJECT RULES. Derived from ISO drafting standards and common CAD practice; no project has agreed to them. Read this when a project has no `CAD_CONVENTIONS.md` and the user chose the standard default: copy it into the project, replace every `<…>`, confirm or change each line with the user, set §0 status to "agreed", delete this banner. Keys match `cad-conventions-template.md`. Tags: `[norm: …]` standard · `[practice]` common practice.

---

## 0. Meta
- **Scope (folders, file patterns covered)**: `<folders>`, `*.dwg`, `*.dxf`
- **Version · date · maintainer · status (draft / agreed)**: default-1 · `<YYYY-MM-DD>` · `<name>` · draft
- **Language(s) of drawing text**: the language already on the sheet; new sheets in `<language>` [practice]
- **Legacy policy for drawings that deviate from this file**: match the drawing inside existing content; new sheets, layers and styles follow this file; report each deviation [practice]
- **Related documents (CAD manual, BIM execution plan, client standard)**: none; add any that exist

## 1. Units and coordinates
- **Model units (`$INSUNITS`) and `$MEASUREMENT`**: 1 unit = 1 mm (`$INSUNITS` 4, `$MEASUREMENT` 1); site and civil models 1 unit = 1 m (`$INSUNITS` 6) [practice]
- **Paper (layout) units**: millimetres; layouts plotted 1:1 [practice]
- **Linear dimension unit, precision, decimal marker**: mm, no unit symbol, stated once ("Dimensions in millimetres"); 0 decimals for buildings; decimal comma, or a point where the project language uses one [norm: ISO 129-1:2018 §4.1.1, §4.3]
- **Levels / elevations: unit, format, reference datum**: metres, 3 decimals, signed, relative to the project datum (e.g. +3,150) [practice]
- **Angle format**: decimal degrees, unit symbol always shown [norm: ISO 129-1:2018 §4.3]
- **Coordinate system, base point, site datum**: keep the model's base point and survey datum; never move, rotate or rescale existing geometry [norm: ISO 7519:2025 §4.1.3]
- **Date format**: YYYY-MM-DD [practice]

## 2. Orientation and views
- **Plan orientation on sheets (north up, project north, other)**: north up; with a skewed building grid, project north up plus a true-north arrow; rotate views, never the model [practice]
- **North arrow: block name, where it must appear**: `NORTH_ARROW` on every plan view and key plan [norm: ISO 9431:1990 §5.2.5; practice]
- **Projection method (mechanical drawings)**: first-angle, symbol shown [norm: ISO 5456-2:1996]
- **Grid labels (letters / numbers, direction, skipped letters)**: numbers 1, 2, 3 … on one axis, letters A, B, C … on the other, I and O skipped [practice]
- **View and section identification style**: capital letter at the viewing arrow and above the view ("A–A"), upright [norm: ISO 128-3:2022 §4.1]

## 3. Sheets
- **Allowed sheet sizes and orientation**: ISO A0–A4; A0–A3 landscape, A4 portrait or landscape; smallest legible size; no elongated sizes [norm: ISO 216:2007; ISO 5457:1999 + Amd 1:2010]
- **Sheet template file (DWT / DWG)**: `<path>`; if none exists, build one from §3–§4
- **Frame, margins, grid reference, fold marks**: borders 20 mm left, 10 mm elsewhere; 0.7 mm frame; centring marks; 50 mm grid-reference fields (letters top to bottom without I and O, numbers left to right, 3.5 mm characters); prints folded to A4, title block on top [norm: ISO 5457 §4; practice]
- **Fixed positions (key plan, legend, notes, revision table)**: title block bottom right, revision table directly above it, legend, notes and key plan in the column above [norm: ISO 9431 §5; ISO 5457 §4.1]

## 4. Title block
- **Block name, insertion point, space (layout / model)**: `TITLE_BLOCK`, 180 mm wide, one per layout, paper space, lower-right corner of the frame [norm: ISO 7200:2004 §6]
- **Drawing number and sheet-number format (regex)**: drawing number = file ID from §12; sheets "n of N" [practice]
- **Fields the agent must never fill**: APPROVER, signatures, stamps, seals; CREATOR only with a name the user gives [practice]

| Tag | Meaning [norm: ISO 7200:2004 §5] | Mandatory | Filled by | Format / example |
|---|---|---|---|---|
| OWNER · DWG_NO | Legal owner · identification number | yes · yes | template · agent | name or logo · ≤ 16 characters, = file ID |
| REV · ISSUE_DATE | Revision index · date of issue | no · yes | agent at release | A, B, C … (no I, O) · YYYY-MM-DD |
| SHEET · SHEETS | Sheet number · number of sheets | yes · no | agent | 2 · 5 |
| TITLE · SUBTITLE | Title · supplementary title | yes · no | text agreed with the user | ≤ 25–30 characters per line |
| DOC_TYPE · STATUS | Document type · document status | yes · no | agent · user | "Floor plan" · In preparation / Under approval / Released / Withdrawn |
| CREATOR · APPROVER | Creator · approval person | yes · yes | user · a person only | names |
| DEPT · TECH_REF · SCALE · PAPER · LANG | Department · technical reference · main scale · paper size · language | no | user · user · agent · agent · agent | — · — · 1:100 · A1 · en |

## 5. Layers
- **Standard (ISO 13567, AIA/NCS, client, house)**: ISO 13567-2, default field widths, no separators: Agent (2) + Element (6) + Presentation (2) [+ Status (1)]; US projects use the AIA/NCS format (`A-WALL-FULL-N`) instead [norm: ISO 13567-2:2017 §5–7]
- **Name pattern (regex) and three examples**: `^[A-Z0-9_-]{10,11}$` · `A-WALL__E-N` · `A--_____T-` · `A--_____UV`
- **Required layers and their purpose**: starter set below; element codes from the project classification table, else 6-character mnemonics padded with `_`, documented here; `-_____` = not element-related [norm: ISO 13567-2 §5.2, §6.2]
- **Non-plotting layers (viewports, helper geometry)**: `A--_____C-` helpers and `A--_____UV` viewport frames, plot flag off; never layer 0 or Defpoints [practice]
- **Property rule (ByLayer; permitted overrides)**: colour, linetype and lineweight ByLayer, no overrides [practice]
- **Who may add layers, and how they are named**: anyone, in this pattern only, logged in §17 [practice]

| Layer (agent `A-` = authoring discipline) | Content | Lineweight mm | Linetype | Plot |
|---|---|---|---|---|
| `A--_____G-` | grid lines and grid labels | 0.18 | long-dashed dotted | yes |
| `A-WALL__E-N` | new elements cut by the plan or section plane (example: walls) | 0.50 | continuous | yes |
| `A-WALL__E-E` · `A-WALL__E-R` | existing to remain · to be removed | 0.35 · 0.25 | continuous · dashed | yes |
| `A-EQUIP_E-` | elements seen in view (example: equipment) | 0.25 | continuous | yes |
| `A--_____T-` · `A--_____D-` · `A--_____J-` | model text · dimensions and levels · section and detail marks | 0.25 · 0.18 · 0.25 | continuous | yes |
| `A--_____H-` · `A--_____K-` | hatching · revision clouds and tags | 0.13 · 0.35 | continuous | yes |
| `A--_____F-` · `A--_____O-` · `A--_____W-` · `A--_____N-` · `A--_____L-` | frame and centring marks · grid-reference lines and characters · title block and revision table · notes · legend | 0.70 · 0.35 · 0.35 · 0.25 · 0.25 | continuous | yes |
| `A--_____C-` · `A--_____UV` | helper geometry · viewport frames | — | — | no |

## 6. Text
- **Styles: name → font → width factor → oblique angle**: `ISO` → single-stroke or sans-serif font in the style of ISO 3098 type B that every recipient has (AutoCAD-family example: isocp.shx) → 1.0 → 0° [norm: ISO 3098-1:2015 §5.5]
- **Heights on paper by use (notes, labels, view titles, title block)**: 2.5 notes, dimensions, tables · 3.5 labels, room names, grid labels · 5 view titles · 7 drawing title and number; nothing below 2.5 [norm: ISO 3098-1 §5.3; practice]
- **Annotative objects or scaled text**: layout text at full size; model text height = paper height × scale denominator; annotative objects only where the drawing already uses them [practice]
- **Case, abbreviations list, fixed wording (notes, units)**: as found in the drawing; abbreviations explained in notes or legend [norm: ISO 9431 §5.2.2]

## 7. Lines and plot styles
- **Line groups; lineweight per layer or use**: narrow/wide/extra-wide 0.25/0.5/1.0 on A1–A0, 0.18/0.35/0.7 on A3–A2; per layer as in §5 [norm: ISO 128-2:2022 §5.1; practice]
- **Linetypes and linetype scaling**: ISO linetypes scaled relative to paper so patterns plot the same in every viewport [norm: ISO 128-2 §5.3; practice]
- **Plot style table (CTB / STB file) and colour policy**: monochrome, lineweight from the layer (AutoCAD-family example: monochrome.ctb); colour only where it carries meaning [practice]

## 8. Layouts and viewports
- **Layout naming; sheets per layout**: one sheet per layout, named with the sheet number (01, 02 …) [practice]
- **Allowed viewport scales**: ISO 5455 series, typically 1:1–1:1000 [norm: ISO 5455:1979 §5.1]
- **Viewport layer, display lock, annotation scale rule**: `A--_____UV`, display locked, annotation scale = viewport scale [practice]
- **What may be drawn in paper space**: frame, title block, revision table, legend, notes, key plan, view titles, scale bar, north arrow; no model geometry [practice]

## 9. Dimensions
- **Dimension styles and when each is used**: `ISO-25` for all views; scale variants through annotative scaling or the style's overall scale, never per-dimension overrides [practice]
- **Text height, terminator type and size**: 2.5 mm text; closed filled arrowheads 2.5 mm (construction projects often agree 45° oblique strokes) [practice]
- **Associativity; marks for out-of-scale, auxiliary and level values**: associative (`$DIMASSOC` 2); out-of-scale values underlined; auxiliary values in parentheses [norm: ISO 129-1:2018 §4.1.3–4.1.4]

## 10. Symbols, blocks, legends
- **Symbol library (path or source)**: `<path>`; otherwise reuse definitions already in the drawing [practice]
- **Key blocks (north arrow, scale bar, section / detail mark, level mark, revision tag)**: `NORTH_ARROW`, `SCALE_BAR`, `SECTION_MARK`, `DETAIL_MARK`, `LEVEL_MARK`, `REV_TAG`; geometry on layer 0, ByBlock/ByLayer properties, attributes for varying text [practice]
- **Legend rule (where, what it lists)**: on every sheet with symbols, listing exactly the symbols used on it [norm: ISO 7519:2025 §4.1.2; practice]
- **Domain symbol standard**: none by default (§16)

## 11. Revisions
- **Revision index scheme; status codes**: A, B, C … without I and O; before the first release STATUS "In preparation" and no REV [norm: ISO 7200:2004 §5.1.4, §5.3.8]
- **Revision table: position and columns**: directly above the title block, same width; Rev · Description · Date · Drawn · Approved [norm: ISO 9431:1990 §5.2.6]
- **Revision clouds: layer, arc size, tag; earlier clouds kept or cleared**: `A--_____K-`, arcs about 10 mm on paper, `REV_TAG` with the index; earlier clouds cleared at the next release [practice]
- **Where revision is recorded (title block, table, file name, metadata)**: title block REV = last table row = transmittal; not in the file name [practice]

## 12. Files and references
- **File-name pattern, regex, example**: `PROJECT-ORIGINATOR-FUNCTION-SPATIAL-FORM-DISCIPLINE-NUMBER`, hyphens only, e.g. `PRJ01-ORG-ZZ-00-DR-A-0101.dwg`; stem regex `^[A-Z0-9]+(-[A-Z0-9]+){6}$` [practice: common in ISO 19650 projects]
- **DWG / DXF version for delivery**: the version received; new files in the version agreed with recipients [practice]
- **XREF path type, binding and overlay policy**: relative paths; overlay unless nesting is intended; bind only for the final archive [practice]
- **Folder structure (work in progress, shared, published, archive)**: `WIP/`, `SHARED/`, `PUBLISHED/`, `ARCHIVE/` (superseded issues) [practice]

## 13. Plot and PDF
- **Device / plotter configuration, media names, plot scale**: PDF plotter configuration, vector quality ≥ 1200 dpi, media = layout sheet size, scale 1:1, never fit-to-paper [practice]
- **Colour or monochrome; lineweights; fonts embedded or converted; PDF layers**: monochrome; lineweights on; TrueType fonts embedded (SHX text becomes geometry); PDF layers on [practice]
- **PDF naming; one PDF per sheet or per set; sheet order**: one PDF per sheet, `<drawing number>.pdf`; optional combined set in sheet order [practice]
- **How a PDF is checked before issue**: page size; scale measured on a known dimension or the scale bar; fonts, XREFs and images present; title block data [practice]

## 14. Delivery checklist
- [ ] `$INSUNITS` set; nothing on layer 0 or Defpoints; helper geometry on non-plotting layers.
- [ ] Only the requested changes (compared with the previous issue); deviations reported.
- [ ] Title block mandatory fields filled or reported missing; REV and date updated; approval left to a person.
- [ ] Revision table row and clouds for this revision; earlier clouds cleared.
- [ ] Legend matches the symbols used; every view has a title and scale; north arrow on plans.
- [ ] Viewports locked at standard scales; audit clean; XREFs resolve through relative paths; PDFs checked per §13; names per §12.

## 15. Never add / never change
- Signatures, approvals, stamps, seals; invented dimensions, levels, names or dates.
- Personal data; tool or AI attribution; notes-to-self or placeholders on plotting layers.
- Layers, text styles or dimension styles outside §5–§9; per-object colour overrides.
- Exploded dimensions, blocks or hatches; moved, rotated or rescaled model geometry; changed units or origin; purges, renames or deletions nobody asked for.

## 16. Domain standards and special rules
| Standard or rule | Applies to | Note |
|---|---|---|
| none by default | — | examples: ISO 23601 escape plans, ISO 7010 safety signs |

## 17. Known deviations and change log
| Date | Item | Decision | By |
|---|---|---|---|
| `<YYYY-MM-DD>` | Started from the standard default | pending agreement | `<name>` |
