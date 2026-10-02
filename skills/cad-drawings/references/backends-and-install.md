# Backends and install: how to get the components you need

Read this when `doctor` reports missing components and you need to know what each one does, how to install it, or how it affects what the skill can do. This file lists install commands per OS, what each component enables, and license considerations.

## Quick reference: what you need for what

| Capability | Windows | macOS / Linux |
|---|---|---|
| Read DXF | ezdxf (already installed) | ezdxf (already installed) |
| Read DWG | AutoCAD via COM, ODA, or LibreDWG | ODA or LibreDWG |
| Render to PNG | pypdfium2 (already installed) | pypdfium2 (already installed) |
| Plot native (COM) | AutoCAD or BricsCAD | — |
| Edit DWG (native) | AutoCAD or BricsCAD | — |
| Convert DWG ↔ DXF | AutoCAD, ODA, or LibreDWG | ODA or LibreDWG |
| Convert DWG → PDF | AutoCAD or ODA | ODA |

## Python packages (via pip or uv)

### Always present (required dependencies)

**ezdxf 1.4.4+**
- What: Read and query DXF and DWG files; render to PNG without CAD
- Install: `pip install ezdxf>=1.4.4` (included in the skill environment)
- License: MIT
- Minimum: 1.4.4 (check: `python -c "import ezdxf; print(ezdxf.__version__)"`)

**pypdfium2 5.0+**
- What: Convert PDF to PNG for viewing (default backend, fastest)
- Install: `pip install pypdfium2>=5` (included in the skill environment)
- License: Apache-2.0 / BSD-3-Clause (combines PDFium under Apache 2.0 and other BSD code)
- Optional: Lazily imported; if not installed, falls back to PyMuPDF or Poppler

**Pillow (via ezdxf)**
- What: Image processing; a transitive dependency of ezdxf
- License: HPND-like (permissive)

**numpy, fonttools, pyparsing (via ezdxf)**
- What: Supporting libraries for DXF geometry and rendering
- License: BSD / MIT (permissive)

### Windows only

**pywin32 312+**
- What: COM automation (required for AutoCAD, BricsCAD, ZWCAD, GstarCAD on Windows)
- Install: `pip install pywin32>=312` (included in the skill environment)
- License: PSF / BSD
- Check: `python -c "import win32com; print(win32com.__version__)"`
- Version 312 fixed a memory leak in variant transfers; use 312 or newer

### Optional (loaded only if present)

**PyMuPDF (fitz) — AGPL-3.0**
- What: Faster PDF to PNG conversion (faster than pypdfium2, but AGPL)
- Install: `pip install PyMuPDF` (not recommended for distribution)
- License: **AGPL-3.0** (copyleft) OR commercial license from Artifex
- When: Falls back to this only if pypdfium2 is not available and user asks
- Bundled caveat: If present and imported, ezdxf prints an "AGPL" warning to stdout, breaking JSON output. The skill detects this and avoids importing PyMuPDF except on user request.

### Deprecated or not recommended

**pdf2image + Poppler (GPL)**
- What: PDF to PNG conversion (slowest, GPL-licensed external tool)
- When: Only if both pypdfium2 and PyMuPDF are unavailable and Poppler is installed
- License: GPL (Poppler)
- Caveat: Requires external binary; not bundled

## External programs

### ODA File Converter

**What:** Convert DWG ↔ DXF (or both to DWG), preferred backend after AutoCAD.

**Quality:** Highest fidelity for conversion, lowest risk of data loss.

**Install per OS:**

**Windows:**
```powershell
# Download from https://www.opendesign.com/guestfiles/ODAFileConverter
# Extract to C:\Program Files\ODA\ODA File Converter 27.1.0\
# Check: dir "C:\Program Files\ODA\*\ODAFileConverter.exe"
```

**macOS:**
```bash
# Download and install DMG from https://www.opendesign.com/guestfiles/ODAFileConverter
# Opens a GUI window; Finder will show the installed location
# Typical: /Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter
```

**Linux (Ubuntu/Debian):**
```bash
# Ubuntu 22.04 / 24.04
sudo apt-get install -y libqt5core5a libqt5gui5 libqt5widgets5 libxcb-xinput0
# Download from https://www.opendesign.com/guestfiles/ODAFileConverter (Linux version)
tar xzf ODAFileConverter_27.1.0_lnx.tar.gz
sudo cp -r ODAFileConverter /usr/local/bin/

# Also install Xvfb (virtual X server) for headless operation
sudo apt-get install -y xvfb
```

**License:** Freeware (proprietary, no public EULA); redistribution terms unclear, so not bundled. Users download directly.

**macOS caveat:** Launches a GUI window, cannot run fully headless.

