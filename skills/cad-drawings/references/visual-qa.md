# Visual QA: checking rendered and printed output

Read this when you are about to deliver a changed or newly printed sheet and want to verify it against the drawing and the project's standards.

## Before checking

Before visual inspection, have these ready:
- **The project's CAD_CONVENTIONS.md**: lists expected sheet sizes, text heights, scales, colours and lineweights
- **The original drawing** (for changes, render it side-by-side or diff the fingerprints first to identify what changed)
- **A reference print** (one known-good PDF if this is a replot)
- **Measurement tool**: DPI setting, scale bar, or a known dimension to verify scale

## Mechanical checks first

Run `qa` before looking at pixels: it decides what a script can decide and leaves the rest to you.

```
python scripts/cad.py qa plan.dxf --pdf sheet.pdf --layout "Sheet-A" --require "Rev. C" --forbid DRAFT
```

To check whether text changed after editing, provide a baseline drawing:

```
python scripts/cad.py qa plan.dxf --baseline plan_old.dxf --layout "Sheet-A"
```

This reports `TEXT_GREW` (text bounding box grew by > 10%) and `TEXT_WRAPPED` (MTEXT now has more lines) for entities with matching handles.

**On the drawing:**
- unset `$INSUNITS`, a layout that holds only viewports, a viewport without a usable scale, an external reference that is missing on disk
- frame missing or lying outside the printable area (new check: `FRAME_NOT_FOUND`, `FRAME_OUTSIDE_PAPER`)
- text outside the detected frame (new check: `TEXT_OUTSIDE_FRAME`)
- text changed size or wrapped after editing (with `--baseline`; new checks: `TEXT_GREW`, `TEXT_WRAPPED`)

**On the PDF:**
- it opens, page count, page size against the layout's paper size (either orientation, small tolerance), almost no content on the page, text that must or must not appear
- frame clipped (new error: `PDF_CLIPPED`) or shifted from expected position (new warning: `PDF_SHIFTED`)

**Findings have a severity.** Only errors end the run with exit 7; warnings and info do not, so read `findings.json` either way.

**About text searches:** required and forbidden text is searched in the PDF's text layer. Text drawn with SHX fonts, or plotted as geometry, is not text there; `qa` then reports that it could not check, it does not report the text as missing.

**What the checks cover and don't:** the checks now detect cut-off frames and whether a frame moved on the PDF after plotting (rotation, scale, or physical shift of the plot). Collisions between objects, whether the right content is on the sheet, and aesthetic judgement stay with the checklist below.

## Finding meanings and remedies

| Finding ID | Severity | Meaning | What to do |
|---|---|---|---|
| `FRAME_NOT_FOUND` | info | No clear rectangular frame found on the layout. | This is normal if the sheet has no printed frame border. If you expect a frame, check layer names with `dump --type LWPOLYLINE --type POLYLINE`, or use `--frame-layer` to specify a layer. |
| `FRAME_OUTSIDE_PAPER` | warning | The detected frame extends beyond the printable area. | Check the page setup (margins, orientation). If margins are asymmetric (e.g., 20/5/5/5 mm on left/right/top/bottom), the frame may intentionally overflow. Verify with the user. |
| `FRAME_CHECK_SKIPPED` | info | Frame position check cannot run because the plot is rotated, scaled, or uses non-standard page setup. | This is expected for rotated and scaled-to-fit plots. Verify the plot visually; the checks do not apply. |
| `TEXT_OUTSIDE_FRAME` | warning | Text bounding box is more than 10% outside the detected frame. The message says "estimate" because text metrics vary by font and platform. | Check visually that the text is where you expect (inside the frame or intentionally outside). If the overhang is < 10%, it is ignored and is not an error. |
| `TEXT_GREW` | warning | Text bounding box grew by > 10% after editing (with `--baseline`). | Check that the text still fits on the sheet. Render both drawings to compare side-by-side. If intentional (e.g., larger font), no action needed. |
| `TEXT_WRAPPED` | warning | MTEXT now wraps to more lines than in the baseline. | Check that lines fit on the sheet and don't overlap other content. Render both to compare. Adjust text content or width if needed. |
| `BASELINE_MISMATCH` | info | Fewer than 50% of texts in the current drawing have a matching handle in the baseline. | This is normal when many texts have been added or deleted. Baseline checks become unreliable below 50% match; read `TEXT_GREW` and `TEXT_WRAPPED` as informational only, not a complete status. |
| `PDF_CLIPPED` | error | Ink touches the PDF's page edge (within 0.5 mm) along at least 5 mm of that edge, or a stroke starting at the edge runs at least 50 mm into the page. | The plot lost content at the edge. Check: (1) frame position in the drawing (use `dump --window`), (2) page setup margins and orientation, (3) plotter clipping offset. Re-plot with adjusted margins. |
| `PDF_EDGE_MARKS` | info | A little ink touches the page edge (under 5 mm along the edge, no long stroke), typically corner or registration marks running off the page. | The sheet itself is not cut. Look at the render if the marks matter; nothing to fix otherwise. |
| `PDF_SHIFTED` | warning | The detected frame on the PDF is offset by > 2 mm from the expected position. | This indicates the plot moved on the sheet (e.g., plotter offset, page setup mismatch). Check: (1) page setup orientation and scale, (2) plotter margins and offsets, (3) whether `plot_type` is 5 (layout plot to the layout extent), scale is 1:1, and centering is off. All three conditions must hold or the check is skipped. Re-plot if the shift is significant. |
| `PDF_TRIM_OUTLINE` | info | A line at the page edge (likely the page outline or margin rule) has been detected and trimmed from content checks. | This is normal and expected; the line is not considered content. |
| `PDF_UNCHECKED` | info | The PDF was not checked for clipping or shift (pypdfium2 missing, or the PDF could not be rasterized). | Install pypdfium2 (`pip install pypdfium2`) to enable PDF checks. Some PDF variants (e.g., very large files, complex rendering) may fail to rasterize; these can be checked visually instead. |

**Note on finding limits:** when a single finding ID appears more than 50 times, the findings list in the JSON is truncated and marked with `"truncated": true`; the summary then contains `"not_listed": N` (count of truncated findings). This prevents overly large JSON summaries. Inspect `findings.json` in the run directory for the complete list.

**Note on PDF_CLIPPED:** the check looks at the rasterised page, so backgrounds and clip paths do not count, and short marks at the edge are reported as `PDF_EDGE_MARKS` (info), not as an error. It can still fire on a full-page scan, a background image or a long line that runs to the edge on purpose (a format outline on all four edges is handled as `PDF_TRIM_OUTLINE`). Assess the render: if the content at the edge is intentional, the error is a false positive for that file; there is no flag to change the thresholds.

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
| Frame appears cut off at the page edge | Margins too narrow, or frame sitting exactly at the edge | Check page setup in the drawing (left/right/top/bottom margins); if tight, add 2–5 mm breathing room and re-plot |
| Text wraps onto more lines after editing | MTEXT box width is fixed, content grew | Check the MTEXT `width` setting; increase it or split long text across multiple MTEXT entities |
| Plot shifted on the page by 3–5 mm | Page setup inherited from a previous plot with different plotter offset | Check: (1) the drawing's page setup (Devices tab in CAD), (2) plotter configuration in the page setup (origin offset, scale). Reset if unclear and re-plot. |
| Page outline (thin line at margins) visible on PDF | Plotter drew the page outline as a line | This is harmless. The QA check `PDF_TRIM_OUTLINE` detects and ignores it. Remove it from the plot setup if it should not appear. |
