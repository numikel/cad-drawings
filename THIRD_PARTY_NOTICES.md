# Third-party licenses

This project uses the following open-source and third-party components.

## Required dependencies (always present)

| Component | License | Use |
|---|---|---|
| [ezdxf](https://github.com/mozman/ezdxf) | MIT | Read and analyze DXF files; approximate rendering |
| [pypdfium2](https://github.com/chinapandaman/pypdfium2) | Apache-2.0 / BSD-3-Clause | Convert PDF to PNG (PDFium + support code) |
| [Pillow](https://python-pillow.org/) | HPND-like (permissive) | Image resizing and cropping (direct dependency) |
| [numpy](https://numpy.org/) | BSD-3 | Numeric computation (transitive via ezdxf and matplotlib) |
| [fonttools](https://github.com/fonttools/fonttools) | MIT | Font utilities (transitive via ezdxf and matplotlib) |
| [pyparsing](https://github.com/pyparsing/pyparsing) | MIT | Parsing (transitive via ezdxf and matplotlib) |

## Optional dependencies

| Component | License | Use | Install if… |
|---|---|---|---|
| [pywin32](https://github.com/pywin32) | PSF / BSD | COM automation of the user's own CAD instance (Windows only) | Needed only for COM features; the user must hold a CAD licence |
| [matplotlib](https://matplotlib.org/) | PSF-based (permissive) | Required rasterization backend of the approximate (ezdxf) render | Always |
| [PyMuPDF (fitz)](https://pymupdf.io/) | **AGPL-3.0 OR commercial** | Faster PDF → PNG conversion (than pypdfium2), only with `render --raster pymupdf` | You need speed and accept AGPL terms, OR have a commercial license; **not recommended for distribution** |

## External programs (not bundled)

These are third-party tools that must be installed separately on the operating system.

| Tool | License | Use | Download |
|---|---|---|---|
| [ODA File Converter](https://www.opendesign.com/guestfiles/ODAFileConverter) | Proprietary freeware (no public redistribution rights) | Convert DWG ↔ DXF (highest fidelity) | User downloads directly; script detects and uses it |
| [LibreDWG](https://www.gnu.org/software/libredwg/) | GPL-3.0+ | Convert DWG ↔ DXF (fallback, approximate) | `apt-get install libredwg-tools` (Linux) / `brew install libredwg` (macOS) / Chocolatey (Windows) |
| [Xvfb](https://www.x.org/releases/X11R7.6/doc/man/man1/Xvfb.1.html) | MIT | Virtual X display for ODA File Converter on headless Linux | `apt-get install xvfb` (Linux only) |

## AutoCAD and CAD hosts (user's license)

[AutoCAD](https://www.autodesk.com/products/autocad/overview), [BricsCAD](https://www.bricsys.com/), [ZWCAD](https://www.zwsoft.com/), and [GstarCAD](https://www.gstarsoft.com.cn/) are third-party CAD systems. This skill automates the user's local instance only via COM (Windows). The user must provide their own license and install the software. This skill is not a workaround for licensing requirements.

## License compliance notes

- **Permissive stack (MIT + BSD)**: The required dependencies are fully permissive. This project can be freely used, modified and distributed.
- **PyMuPDF (AGPL)**: Not bundled; imported only when the user installed it and asked for `render --raster pymupdf`. For distribution, either exclude it or accept AGPL terms.
- **Poppler and LibreDWG (GPL)**: External tools, not bundled. Users install them separately if needed. The skill detects and uses them; their use does not trigger GPL on this project (tool invocation, not linking).
- **ODA File Converter**: Proprietary freeware; the publisher prohibits redistribution. Users download directly from the publisher's website.

## Trademark acknowledgements

- **Autodesk, AutoCAD, DWG**: Registered trademarks of Autodesk, Inc. This project is not affiliated with Autodesk.
- **BricsCAD**: Trademark of Bricsys.
- **ZWCAD**: Trademark of ZWSOFT.
- **GstarCAD**: Trademark of GSTARSOFT.

## How to check component versions

```bash
python skills/cad-drawings/scripts/doctor.py
```

This reports installed versions and minimums required.
