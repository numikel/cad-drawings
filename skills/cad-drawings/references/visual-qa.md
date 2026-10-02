# Visual QA: checking rendered and printed output

Read this when you are about to deliver a changed or newly printed sheet and want to verify it against the drawing and the project's standards.

## Before checking

Before visual inspection, have these ready:
- **The project's CAD_CONVENTIONS.md**: lists expected sheet sizes, text heights, scales, colours and lineweights
- **The original drawing** (for changes, render it side-by-side or diff the fingerprints first to identify what changed)
- **A reference print** (one known-good PDF if this is a replot)
- **Measurement tool**: DPI setting, scale bar, or a known dimension to verify scale

## Viewing and scaling

### Thumbnails and shrinking

Browsing tools (Finder, Windows Explorer, image viewers) create thumbnails that shrink details. Do not rely on thumbnails to spot small text or thin lines; always open the file at 100% zoom (1:1 pixels) or print at the intended scale.

Example: a 2.5 mm text at 300 DPI is ~300 pixels tall on screen, but a thumbnail at 200 pixels wide will shrink it to nearly unreadable.

### DPI and printed text height

Screen DPI varies; printed DPI is fixed. Verify printed size:

**Text height on paper** (mm) = **pixels on screen** ÷ **screen DPI** × **25.4**

Example: Text in a PDF shows 100 pixels tall on a 96 DPI screen.
- Height on paper = 100 ÷ 96 × 25.4 ≈ 26.5 mm

But this is the PDF's DPI, not the screen's. Check the PDF metadata (Properties, Details, `pdfinfo`) for actual DPI.

For a PNG from `render`:
- If created at 150 DPI (default), 1 screen pixel ≈ 1⁄150 inch ≈ 0.17 mm
- Text that is 2.5 mm tall on paper is ~15 pixels tall in a 150-DPI PNG

### Crops and tiles

The `render` command produces full-resolution crops and tiles separate from the main page. Crops and tiles are at the full resolution specified (e.g., 150 DPI); use them for detail inspection. When comparing with a print, measure both at the same DPI and scale.

## Checklist before delivery

### Basic checks (every output)

- [ ] **Page size**: measured or checked in the file properties (PDF: `pdfinfo` or Properties → Details)
- [ ] **Scale**: measure a known dimension or the scale bar with a ruler; verify it matches the drawing's stated scale (e.g., "1:50" should measure as printed)
- [ ] **Title block**: all mandatory fields filled or explicitly confirmed empty with the user (signatures, approvals, stamps left to a person); date and revision index correct
- [ ] **Drawing number and sheet number**: match the file name and the project's naming scheme (§12 in CAD_CONVENTIONS.md)

### Content checks (after changes)

- [ ] **Changed areas only**: compare with the original; mark or crop changed areas; nothing else should differ
- [ ] **No unintended removals**: use `diff` to confirm what was deleted; never claim removal without user confirmation
- [ ] **Text legible**: no text below 2.5 mm on paper; no overlaps (measure with crops if in doubt)
- [ ] **Dimensions and dimensions marks**: associative, on the project dimension style, no typed-over values, scale bar present if needed
- [ ] **Layers**: all required layers present; non-plotting layers not showing; revision clouds on the revision layer

### Visual integrity checks

- [ ] **No clipped content**: margins and frames complete; no text or geometry cut off at edges
- [ ] **Lineweights visible**: thin lines distinct from thick lines (check with a `--max-px` crop if viewing digitally)
- [ ] **Linetype patterns**: dashes, dots and gaps visible at the scale printed (measure: pattern gaps should be at least 0.7 mm)
- [ ] **No broken XREFs**: all external references resolve and show their content; gaps or placeholder boxes indicate missing files
- [ ] **Fonts present**: fonts are embedded in PDF or substituted without visible loss (check PDF properties or print a test)
- [ ] **Colours (if used)**: match the project's plot style table; if mapped to monochrome (grayscale), verify that grayscale values are clearly distinct

### Symbol and annotation checks (project-dependent)

- [ ] **North arrow**: present on all plan views, pointing the same direction, block name correct
- [ ] **Scale**: stated on every view, matches the viewport scale, main scale in the title block
- [ ] **Legend**: present, lists exactly the symbols used on the sheet, none missing or extra
- [ ] **Revision table**: present if revisions exist; format and position correct; clouds match the table index
- [ ] **Grid labels (if used)**: letters and numbers in the right direction, skip I and O, 50 mm field size
- [ ] **Section and detail marks**: arrows point to the correct view, labels and scales are clear

### PDF-specific checks

- [ ] **Vector vs raster**: large areas should be vector (sharp edges), not rasterized (blurry). Check by zooming to 400%; if pixelated, the source was rasterized
- [ ] **Text layer**: in vector PDFs, text is selectable (copy-paste should work); in rasterized, it is not
- [ ] **Transparency**: vector PDFs do not support transparency; shaded 3D objects in viewports are rasterized, breaking the text layer
- [ ] **Embedded fonts**: if fonts are not embedded and the recipient doesn't have them, fallback fonts may look wrong (check PDF properties)

## Assumptions and limitations template

Before delivery, document assumptions about what was not checked or what has known limitations:

```
# Assumptions and limitations for [drawing name]

Verified:
- [x] Page size: A1 (594 × 841 mm)
- [x] Text heights: ≥ 2.5 mm on paper (verified by measurement)
- [x] Changed areas match the edit plan (4 text fields updated, no geometry moved)

Known limitations:
- [ ] Fonts: [Font name] is not embedded; it assumes the recipient has it installed
- [ ] Lineweights: plot style table used; if printed in grayscale, verify line distinctions on the physical printout
- [ ] 3D content: [Any shaded viewports] are approximate; use the 2D plan for exact measurements
- [ ] External references: [List any XREFs]; ensure all are included in the delivery

Checked by: [name], Date: [YYYY-MM-DD]
```

## Common issues and how to spot them

| Symptom | Likely cause | Check |
|---|---|---|
| Text too small to read | Viewport scale is wrong, or text was scaled when it shouldn't be | Measure with a scale bar; check model-space text height vs viewport scale denominator |
| Some text missing or in wrong font | SHX fonts not embedded; fallback was generic | Check PDF properties (Fonts tab); verify SHX fonts are listed as embedded or converted |
| Thin lines barely visible | Plot style table missing or wrong lineweight mapping | Print a crop with thick and thin lines side by side; measure lineweight |
| Geometry slightly offset from text | Layers frozen in one viewport but not another | `info --conventions` to list viewport layers; compare frozen layers per layout |
| Detail looks blurry (pixels visible at 200%) | PNG created at too-low DPI (e.g., 72 DPI instead of 150) | Check the PNG's EXIF DPI metadata, or request a re-render with `--max-px` specified |
| Colours washed out or inverted | Monochrome plot style table applied; missing colors in the CTB | Check the plot style table (CTB) mapping; verify colour policy in CAD_CONVENTIONS.md |
| Crop or tile sizes wrong | Spatial extent was misidentified | Re-run `dump --window` to verify the extent; request a new render with correct coordinates |
| PDF too large (> 10 MB) | Rasterized content or uncompressed images | Check PDF properties (compression, images); request a re-export with compression enabled |
