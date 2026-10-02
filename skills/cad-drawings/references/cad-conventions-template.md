# CAD_CONVENTIONS.md template

Read this when you create or update a project's `CAD_CONVENTIONS.md` together with the user (interview plus inference from existing drawings), or need to know which conventions a project must define before you edit or plot its drawings. Copy the skeleton below into the project, fill every row and delete this banner. `CAD_CONVENTIONS.default.md` is the same skeleton, pre-filled with standard defaults.

## How to fill it

- `Source`: `user` (stated) · `inferred: <evidence>` (e.g. `inferred: 41 of 44 layouts use A1`) · `default` (copied from the standard default) · `n/a`. Never guess: leave `Value` empty and ask.
- Infer first, then ask. Sample at least three recently issued drawings; the majority pattern becomes the rule, outliers go to §17. Ask only what drawings cannot show: delivery format, naming, approvals, the never-add list, domain rules.
- One line per rule; keep the keys unchanged so agents can find them; record agreed changes in §17. Precedence: user instruction → this file → the drawing being edited → `references/drafting-standards.md`.

---

# CAD_CONVENTIONS — `<project name>`

## 0. Meta
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Scope (folders, file patterns covered) | | | ask |
| Version · date · maintainer · status (draft / agreed) | | | ask |
| Language(s) of drawing text | | | text content of recent sheets |
| Legacy policy for drawings that deviate from this file | | | ask: match existing and report, or convert |
| Related documents (CAD manual, BIM execution plan, client standard) | | | ask |

## 1. Units and coordinates
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Model units (`$INSUNITS`) and `$MEASUREMENT` | | | header; model extents vs known object size |
| Paper (layout) units | | | page setups of layouts |
| Linear dimension unit, precision, decimal marker | | | dim styles: DIMLUNIT, DIMDEC, DIMDSEP |
| Levels / elevations: unit, format, reference datum | | | level-mark blocks and their text |
| Angle format | | | dim styles: DIMAUNIT |
| Coordinate system, base point, site datum | | | survey notes, geolocation, XREF insertion points |
| Date format | | | title block values |

## 2. Orientation and views
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Plan orientation on sheets (north up, project north, other) | | | north-arrow rotation vs viewport twist |
| North arrow: block name, where it must appear | | | block names; layouts containing it |
| Projection method (mechanical drawings) | | | projection symbol |
| Grid labels (letters / numbers, direction, skipped letters) | | | grid-bubble texts |
| View and section identification style | | | section and detail mark blocks |

## 3. Sheets
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Allowed sheet sizes and orientation | | | layout paper sizes (counts) |
| Sheet template file (DWT / DWG) | | | ask |
| Frame, margins, grid reference, fold marks | | | frame block geometry |
| Fixed positions (key plan, legend, notes, revision table) | | | recurring positions on sheets |

## 4. Title block
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Block name, insertion point, space (layout / model) | | | inserts with attributes in layouts |
| Drawing number and sheet-number format (regex) | | | existing attribute values |
| Fields the agent must never fill | | | ask (signatures, approvals, stamps) |

| Tag | Meaning (ISO 7200 field) | Mandatory | Filled by | Format / example |
|---|---|---|---|---|
| | | | | |

## 5. Layers
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Standard (ISO 13567, AIA/NCS, client, house) | | | layer table |
| Name pattern (regex) and three examples | | | cluster existing layer names |
| Required layers and their purpose | | | layers present in every sheet |
| Non-plotting layers (viewports, helper geometry) | | | plot flag off; layers of viewport entities |
| Property rule (ByLayer; permitted overrides) | | | share of entities with ByLayer properties |
| Who may add layers, and how they are named | | | ask |

## 6. Text
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Styles: name → font → width factor → oblique angle | | | style table and usage counts |
| Heights on paper by use (notes, labels, view titles, title block) | | | layout text heights; model heights ÷ viewport scale |
| Annotative objects or scaled text | | | annotative flags on text and styles |
| Case, abbreviations list, fixed wording (notes, units) | | | recurring notes |

## 7. Lines and plot styles
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Line groups; lineweight per layer or use | | | layer lineweights; plot style mapping |
| Linetypes and linetype scaling | | | linetype table; `$LTSCALE`, `$PSLTSCALE` |
| Plot style table (CTB / STB file) and colour policy | | | plot settings of layouts |

## 8. Layouts and viewports
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Layout naming; sheets per layout | | | layout names |
| Allowed viewport scales | | | viewport scales (counts) |
| Viewport layer, display lock, annotation scale rule | | | viewport properties |
| What may be drawn in paper space | | | entity types in layouts |

## 9. Dimensions
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Dimension styles and when each is used | | | style usage counts |
| Text height, terminator type and size | | | DIMTXT, DIMBLK / DIMTSZ, DIMASZ, DIMSCALE |
| Associativity; marks for out-of-scale, auxiliary and level values | | | `$DIMASSOC`; samples |

## 10. Symbols, blocks, legends
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Symbol library (path or source) | | | ask |
| Key blocks (north arrow, scale bar, section / detail mark, level mark, revision tag) | | | block names and counts |
| Legend rule (where, what it lists) | | | legend blocks or tables |
| Domain symbol standard | | | ask |

## 11. Revisions
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Revision index scheme; status codes | | | revision table values |
| Revision table: position and columns | | | table block |
| Revision clouds: layer, arc size, tag; earlier clouds kept or cleared | | | cloud polylines per layer |
| Where revision is recorded (title block, table, file name, metadata) | | | ask |

## 12. Files and references
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| File-name pattern, regex, example | | | names of files in the folder |
| DWG / DXF version for delivery | | | file header version |
| XREF path type, binding and overlay policy | | | XREF paths in the drawings |
| Folder structure (work in progress, shared, published, archive) | | | ask |

## 13. Plot and PDF
| Key | Value | Source | Infer from / ask |
|---|---|---|---|
| Device / plotter configuration, media names, plot scale | | | page setups |
| Colour or monochrome; lineweights; fonts embedded or converted; PDF layers | | | existing PDFs |
| PDF naming; one PDF per sheet or per set; sheet order | | | existing PDFs |
| How a PDF is checked before issue | | | ask |

## 14. Delivery checklist
- [ ] `<check>` — one line each; see `CAD_CONVENTIONS.default.md` for a starting list.

## 15. Never add / never change
- `<item>` — one line each; see `CAD_CONVENTIONS.default.md` for a starting list.

## 16. Domain standards and special rules
| Standard or rule | Applies to | Note |
|---|---|---|

## 17. Known deviations and change log
| Date | Item | Decision | By |
|---|---|---|---|
