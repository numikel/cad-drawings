#!/usr/bin/env python3
"""Environment check for the cad-drawings skill. Standard library only; installs nothing.

    python scripts/doctor.py [--probe-com]

Prints one JSON document that follows ``assets/output.schema.json`` (command ``doctor``). The
interesting part is ``summary.capabilities``: for each capability a ``status`` (available |
degraded | missing | not_implemented), the ``via`` backend, and for missing or degraded ones an
``install`` list of commands or download pointers for the current operating system. Capabilities
describe what the shipped commands can do: read_dxf, read_dwg, convert (dwg<->dxf), render and
pdf_to_png. ``edit_dwg`` and ``plot_deliverable`` are reported as ``not_implemented`` (no command
yet; with a CAD host present, custom code can use ``cadlib.acad``).

Exit code: 0 with the matrix, even when capabilities are missing (the matrix is the answer).
Exit 3 only when the Python interpreter itself is older than 3.10. Exit 2 for bad arguments.

Detection is read-only: registry reads (``winreg``), ``tasklist`` / ``pgrep``, ``PATH`` and
well-known install folders, ``importlib.metadata``. No CAD application is started unless
``--probe-com`` is given; that flag starts one private CAD instance (it may use a licence), so
callers pass it only after the user agreed.

Environment overrides: ``CAD_DRAWINGS_ODA`` (full path of ``ODAFileConverter``).

The helpers (``find_oda``, ``find_libredwg``, ``detect_cad_hosts``, ``install_options``) are
reused by ``cadlib.convert``; keep this file importable without any third-party package.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import importlib.metadata as importlib_metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

MIN_PYTHON = (3, 10)
MIN_VERSIONS = {"ezdxf": "1.4.4", "pywin32": "312", "pypdfium2": "5"}
# distribution name -> (required for the core skill, only meaningful on Windows)
PACKAGES: dict[str, tuple[bool, bool]] = {
    "ezdxf": (True, False),
    "pypdfium2": (True, False),
    "pywin32": (False, True),
    "pillow": (False, False),
    "matplotlib": (False, False),
    "pymupdf": (False, False),
    "jsonschema": (False, False),
}
ODA_DOWNLOAD = "https://www.opendesign.com/guestfiles/oda_file_converter"
LIBREDWG_HOME = "https://www.gnu.org/software/libredwg/"
CAD_PROCESS_NAMES = ("acad.exe", "bricscad.exe", "zwcad.exe", "gcad.exe")
CAD_PGREP_NAMES = ("acad", "bricscad", "zwcad", "gcad")
# product, ProgID pattern (read-only registry lookup; AutoCAD LT has no COM, nothing to detect)
PROGID_PATTERNS: tuple[tuple[str, str], ...] = (
    ("AutoCAD", r"^AutoCAD\.Application(\.\d+(\.\d+)?)?$"),
    ("BricsCAD", r"^BricscadApp\.AcadApplication$"),
    ("ZWCAD", r"^ZWCAD\.Application$"),
    ("GstarCAD", r"^GStarCAD\.Application(\.\d+)?$"),
)
STATUS_AVAILABLE = "available"
STATUS_DEGRADED = "degraded"
STATUS_MISSING = "missing"
STATUS_NOT_IMPLEMENTED = "not_implemented"
NO_COMMAND_NOTE = (
    "no command yet; with a CAD host present, custom code can use cadlib.acad "
    "(see references/com-automation.md)"
)
SUBPROCESS_TIMEOUT_S = 20


# --------------------------------------------------------------------------------------
# versions and packages
# --------------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    match = re.match(r"\d+(?:\.\d+)*", text.strip())
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def version_at_least(found: str, minimum: str) -> bool:
    a, b = parse_version(found), parse_version(minimum)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


def installed_packages() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            out[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            out[name] = None
    return out


def package_state(name: str, version: str | None) -> str:
    """'missing', 'old' (below the minimum) or 'ok'."""
    if version is None:
        return "missing"
    minimum = MIN_VERSIONS.get(name)
    if minimum and not version_at_least(version, minimum):
        return "old"
    return "ok"


# --------------------------------------------------------------------------------------
# install guidance
# --------------------------------------------------------------------------------------


def _platform_key(platform: str | None = None) -> str:
    plat = platform or sys.platform
    if plat.startswith("win"):
        return "win32"
    if plat == "darwin":
        return "darwin"
    return "linux"


def install_options(platform: str | None = None) -> dict[str, list[str]]:
    """Ready commands (or download pointers) per component, for one operating system."""
    key = _platform_key(platform)
    py = "python" if key == "win32" else "python3"
    pip = f"{py} -m pip install"
    oda = {
        "win32": [f"Download and run the ODA File Converter installer: {ODA_DOWNLOAD}"],
        "darwin": [f"Download and install the ODA File Converter (.dmg): {ODA_DOWNLOAD}"],
        "linux": [
            f"Download the ODA File Converter (.deb, .rpm or AppImage): {ODA_DOWNLOAD}",
            "sudo apt install xvfb  # needed to run it without a display",
        ],
    }[key]
    libredwg = {
        "win32": [f"Get a LibreDWG build ({LIBREDWG_HOME}) and put dwg2dxf.exe on PATH"],
        "darwin": ["brew install libredwg"],
        "linux": ["sudo apt install libredwg-tools"],
    }[key]
    cad = {
        "win32": ["Install AutoCAD or a COM-compatible CAD (BricsCAD, ZWCAD, GstarCAD)"],
        "darwin": ["AutoCAD for Mac has no COM interface; use ODA File Converter instead"],
        "linux": ["No COM on Linux; use ODA File Converter instead"],
    }[key]
    return {
        "ezdxf": [f'{pip} "ezdxf>={MIN_VERSIONS["ezdxf"]}"'],
        "pypdfium2": [f'{pip} "pypdfium2>={MIN_VERSIONS["pypdfium2"]}"'],
        "pywin32": [f'{pip} "pywin32>={MIN_VERSIONS["pywin32"]}"'],
        "drawing": [f"{pip} matplotlib pillow"],
        "oda": oda,
        "libredwg": libredwg,
        "cad": cad,
    }


def converter_install_hint(platform: str | None = None) -> str:
    """One paragraph for NO_BACKEND errors: how to get a DWG converter on this OS."""
    opts = install_options(platform)
    parts = [
        f"{i}. {line}"
        for i, line in enumerate(opts["cad"][:1] + opts["oda"][:1] + opts["libredwg"][:1], 1)
    ]
    return "no DWG converter found; options: " + " ".join(parts)


# --------------------------------------------------------------------------------------
# converters on disk
# --------------------------------------------------------------------------------------


def _isfile(path: str) -> bool:
    return os.path.isfile(path)


def find_oda(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    program_dirs: Sequence[str | Path] | None = None,
    environ: dict[str, str] | None = None,
    home: str | Path | None = None,
) -> str | None:
    """Path of ``ODAFileConverter`` or None.

    Order: ``CAD_DRAWINGS_ODA``, ``PATH``, then well-known install locations. On Windows the
    installer puts it in a versioned folder (``ODA\\ODA File Converter <version>``), so the folder
    is globbed and the highest version wins.
    """
    env = os.environ if environ is None else environ
    which_fn = which or shutil.which
    key = _platform_key(platform)
    override = env.get("CAD_DRAWINGS_ODA")
    if override and _isfile(override):
        return override
    on_path = which_fn("ODAFileConverter")
    if on_path:
        return on_path
    candidates: list[str] = []
    if key == "win32":
        roots = program_dirs
        if roots is None:
            roots = [
                value
                for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
                if (value := env.get(var))
            ] or [r"C:\Program Files"]
        found: list[str] = []
        for root in roots:
            found.extend(
                glob.glob(os.path.join(glob.escape(str(root)), "ODA", "*", "ODAFileConverter.exe"))
            )
        found.sort(
            key=lambda p: parse_version(re.sub(r"^\D+", "", Path(p).parent.name)), reverse=True
        )
        candidates = found
    elif key == "darwin":
        candidates = ["/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"]
    else:
        base = str(home) if home is not None else os.path.expanduser("~")
        candidates = ["/usr/bin/ODAFileConverter", "/usr/local/bin/ODAFileConverter"]
        candidates += sorted(
            glob.glob(os.path.join(glob.escape(base), "Apps", "ODAFileConverter*.AppImage"))
        )
        candidates += sorted(glob.glob("/opt/ODAFileConverter*/ODAFileConverter"))
    return next((c for c in candidates if _isfile(c)), None)


def find_libredwg(*, which: Callable[[str], str | None] | None = None) -> dict[str, str]:
    """``{"dwg2dxf": path, "dxf2dwg": path}`` for the LibreDWG tools that are on PATH."""
    which_fn = which or shutil.which
    return {tool: p for tool in ("dwg2dxf", "dxf2dwg") if (p := which_fn(tool))}


def has_display(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


# --------------------------------------------------------------------------------------
# CAD hosts (read-only)
# --------------------------------------------------------------------------------------


def _reg_subkeys(winreg: Any, root: Any, subkey: str, view: int = 0) -> list[str]:
    names: list[str] = []
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        pass
    return names


def _reg_default(winreg: Any, root: Any, subkey: str, view: int = 0) -> str | None:
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            return str(winreg.QueryValueEx(key, "")[0])
    except OSError:
        return None


def _reg_value(winreg: Any, root: Any, subkey: str, name: str, view: int = 0) -> str | None:
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return os.path.expandvars(value) if isinstance(value, str) else None
    except OSError:
        return None


def _exe_from_command(command: str | None) -> str | None:
    if not command:
        return None
    command = command.strip()
    if command.startswith('"'):
        return command.split('"')[1]
    match = re.match(r"(.+?\.exe)", command, re.IGNORECASE)
    return match.group(1) if match else command


def _progid_sort_key(progid: str) -> tuple[int, ...]:
    return parse_version(progid.split(".", 2)[2]) if progid.count(".") >= 2 else ()


def detect_cad_hosts() -> list[dict[str, Any]]:
    """COM-capable CAD hosts registered on this machine, newest first (Windows only).

    Reads ``HKEY_CLASSES_ROOT`` (ProgID -> CLSID -> LocalServer32). Creates no COM object.
    """
    if sys.platform != "win32":
        return []
    import winreg

    hkcr = winreg.HKEY_CLASSES_ROOT
    names = _reg_subkeys(winreg, hkcr, "")
    hosts: list[dict[str, Any]] = []
    for product, pattern in PROGID_PATTERNS:
        regex = re.compile(pattern)
        for progid in sorted(
            (n for n in names if regex.match(n)), key=_progid_sort_key, reverse=True
        ):
            clsid = _reg_default(winreg, hkcr, rf"{progid}\CLSID")
            command = _reg_default(winreg, hkcr, rf"CLSID\{clsid}\LocalServer32") if clsid else None
            exe = _exe_from_command(command)
            hosts.append(
                {
                    "product": product,
                    "progid": progid,
                    "exe": exe,
                    "exe_exists": bool(exe and os.path.exists(exe)),
                }
            )
    return hosts


def detect_autocad_installs() -> list[dict[str, str]]:
    """AutoCAD installs from the HKLM Autodesk registry key as product/version/key (Windows)."""
    if sys.platform != "win32":
        return []
    import winreg

    view = winreg.KEY_WOW64_64KEY
    base = r"SOFTWARE\Autodesk\AutoCAD"
    out: list[dict[str, str]] = []
    for release in _reg_subkeys(winreg, winreg.HKEY_LOCAL_MACHINE, base, view):
        for product in _reg_subkeys(winreg, winreg.HKEY_LOCAL_MACHINE, rf"{base}\{release}", view):
            key = rf"{base}\{release}\{product}"
            if _reg_value(winreg, winreg.HKEY_LOCAL_MACHINE, key, "AcadLocation", view):
                name = _reg_value(winreg, winreg.HKEY_LOCAL_MACHINE, key, "ProductName", view)
                out.append(
                    {"product": (name or "AutoCAD")[:40], "version": release, "key": product}
                )
    return out


def running_cad_processes() -> list[str]:
    """Names of CAD processes that are running (``tasklist`` / ``pgrep``; read-only)."""
    running: list[str] = []
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SUBPROCESS_TIMEOUT_S,
                check=False,
            )
            listing = proc.stdout.lower()
            running = [n for n in CAD_PROCESS_NAMES if f'"{n}"' in listing]
        else:
            for name in CAD_PGREP_NAMES:
                found = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True,
                    timeout=SUBPROCESS_TIMEOUT_S,
                    check=False,
                )
                if found.returncode == 0:
                    running.append(name)
    except (OSError, subprocess.SubprocessError):
        pass
    return running


def probe_com_instance(starter: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Start one private CAD instance through ``cadlib.acad``, report, and quit it.

    Only called for ``--probe-com``. ``starter`` returns a context manager yielding a session
    (injected by tests; the default uses ``cadlib.acad.AcadSession.start``).
    """
    if starter is None:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        try:
            from cadlib.acad import AcadSession
        except ImportError:
            return {"started": False, "error": "acad module missing"}
        starter = AcadSession.start
    try:
        with starter() as session:
            return {
                "started": True,
                "pid": getattr(session, "pid", None),
                "version": getattr(session, "version", None),
            }
    except Exception as exc:  # noqa: BLE001 - any COM failure is a result, not a crash
        return {"started": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


# --------------------------------------------------------------------------------------
# environment snapshot and assessment
# --------------------------------------------------------------------------------------


@dataclass
class Environment:
    platform: str
    python: tuple[int, int, int]
    packages: dict[str, str | None]
    oda: str | None = None
    libredwg: dict[str, str] = field(default_factory=dict)
    hosts: list[dict[str, Any]] = field(default_factory=list)
    installs: list[dict[str, str]] = field(default_factory=list)
    running: list[str] = field(default_factory=list)
    free_gb: float | None = None
    xvfb: str | None = None
    display: bool = True
    com_probe: dict[str, Any] | None = None


def gather_environment(*, probe_com: bool = False) -> Environment:
    key = _platform_key()
    free: float | None = None
    with contextlib.suppress(OSError):
        free = round(shutil.disk_usage(tempfile.gettempdir()).free / 1024**3, 1)
    env = Environment(
        platform=key,
        python=(sys.version_info[0], sys.version_info[1], sys.version_info[2]),
        packages=installed_packages(),
        oda=find_oda(),
        libredwg=find_libredwg(),
        hosts=detect_cad_hosts(),
        installs=detect_autocad_installs(),
        running=running_cad_processes(),
        free_gb=free,
        xvfb=(shutil.which("Xvfb") or shutil.which("xvfb-run")) if key == "linux" else None,
        display=has_display() if key == "linux" else True,
    )
    if probe_com:
        env.com_probe = probe_com_instance()
    return env


def _cap(
    status: str, via: str | None, install: list[str] | None = None, note: str | None = None
) -> dict[str, Any]:
    cap: dict[str, Any] = {"status": status, "via": via}
    if install:
        cap["install"] = install[:2]
    if note:
        cap["note"] = note
    return cap


def assess(env: Environment) -> tuple[dict[str, Any], list[str]]:
    """Build ``summary`` and warnings from an environment snapshot (pure, no I/O)."""
    opts = install_options(env.platform)
    pk = env.packages
    ezdxf_state = package_state("ezdxf", pk.get("ezdxf"))
    pdfium_state = package_state("pypdfium2", pk.get("pypdfium2"))
    pywin_state = package_state("pywin32", pk.get("pywin32"))
    has_draw = bool(pk.get("matplotlib") or pk.get("pillow"))
    host = next((h for h in env.hosts if h.get("exe_exists")), None)
    com_host = bool(host) and env.platform == "win32"
    com_ready = com_host and pywin_state == "ok"
    oda_ok = bool(env.oda) and (env.platform != "linux" or bool(env.xvfb) or env.display)
    warnings: list[str] = []

    ez_install = opts["ezdxf"] if ezdxf_state != "ok" else []
    caps: dict[str, Any] = {}
    caps["read_dxf"] = (
        _cap(STATUS_AVAILABLE, "ezdxf")
        if ezdxf_state == "ok"
        else _cap(
            STATUS_DEGRADED if ezdxf_state == "old" else STATUS_MISSING,
            "ezdxf",
            ez_install,
            f"ezdxf {pk.get('ezdxf')} is below {MIN_VERSIONS['ezdxf']}"
            if ezdxf_state == "old"
            else None,
        )
    )

    # DWG -> DXF conversion: COM, then ODA, then LibreDWG
    if ezdxf_state != "ok":
        caps["read_dwg"] = _cap(
            STATUS_MISSING, None, ez_install, "needs ezdxf to read the converted DXF"
        )
    elif com_ready:
        caps["read_dwg"] = _cap(
            STATUS_AVAILABLE, "com", note="starts a private CAD instance; ask the user first"
        )
    elif oda_ok:
        caps["read_dwg"] = _cap(STATUS_AVAILABLE, "oda")
    elif env.libredwg.get("dwg2dxf"):
        caps["read_dwg"] = _cap(
            STATUS_DEGRADED, "libredwg", opts["oda"], "approximate: some objects may be dropped"
        )
    else:
        install = (opts["cad"] if env.platform == "win32" else []) + opts["oda"] + opts["libredwg"]
        note = "ODA File Converter needs a display (Xvfb)" if env.oda and not oda_ok else None
        caps["read_dwg"] = _cap(STATUS_MISSING, None, install, note)
    if com_host and pywin_state != "ok":
        warnings.append(f"CAD host registered but pywin32 is {pywin_state}; COM is unavailable")
        caps["read_dwg"].setdefault("install", opts["pywin32"])

    # render for viewing
    if com_ready and pdfium_state == "ok":
        caps["render"] = _cap(STATUS_AVAILABLE, "com+pypdfium2")
    elif ezdxf_state == "ok" and has_draw:
        install = opts["pypdfium2"] if pdfium_state != "ok" else []
        caps["render"] = _cap(
            STATUS_DEGRADED, "ezdxf", install, "approximate: no plot styles, substitute fonts"
        )
    else:
        caps["render"] = _cap(
            STATUS_MISSING, None, ez_install + opts["drawing"] + opts["pypdfium2"]
        )

    # DWG <-> DXF conversion: both directions need a backend (COM writes DXF only)
    reader = dict(caps["read_dwg"])
    writer_ok = oda_ok or bool(env.libredwg.get("dxf2dwg"))
    if reader["status"] == STATUS_AVAILABLE and not writer_ok:
        reader["status"] = STATUS_DEGRADED
        reader["note"] = "dwg->dxf only; dxf->dwg needs ODA File Converter or LibreDWG"
        reader.setdefault("install", opts["oda"][:1])
    elif reader["status"] != STATUS_MISSING and writer_ok and reader.get("via") == "libredwg":
        reader["note"] = "approximate: some objects may be dropped; dxf->dwg writes r2004 only"
    caps["convert"] = reader

    # F2 features: reported honestly, not as capabilities of this version
    caps["plot_deliverable"] = _cap(STATUS_NOT_IMPLEMENTED, None, note=NO_COMMAND_NOTE)
    caps["edit_dwg"] = _cap(STATUS_NOT_IMPLEMENTED, None, note=NO_COMMAND_NOTE)

    caps["pdf_to_png"] = (
        _cap(STATUS_AVAILABLE, "pypdfium2")
        if pdfium_state == "ok"
        else _cap(
            STATUS_MISSING if pdfium_state == "missing" else STATUS_DEGRADED,
            None,
            opts["pypdfium2"],
        )
    )

    packages = {}
    for name, (_required, windows_only) in PACKAGES.items():
        if windows_only and env.platform != "win32":
            continue
        state = package_state(name, pk.get(name))
        minimum = MIN_VERSIONS.get(name)
        packages[name] = (
            "missing"
            if state == "missing"
            else f"{pk[name]} (below {minimum})"
            if state == "old"
            else str(pk[name])
        )
    if pk.get("pymupdf"):
        warnings.append("PyMuPDF is AGPL-licensed: optional accelerator only, never required")
    if env.platform == "darwin" and env.oda:
        warnings.append("ODA File Converter opens a window on macOS")
    if env.platform == "linux" and env.oda and not (env.xvfb or env.display):
        warnings.append("ODA File Converter needs a display: install Xvfb")
    if env.free_gb is not None and env.free_gb < 5:
        warnings.append(f"only {env.free_gb} GB free on the temp drive")

    summary: dict[str, Any] = {
        "platform": env.platform,
        "python": ".".join(str(n) for n in env.python),
        "packages": packages,
        "converters": {"oda": env.oda, "libredwg": sorted(env.libredwg)},
        "cad_progids": [h["progid"] for h in env.hosts][:6],
        "cad_installs": env.installs[:3],
        "cad_running": env.running,
        "free_gb": env.free_gb,
        "capabilities": caps,
    }
    if env.platform == "linux":
        summary["xvfb"] = bool(env.xvfb)
    if any(h["product"] != "AutoCAD" for h in env.hosts):
        warnings.append("non-AutoCAD COM hosts are detected but their API parity is untested")
    if env.com_probe is not None:
        summary["com_probe"] = env.com_probe
    return summary, warnings


def build_report(*, probe_com: bool = False, env: Environment | None = None) -> dict[str, Any]:
    """The full JSON document (output contract). ``env`` is injectable for tests."""
    if env is None and sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        return {
            "status": "refused",
            "command": "doctor",
            "exit_code": 3,
            "summary": {"python": sys.version.split()[0]},
            "errors": [
                {
                    "code": "PYTHON_TOO_OLD",
                    "message": f"Python {need} or newer is required",
                    "hint": f"install Python {need}+ and run this script with it",
                }
            ],
        }
    snapshot = env or gather_environment(probe_com=probe_com)
    summary, warnings = assess(snapshot)
    report: dict[str, Any] = {
        "status": "ok",
        "command": "doctor",
        "exit_code": 0,
        "backend": "none",
    }
    report["summary"] = summary
    if warnings:
        report["warnings"] = warnings[:20]
    return report


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # JSON contract, exit 2
        _emit(
            {
                "status": "error",
                "command": "doctor",
                "exit_code": 2,
                "errors": [{"code": "BAD_ARGS", "message": message}],
            }
        )
        raise SystemExit(2)


def _emit(report: dict[str, Any]) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--probe-com",
        action="store_true",
        help="start one private CAD instance to prove COM works (may take a licence; ask the user first)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Report what the cad-drawings skill can do on this machine.")
    add_arguments(parser)
    args = parser.parse_args(argv)
    report = build_report(probe_com=args.probe_com)
    _emit(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
