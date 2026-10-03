# Drafting standards for editing and plotting CAD drawings

Read this when you are about to add, change or delete content in a DWG/DXF drawing, create or modify a layout (sheet), or plot/export sheets, and the project's `CAD_CONVENTIONS.md` is missing or silent on the point. Project conventions always win over this file.

**Note for the reader:** the standards cited here are paywalled. The text below paraphrases them from secondary sources and was checked against the publishers' catalogues in 2026-09 (see §16); it is a guide to where to look, not a substitute for the standard. Before relying on a value in a production workflow, confirm it in the edition your project uses.

Tags: `[norm: ISO 5457 §4.2]` = paraphrase of a published standard (the clause is where to look, the wording here is ours). `[practice]` = widely used CAD practice, not a standard.

## 1. Precedence and first moves

- Authority order: explicit user request → project `CAD_CONVENTIONS.md` → what the drawing already does (nearest comparable objects) → this file. [practice]
- When `CAD_CONVENTIONS.md` and the drawing disagree, apply the file's legacy policy; if it has none, match the drawing inside existing content and report the deviation. [practice]
- Inspect before editing: units (`$INSUNITS`, `$MEASUREMENT`), layouts, viewports and their scales, layer names, text and dimension styles in use, title block and its attributes, revision table, north arrow, external references, plot style table attached to each layout. [practice]
- Work on a copy unless told otherwise, and save in the source file's DWG/DXF version. [practice]

## 2. Minimal-diff editing (the core rule)

- Change only what was requested. No cleanup, renaming, purging, re-layering or restyling "while you are there". Report every assumption, deviation and field you could not fill. [practice]
- Clone formatting from the nearest existing peer object: layer, text style, height, dimension style, block, linetype scale. Never create a style, layer or block when a suitable one exists. [practice]
- Keep colour, linetype and lineweight ByLayer; use per-object overrides only where the drawing already does. [practice]
- Never explode dimensions, blocks, hatches or multiline text (exploding a dimension or hatch destroys its associativity). Never type over a measured dimension value. [practice]
- Edit objects in place (keeps handles, extended data, fields and associativity) instead of deleting and recreating them. [practice]
- Redefining a block changes every insertion; an XREF is another file; model-space content shows in every viewport that looks at that area. Confirm scope across all layouts first. [practice]
- Never move, rotate or rescale existing geometry, the base point, the origin or the UCS to make something fit. Rotate or scale the view (viewport), not the model. [practice]

## 3. Units, coordinates, measurement

- Model space is drawn 1:1 in real-world units; layouts are drawn in sheet units (mm or inch) and plotted 1:1; the scale lives in each viewport. [practice]
- Set `$INSUNITS` explicitly in every deliverable (0 = unitless is a defect) and keep `$MEASUREMENT` consistent with it (metric or imperial linetype and hatch scaling). [practice]
- Blocks or XREFs from a drawing with other units get scaled on insertion: check the size. [practice]
- Never take a dimension by scaling a sheet; measure the model and state the unit. [norm: ISO 128-1:2020 §5 b)]
- Construction drawings are dimensionally accurate and tied to an identified site datum or common coordinate system; keep that link. [norm: ISO 7519:2025 §4.1.3]
- One predominant linear unit per drawing, stated once ("Dimensions in millimetres") and omitted from the values; angles always carry their unit; any other unit is written out. [norm: ISO 129-1:2018 §4.3]
- Decimal marker: ISO specifies a comma; many English-language and US projects use a point. Follow the project. [practice]

## 4. Sheets, frames, title block

| Size | Trimmed sheet (mm) | Drawing space (mm) | Grid fields (long × short) |
|---|---|---|---|
| A0 | 841 × 1189 | 821 × 1159 | 24 × 16 |
| A1 | 594 × 841 | 574 × 811 | 16 × 12 |
| A2 | 420 × 594 | 400 × 564 | 12 × 8 |
| A3 | 297 × 420 | 277 × 390 | 8 × 6 |
| A4 | 210 × 297 | 180 × 277 | 6 × 4 |

[norm: ISO 216:2007; ISO 5457:1999 Tables 1–2]