**Linux caveat:** Requires Xvfb (virtual X display) to run without a display server.

### LibreDWG

**What:** Convert DWG ↔ DXF; handles all public DWG versions (approximate results for R2010+).

**Quality:** Good for older DWG files; R2010+ objects may be dropped. Always a fallback after ODA.

**Install per OS:**

**Windows:**
```powershell
# Using Chocolatey (if installed)
choco install libredwg

# Or download from https://github.com/LibreDWG/libredwg/releases
# Extract and add dwg2dxf / dxf2dwg to PATH
```

**macOS:**
```bash
# Using Homebrew
brew install libredwg

# Check
which dwg2dxf dxf2dwg
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get install -y libredwg-tools

# Check
which dwg2dxf dxf2dwg
```

**License:** GPL-3.0+ (copyleft). The tools are free software; distribution is allowed, but derived works must remain open source.

**Caveat:** Use with caution; file a test conversion with a non-critical drawing first. If the result is incorrect, fall back to ODA or COM.

### AutoCAD, BricsCAD, ZWCAD, GstarCAD (CAD hosts)

**What:** Native DWG editing, plotting, conversion via COM.

**License:** Each requires a license held by the user. The skill automates the user's local instance only; never as a service.

**Install:** Download from the respective vendor's website. The skill detects installed versions via the Windows registry (AutoCAD, BricsCAD) or ProgID lookup.

**CAD hosts detected by the skill (via registry on Windows):**
- AutoCAD: `AutoCAD.Application[.NN[.N]]` (version-specific, e.g., `24.3`)
- BricsCAD: `BricscadApp.AcadApplication`
- ZWCAD: `ZWCAD.Application`
- GstarCAD: `GStarCAD.Application[.NN]`

**macOS / Linux:** CAD hosts are not available via COM on these platforms.

### Xvfb (Linux only)

**What:** Virtual X server for running GUI programs (like ODA File Converter) without a display.

**Install:**
```bash
sudo apt-get install -y xvfb
```

**When needed:** If ODA File Converter is installed on Linux and no DISPLAY environment variable is set.

## Component decision tree

The `doctor` command checks all components and builds this decision tree for you:

1. **Can I read DXF?** ✓ Always (ezdxf)
2. **Can I read DWG?** Check AutoCAD (Windows COM) → ODA → LibreDWG
3. **Can I render to PNG?** ✓ Always (pypdfium2, PyMuPDF, Poppler)
4. **Can I plot native (PDF with exact colors/fonts)?** Check AutoCAD / BricsCAD (Windows COM) → refuse
5. **Can I edit DWG in place?** Check AutoCAD / BricsCAD (Windows COM) → suggest edit via DXF
6. **Can I convert DWG ↔ DXF?** Check AutoCAD (Windows COM) → ODA → LibreDWG → refuse

If a step refuses, the next step is tried. If all steps refuse, the command exits with MISSING_DEPENDENCY and suggests install options.

## Version and compatibility notes

| Component | Minimum | Tested | Notes |
|---|---|---|---|
| Python | 3.10 | 3.13 | Type hints, modern standard library |
| ezdxf | 1.4.4 | 1.4.4+ | Do not use older versions; APIs changed |
| pypdfium2 | 5.0 | 5.0+ | Wheel includes all dependencies |
| pywin32 | 312 | 312+ | 311 and earlier have memory leak |
| ODA File Converter | 27.0 | 27.1 | Check for newer releases |
| LibreDWG | 0.13 | 0.14 | Use 0.14+ for 11 CVE fixes |
| AutoCAD | 2020 | 2024 | Older versions may lack some APIs |
| BricsCAD | V20 | V24 | Newer versions have better compatibility |

## Troubleshooting install issues

**macOS: ODA File Converter app not found**
```bash
# After downloading the DMG, mount it and copy to /Applications
hdiutil attach ODAFileConverter_27.1.0.dmg
cp -r /Volumes/ODAFileConverter/ODAFileConverter.app /Applications/
hdiutil detach /Volumes/ODAFileConverter
# Check
/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter --help
```

**Linux: LibreDWG dwg2dxf not found after apt-get install**
```bash
# Add the package repository if needed
sudo add-apt-repository -y ppa:libredwg-team/ppa
sudo apt-get update
sudo apt-get install -y libredwg-tools
```

**Windows: pywin32 fails to import**
```powershell
# Rebuild COM cache
python -m pip install --force-reinstall pywin32
python -m pywin32_postinstall -install
```

**All platforms: Check component versions**
```bash
python scripts/doctor.py
```

The `doctor` command reports versions of all installed components and suggests next steps for anything missing.
