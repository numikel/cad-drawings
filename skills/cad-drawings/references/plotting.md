# Plotting sheets for delivery (PDF)

The `plot` command requires the CAD application to run and produces PDF output conforming to the page setup of each layout.

## When to use

- You need a deliverable PDF with correct plot styles, media size, and page orientation.
- The layout is configured in the CAD drawing (page setup, plotter, media) and you want to respect that setup.
- You need multiple layouts plotted to separate PDFs in one run.

## Getting started

Ask the user for consent: the CAD application will start, and the PDF viewer may open when plotting is done.

Get the layout names and page setups from the drawing:

```sh
python scripts/cad.py info <file> --conventions
```

Run the plot command with the user's consent:

```sh
python scripts/cad.py plot <file> --allow-com
```

Check the PDF outputs in the run directory.

## Flags and page setup

| Flag | Default | Effect |
|---|---|---|
| `--layout NAME` | Paper layouts with content | Name a specific layout; repeatable to plot several |
| `--device NAME` | Layout's configured device | Plot device (e.g., 'DWG to PDF.pc3') |
| `--media NAME` | Layout's configured media | Canonical media name the device offers |
| `--area layout\|extents\|display\|window` | Layout's own | Plot area in the layout |
| `--window X1,Y1,X2,Y2` | (none) | Plot window bounds; implies `--area window` |
| `--scale fit\|1:N\|N:1` | Layout's own scale | `fit` fits the drawing to the page; `1:50` plots the drawing at 1:50 scale |
| `--rotate 0\|90\|180\|270` | Layout's own | Rotate the page after plotting |
| `--style-sheet NAME` | (none) | Plot style table name (`.ctb` or `.stb`) known to the CAD |
| `--dest DIR` | (none) | Copy the finished PDFs to this folder |
| `--overwrite` | false | Replace existing files in `--dest` without asking |
| `--timeout SECONDS` | 100 | Soft time limit per layout |

**Device handling:** a layout that already uses a PDF device is plotted as it is. A layout that uses another real device (a DWF plotter, a printer) keeps it: assigning a new device to such a layout resets its plot setup (area, origin, scale), which on a real drawing produced a PDF with one object instead of about 18 000. The built-in PDF device is named only in the plot call, and the report carries a warning that says so. A layout without any device gets the PDF device assigned. An explicit `--device` is always assigned as asked, with the consequences above.

**Page setup fallbacks:** if the layout has no device, the built-in PDF device is used. Media size, scale, rotation, and area fall back to the layout's own settings if not given.

## Output

The command writes one PDF per layout to the run directory with the name `<stem>__<layout>.pdf`. A report `plot.json` lists the layout, device, media, page size (mm), scale, rotation, and warnings for every PDF.

If `--dest` is given, finished PDFs are copied there atomically. A failed layout does not prevent others from plotting (exit 7, partial success).

## Warnings

- **VIEWER_WARNING** is always returned: the PDF plotter may open the user's default PDF viewer on Windows. This is a COM limitation and cannot be switched off. Tell the user.
- **approximate DWG conversion**: if the input is a DWG and had to be converted to DXF first (because no direct DWG backend is available), the layout list may be incomplete.

## Gotchas

- **The PDF plotter must exist on the system.** If it does not, COM tries to use the built-in PDF device (fallback). A missing PDF device is reported as `NO_BACKEND`.
- **Each layout is plotted to a fresh document.** The CAD application closes the document after plotting, so layout-to-layout state is not carried over.
- **Page sizes are in millimetres.** The output verification compares page size with the media within `±0.1 mm` tolerance.
- **Media names are device-specific.** A name that works for one device may not work for another. Use `info --conventions` to see what the drawing knows about the configured device.
- **System variables are reset after the plot.** `BACKGROUNDPLOT` is set to 0 (synchronous plotting) during the command and restored to the original value afterwards.

## Exit codes

- 0: all layouts plotted successfully
- 2: bad arguments (e.g., invalid scale or rotation)
- 3: CAD application not available (missing consent or no CAD on this system)
- 6: no paper layout with content to plot (if no `--layout` is given) or a target already exists in `--dest`
- 7: partial success (some layouts failed; finished PDFs are kept)