- Smallest sheet that keeps the drawing clear; no elongated sizes. Borders 20 mm left (filing margin, frame included), 10 mm elsewhere; frame a continuous 0.7 mm line. [norm: ISO 5457 §3.1–3.2, §4.2]
- Four centring marks on the sheet's axes of symmetry (0.7 mm lines from the grid-reference border to about 10 mm past the frame) and trimming marks at the corners. [norm: ISO 5457 §4.3, §4.5]
- Grid reference: 50 mm fields measured from the centring marks; capital letters top to bottom (no I or O), numerals left to right, shown on opposite edges (A4: top and right only); 3.5 mm characters; 0.35 mm grid lines. [norm: ISO 5457 §4.4]
- Title block in the bottom-right corner of the drawing space. A0–A3 landscape only; A4 portrait or landscape (Amendment 1:2010). The drawing reads in the title block's direction. Sheet-size designation in the bottom border, right corner. [norm: ISO 5457 §3.1, §4.1 + Amd 1:2010]
- Title block 180 mm wide (fits A4 inside the 20/10 mm margins), same block on every size. [norm: ISO 7200:2004 §6] Construction sheets may instead use a right-hand text column 100–170 mm wide that contains the title block. [norm: ISO 9431:1990 §5.1]
- Title block data fields. Mandatory: legal owner, identification number (unique within the owner), date of issue, sheet number, title, approval person, creator, document type. Optional: revision index, number of sheets, language code, supplementary title, responsible department, technical reference, classification/keywords, document status, page number, number of pages, paper size. [norm: ISO 7200:2004 §5]
- Revision index: A, B, C … then AA, AB …, or 1, 2, 3 …; avoid I and O. The date of issue changes with every released version. Status words such as "In preparation", "Under approval", "Released", "Withdrawn". [norm: ISO 7200:2004 §5.1.4, §5.1.5, §5.3.8]
- Scale placement: ISO 5455 puts the main scale in the title block; ISO 7200 keeps the title block minimal and shows scale and projection symbol outside it, only when used. Follow the sheet template. [norm: ISO 5455:1979 §4; ISO 7200:2004 §4]
- Construction sheets: figures in rows and columns, main figure top left; a text column on the right holds explanations (symbols, abbreviations, units), instructions, references, a location key plan with north arrow, the revision table and the title block; plan for folding to A4. [norm: ISO 9431 §3–5]
- US sheets: inch sizes A (8.5 × 11 in) up to F plus metric sizes. [norm: ASME Y14.1-2020]
- Title blocks, revision rows and tags are blocks with attributes (or fields): edit attribute values, never overlay loose text. Never fill approval, signature or stamp fields for anyone; leave unknown mandatory fields empty and report them. [practice]

## 5. Scales

- Recommended: 50:1, 20:1, 10:1, 5:1, 2:1, 1:1, 1:2, 1:5, 1:10, 1:20, 1:50, 1:100, 1:200, 1:500, 1:1000, 1:2000, 1:5000, 1:10 000; extend by powers of ten; other ratios only when the function requires. Designation "SCALE 1:50" (the word may be dropped if unambiguous). [norm: ISO 5455 §3, §5.1]
- Main scale in the title block, other scales next to the view or detail label; details too small at the main scale get an enlarged detail view. [norm: ISO 5455 §4, §5.3]
- Every view states its scale and the sheet states the intended paper size. [norm: ISO 7519:2025 §4.1.3]
- Typical building scales: site and location 1:1000–1:200; plans, sections and elevations 1:200–1:50; assemblies and details 1:20–1:1. [practice]
- If prints may be resized, add a graphic scale bar; label reduced prints (e.g. "A1 original printed at A3, 50 %"). [practice]

## 6. Lines

- Widths: 0.13, 0.18, 0.25, 0.35, 0.5, 0.7, 1, 1.4, 2 mm (ratio 1:√2). Narrow : wide : extra-wide = 1 : 2 : 4. Width constant along a line; different widths must stay clearly distinguishable (constant-width output: deviation ≤ ±0.1 d). [norm: ISO 128-2:2022 §5.1–5.2]
- Pattern elements (d = line width): dot ≤ d, gap 3d, short dash 6d, dash 12d, long dash ≈ 24d. Scale linetypes relative to paper so patterns plot the same at every viewport scale. [norm: ISO 128-2 §5.3; practice]
- Keep at least 0.7 mm between parallel lines on paper; dashed and chain lines cross and meet at dashes, not gaps. [norm: ISO 128-2 §6.1–6.2]
- Construction drawings: two or three widths per drawing; elements cut by the section plane heavier than elements seen beyond it; material boundaries in view thin. [norm: ISO 7519:2025 §5.5 and ISO 128-2 Annex B]
- Typical groups (narrow/wide/extra-wide): 0.18/0.35/0.7 for A3–A2 or dense sheets; 0.25/0.5/1.0 for A1–A0. Lineweights come from layers or the plot style table, not per-object values. [practice]
- US: the NCS Plotting Guidelines use the same ISO series of widths. [norm: US NCS]

## 7. Lettering and text

- Nominal heights (capital height, on paper): 1.8, 2.5, 3.5, 5, 7, 10, 14, 20 mm. Stroke ≈ h/10 (type B/CB, vertical preferred) or h/14 (type A/CA); spacing between characters ≈ twice the stroke; upright or sloped 75°. [norm: ISO 3098-1:2015 §4–5]
- Anything a reader must read ≥ 2.5 mm on the sheet; ≥ 3.5 mm when sheets are routinely printed at half size. [practice]
- Typical hierarchy: 2.5 notes and dimensions · 3.5 labels · 5 view titles · 7 drawing number and title. [practice] One text height for all dimensions on a drawing. [norm: ISO 129-1 §4.1.7]
- Model-space text height = paper height × scale denominator (2.5 mm at 1:100 in a millimetre model = 250). Annotative objects store a paper height and show only in viewports whose annotation scale they carry. Paper-space text is drawn at full size. [practice]
- Keep the drawing's language, terms, capitalization and abbreviations; explain symbols and abbreviations in the legend; prefer graphics to words. [norm: ISO 128-1 §5 d)]
- Text reads from the bottom or from the right of the sheet. [norm: ISO 129-1 §4.1.1; practice] Use existing text styles; fonts must exist for every recipient or be embedded/converted in PDFs. [practice]

## 8. Dimensions

- Dimension only what defines the object, each dimension once, in the view that shows it best; group related dimensions; avoid dimensions inside outlines and to hidden lines. [norm: ISO 129-1 §4.1–4.2]
- Values describe the finished state. Out-of-scale values are underlined, information-only values go in parentheses, theoretically exact values in a frame. [norm: ISO 129-1 §4.1.1–4.1.5]
- Keep dimensions associative and on the project dimension style; change the style (height, terminator, units, precision, decimal separator), not individual dimensions. [practice]
- Construction limit deviations: ISO 6284; modular sizes and grids: ISO 8560; levels: ISO 129-1 §7.10. Mechanical tolerancing: ISO GPS series or ASME Y14.5-2018. [norm]

## 9. Layers

- Use the project's layer standard; otherwise extend the naming pattern found in the drawing. No near-duplicates ("Walls", "WALLS_1"). [practice]
- ISO structure: mandatory Agent responsible (2 characters) + Element (6) + Presentation (2); optional Status, Sector, Phase, Projection, Scale, Work package and User-defined fields (widths: see the standard). Fixed field widths for wildcard selection; characters A–Z, 0–9, `-` (all values / no further subdivision) and `_` (unused or undecided); trailing optional fields may be omitted; document any project variant. [norm: ISO 13567-2:2017 §4–7]
- Presentation codes (first character, coarse M model / P page): E element graphics, T text, H hatching, D dimensions, J section/detail marks, K revision marks, G grid, U user (R red lines, C construction lines), B border (F frame lines, O other graphics), V sheet text (W title, N notes), I tables (L legends, S schedules). [norm: ISO 13567-2:2017 §6.3]
- US: `Discipline(1–2)-Major(4)[-Minor(4)[-Minor(4)]][-Status(1)]`, e.g. `A-WALL-FULL-N`; status N new, E existing to remain, D existing to demolish, F future, T temporary, M to be moved, X not in contract, A abandoned, 1–9 phases. [norm: US NCS, AIA CAD Layer Guidelines]
- No content on layer 0 (reserve it for block-definition geometry) or on Defpoints. [practice]
- Viewport frames and helper geometry go on a dedicated non-plotting layer (plot flag off), not on Defpoints. Layers that are off or frozen never plot. [practice]
- New layers appear in existing viewports: freeze them per viewport where they do not belong instead of deleting content. [practice]

## 10. Blocks, symbols, legends

- Repeated symbols are blocks, inserted from the project library or an existing definition. [practice]
- Every sheet with symbols has a legend (in the text column or on a referenced legend drawing); it lists every symbol used on the sheet and nothing else, so add a legend row when you add a new symbol type. [norm: ISO 7519:2025 §4.1.2; practice]
- Domain symbols (safety signs, electrical, piping) follow the standard named in `CAD_CONVENTIONS.md`; pasting from another drawing keeps the target's same-named layers, styles and blocks. [practice]

## 11. Views, orientation, designation

- Keep the project's plan orientation. Show a north arrow on plans and on the location key plan; if a view is rotated to fit, rotate the view, not the model, and show north. [norm: ISO 9431 §5.2.5; practice]
- Views and sections other than the principal view: capital letter at the viewing-direction arrow and the same letter above the view, always upright. [norm: ISO 128-3:2022 §4.1]
- Mechanical drawings show the projection-method symbol. [norm: ISO 5456-2:1996] Buildings, parts and rooms use one consistent designation system. [norm: ISO 4157-1/-2/-3:1998]

## 12. Revisions

- Each release: new revision index, date, description of the change and responsible person in the revision table; the table sits above the title block (same width) or left of it (≥ 100 mm wide). [norm: ISO 9431 §5.2.6; ISO 7200 §5.1.4]
- Mark changed areas with a revision cloud and a tag carrying the revision index, on a revision-marks layer; keep or clear earlier clouds per project rule. Title block revision = last row of the revision table = transmittal revision. [practice]
- Where ISO 19650 applies, status (suitability) and revision are separate metadata, e.g. UK National Annex: S-codes non-contractual, A-codes published; revisions P01… preliminary, C01… contractual. [practice]
- US engineering drawings: revision practice per ASME Y14.35-2025. [norm]

## 13. Layouts, viewports, plotting, PDF

- One sheet per layout, named after the sheet ID; layout media = sheet size; plot scale 1:1, never "fit to paper" for deliverables. [practice]
- Viewports: a standard scale (§5), display locked, annotation scale equal to viewport scale, on the non-plotting viewport layer. [practice]
- Plot with the project's plot style table (CTB by colour, STB named styles; never mixed in one set). [practice]
- PDF: vector output at or above plotter resolution; embed TrueType fonts or convert text to geometry (SHX text always becomes geometry); include layers when asked; avoid shaded 3D visual styles in viewports (they rasterize). [practice]
- Verify each PDF: page size, 1:1 scale (measure a known dimension or the scale bar), no missing XREFs, images or fonts, legible lineweights, correct title block, sheet order. Drawings must stay legible after copying. [norm: ISO 128-1 §5 c); practice]

## 14. Files, references, packaging

- XREFs use relative paths. Deliveries include every dependency: XREFs, images, fonts, plot style tables, plotter configurations; check fonts, linetype and hatch files explicitly, because packaging tools do not always add them. [practice]
- Audit before delivery; purge only unused items, only on delivery copies, only with consent. [practice]
- File names follow the project scheme. ISO 19650-2 with the UK National Annex (2021) uses `Project-Originator-Functional-Spatial-Form-Discipline-Number` with hyphens, revision and status held as metadata, and distinct IDs for exported renditions where the CDE requires it. [practice]
- Hyperlinked references need a durable target or an offline copy. [norm: ISO 7519:2025 §4.1.5] The release procedure and every change after release are documented. [norm: ISO 128-1 §5 e)]

## 15. Never (generic)

- Invent data (dimensions, levels, names, dates, approvals); fill signatures, stamps or approval fields.
- Add personal data, tool or AI attribution, notes-to-self or placeholder text on plotting layers.
- Draw on layer 0 or Defpoints; leave helper geometry on plotting layers; override colours against the standard; rasterize vector content.
- Explode dimensions, blocks or hatches; move, rotate or rescale the model; change units or origin.
- Purge, rename or delete content nobody asked you to touch.

Domain standards signal that a project needs extra conventions (record them in `CAD_CONVENTIONS.md` §16); they are not rules of this file: ISO 23601:2020 escape plans, ISO 7010:2019 safety signs, ISO 3766 reinforcement, ISO 11091 landscape, ISO 129-5 steelwork, ISO 2553 welds, ASME Y14.5 GD&T.

## 16. Editions checked (2026-09)

All standards were consulted against their official editions in the public standards catalogues (ISO, ASME, US NCS). **Before relying on this file in a production workflow, the maintainer must verify all citations against current official editions** to ensure no standards have been withdrawn, superseded or revised in ways that change the cited sections.

| Standard | Edition / status |
|---|---|
| ISO 128-1 | 2020, confirmed 2026 |
| ISO 128-2 | 2022 (replaced 128-2:2020) |
| ISO 128-3 | 2022 (replaced 128-3:2020, 128-43) |
| ISO 129-1 | 2018 + Amd 1:2020; revision started |
| ISO 3098-1 / -2 / -5 | 2015 / 2000 / 1997 |
| ISO 5455 / ISO 5456-2 | 1979 / 1996, both confirmed 2025 |
| ISO 5457 / ISO 216 | 1999 + Amd 1:2010, under review / 2007 |
| ISO 7200 | 2004, confirmed 2025 |
| ISO 9431 | 1990 |
| ISO 7519 | 2025 (third edition) |
| ISO 4157-1 / -2 / -3 | 1998 |
| ISO 13567-1 / -2 | 2017, confirmed 2023 (TR 13567-3 withdrawn 2015) |
| ISO 19650-2 | 2018, revision at DIS stage |
| ASME Y14.1 / Y14.2 / Y14.5 / Y14.35 | 2020 (R2026) / 2014 (R2026) / 2018 (R2024) / 2025 |
| US National CAD Standard | V7 (2025) |
