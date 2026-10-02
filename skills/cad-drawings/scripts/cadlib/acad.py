"""COM session library: a dedicated CAD instance driven through late-bound COM (Windows only).

Design rules, each one the answer to a failure seen in practice:

* **Own instance, own PID.** ``AcadSession.start`` creates a NEW process (``CoCreateInstanceEx``
  with ``CLSCTX_LOCAL_SERVER``, never plain ``Dispatch``, which attaches to the first running
  instance) and attributes its PID by diffing the process list before/after, cross-checked
  against the PID that owns the application window. If no single new PID can be attributed the
  start is refused: nothing is ever killed that this session did not start.
* **Late binding only** (``win32com.client.dynamic``): no ``gen_py`` cache is built.
* **Retry wraps the whole expression**, classified by HRESULT, never by localized message text
  (``retry``/``retry_expr``). Transient: ``RPC_E_CALL_REJECTED``, ``RPC_E_SERVERCALL_RETRYLATER``
  and late-binding ``AttributeError: <unknown>.X``. Everything else from COM is permanent and
  becomes ``ComError`` (``CadError`` code ``COM_ERROR``) carrying the HRESULT and, when present,
  the host's own sub-code.
* **Quit is asynchronous** and can hang after a failed plot: ``quit`` re-wraps the application
  object (a stale proxy raises ``AttributeError: <unknown>.Quit`` once documents were closed),
  calls ``Quit``, waits for the own PID to vanish, then asks that PID to close (``taskkill``
  without ``/F``) and only after a timeout force-terminates that PID (and its children).
* **Documents are addressed by normalised full path**, never through ``ActiveDocument``. A file
  already open in the instance and not opened by this session is refused (``DOC_OPEN_BY_USER``).
  ``.dwl``/``.dwl2`` files are removed only when this session created them.
* **Outputs are written to a fresh temporary name next to the destination, verified (exists,
  newer than the call, non-empty, format sanity) and then moved into place**; ``PlotToFile`` never
  overwrites, and an old artifact must never pass for a new one.
* System variables are changed only inside ``sysvars()`` and restored; ``FILEDIA`` is never
  touched.

PDF viewer side effect: with some PDF device configurations the CAD opens the finished PDF in
the default viewer. No system variable or Preferences property controls this (probed:
``Preferences.Output`` has no such member and PDFOPEN-like variables do not exist); it is an
option of the plotter configuration, and editing the user's plotter files is out of bounds.
So ``plot_layout_pdf`` writes straight to the final name (never moves the file during or right
after the call, which made the viewer report "file not found") and always returns a warning.

Measured on a synthetic drawing with two layouts (AutoCAD 2024, DWG opened read-only in a fresh
instance, then SaveAs DXF 2013): WITHOUT activating the layouts first, every viewport of the
layout that was never current exports with ``id 0, status 0`` (reads as "off"); WITH each layout
activated through ``CTAB`` before ``SaveAs`` all viewports export with status 1 and 2 and the
viewport-frozen layers are intact. ``export_dxf`` therefore activates every layout (and restores
the original tab) and still scans the written file, warning when any viewport has status <= 0.

The module imports cleanly on every platform; Windows-only imports happen inside functions and
raise ``CadError(MISSING_DEPENDENCY)`` only when the COM backend is actually used.
"""

from __future__ import annotations

import contextlib
import csv
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

from .result import CadError, ExitCode

T = TypeVar("T")

# --- HRESULTs (signed 32-bit, as pywintypes.com_error reports them) -------------------------
RPC_E_CALL_REJECTED = -2147418111  # 0x80010001
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
DISP_E_EXCEPTION = -2147352567  # 0x80020009: the automation server raised its own error
TRANSIENT_HRESULTS = frozenset({RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER})
# The server went away while we talked to it: expected during Quit.
GONE_HRESULTS = frozenset(
    {
        -2147417848,  # RPC_E_DISCONNECTED
        -2147023174,  # RPC_S_SERVER_UNAVAILABLE
        -2147023170,  # RPC_S_CALL_FAILED
        -2147417827,  # RPC_E_SERVER_DIED_DNE (server died, object no longer exists)
    }
)

# AcSaveAsType values.
SAVEAS_DXF = {"2000": 13, "2004": 25, "2007": 37, "2010": 49, "2013": 61, "2018": 65}
SAVEAS_DWG = {"2000": 12, "2004": 24, "2007": 36, "2010": 48, "2013": 60, "2018": 64}

PDF_DEVICE = "DWG To PDF.pc3"
# AcPlotType
_PLOT_TYPES = {"display": 0, "extents": 1, "limits": 2, "view": 3, "window": 4, "layout": 5}
_ROTATIONS = {0: 0, 90: 1, 180: 2, 270: 3}  # AcPlotRotation
_ACSCALE_TO_FIT = 0
_PAGE_SETUP_KEYS = frozenset(
    {"device", "media", "plot_area", "window", "scale", "rotation", "style_sheet"}
)
_PAPER_UNITS_TO_MM = {0: 25.4, 1: 1.0}  # acInches, acMillimeters (acPixels unsupported)

_GENERIC_PROGIDS = (
    "AutoCAD.Application",
    "BricscadApp.AcadApplication",
    "ZWCAD.Application",
    "GStarCAD.Application",
)
_VERSIONED_AUTOCAD = re.compile(r"^AutoCAD\.Application\.(\d+)(?:\.(\d+))?$")
_MEDIA_SIZE = re.compile(r"\((\d+(?:\.\d+)?)_x_(\d+(?:\.\d+)?)_(MM|INCHES)\)", re.IGNORECASE)

VIEWER_WARNING = (
    "the PDF plotter may open the result in the default PDF viewer; this is a device option "
    "(PDF options of the plotter) that COM cannot switch off, tell the user"
)

MIN_FREE_GB = 5.0
OP_TIMEOUT = 90.0  # per COM operation (open, save, plot, start) before the watchdog acts
_WATCHDOG_GRACE = 10.0
_MAX_SCAN_BYTES = 200 * 1024 * 1024


# --- small indirections (replaced in unit tests) --------------------------------------------
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> float:
    return time.monotonic()


def _wait_until(pred: Callable[[], bool], timeout: float, interval: float = 0.5) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses; True when it became true."""
    deadline = _now() + timeout
    while True:
        if pred():
            return True
        remaining = deadline - _now()
        if remaining <= 0:
            return False
        _sleep(min(interval, remaining))


# --- platform ---------------------------------------------------------------------------------
def _com() -> SimpleNamespace:
    """Import the Windows COM modules lazily; CadError(MISSING_DEPENDENCY) elsewhere."""
    if sys.platform != "win32":
        raise CadError(
            "MISSING_DEPENDENCY",
            f"the COM backend needs Windows (this is {sys.platform})",
            exit_code=ExitCode.MISSING_DEPENDENCY,
            hint="use the DXF path: install ODA File Converter or LibreDWG (see doctor)",
        )
    try:
        import pythoncom
        import pywintypes
        import win32com.client
        import win32process
        from win32com.client import dynamic
    except ImportError as exc:
        raise CadError(
            "MISSING_DEPENDENCY",
            f"pywin32 is not installed ({exc})",
            exit_code=ExitCode.MISSING_DEPENDENCY,
            hint="python -m pip install 'pywin32>=312' (ask the user first)",
        ) from exc
    return SimpleNamespace(
        pythoncom=pythoncom,
        pywintypes=pywintypes,
        client=win32com.client,
        dynamic=dynamic,
        win32process=win32process,
    )


def discover_progids() -> list[str]:
    """ProgIDs of installed CAD hosts: versioned AutoCAD newest first, then generic ones.

    Reads the registry (read-only). On other platforms returns the generic names only.
    """
    versioned: list[tuple[tuple[int, int], str]] = []
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    match = _VERSIONED_AUTOCAD.match(name)
                    if match:
                        key = (int(match.group(1)), int(match.group(2) or 0))
                        versioned.append((key, name))
        except (ImportError, OSError):
            pass
    ordered = [name for _, name in sorted(versioned, reverse=True)]
    return ordered + [p for p in _GENERIC_PROGIDS if p not in ordered]


PROGIDS: list[str] = discover_progids()


def is_experimental(progid: str) -> bool:
    """Hosts other than AutoCAD work through the same API but are not verified."""
    return not progid.lower().startswith("autocad.")


def _image_for(progid: str) -> str:
    low = progid.lower()
    if low.startswith("bricscad"):
        return "bricscad.exe"
    if low.startswith("zwcad"):
        return "ZWCAD.exe"
    if low.startswith("gstarcad"):
        return "gcad.exe"
    return "acad.exe"


# --- errors and retry -------------------------------------------------------------------------
class ComError(CadError):
    """A permanent COM failure: carries the HRESULT and the host's own sub-code if any."""

    def __init__(
        self, message: str, *, hresult: int | None, scode: int | None, hint: str | None = None
    ) -> None:
        super().__init__("COM_ERROR", message, exit_code=ExitCode.ERROR, hint=hint)
        self.hresult = hresult
        self.scode = scode


def _signed(value: int) -> int:
    return value - (1 << 32) if value > 0x7FFFFFFF else value


def hresult_of(exc: BaseException) -> int | None:
    """HRESULT of a ``com_error``-like exception (duck-typed: an int ``hresult`` attribute)."""
    value = getattr(exc, "hresult", None)
    return _signed(value) if isinstance(value, int) else None


def excepinfo_code(exc: BaseException) -> int | None:
    """Sub-code from EXCEPINFO (scode, else wCode) when the server supplied one."""
    args = getattr(exc, "args", ())
    info = args[2] if len(args) > 2 else None
    if isinstance(info, (tuple, list)) and len(info) >= 6:
        for candidate in (info[5], info[0]):
            if isinstance(candidate, int) and candidate != 0:
                return _signed(candidate)
    return None


def classify(exc: BaseException) -> str:
    """'transient' (retry), 'permanent' (COM failure) or 'other' (not a COM problem)."""
    if isinstance(exc, CadError):
        return "other"
    hresult = hresult_of(exc)
    if hresult is not None:
        if hresult in TRANSIENT_HRESULTS or excepinfo_code(exc) in TRANSIENT_HRESULTS:
            return "transient"
        return "permanent"
    if isinstance(exc, AttributeError) and "<unknown>" in str(exc):
        return "transient"  # late-binding proxy that is not ready or went stale
    return "other"


def _com_error(exc: BaseException) -> ComError:
    hresult = hresult_of(exc)
    scode = excepinfo_code(exc)
    parts = [f"HRESULT 0x{(hresult or 0) & 0xFFFFFFFF:08X}"]
    if scode is not None:
        parts.append(f"host code 0x{scode & 0xFFFFFFFF:08X}")
    args = getattr(exc, "args", ())
    text = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
    detail = "; ".join(parts) + (f" ({text})" if text else "")
    return ComError(f"COM call failed: {detail}", hresult=hresult, scode=scode)


def backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Delay before retry number ``attempt`` (0-based): exponential, capped."""
    return min(max_delay, base_delay * (2**attempt))


def retry(
    fn: Callable[..., T],
    *args: Any,
    tries: int = 12,
    base_delay: float = 0.4,
    max_delay: float = 5.0,
    **kwargs: Any,
) -> T:
    """Call ``fn(*args, **kwargs)`` again while COM reports a transient failure.

    This wraps the WHOLE call. ``retry(doc.Blocks.Item, i)`` evaluates ``doc.Blocks`` before
    retry starts, so for chained access use ``retry_expr(lambda: doc.Blocks.Item(i))``.
    Permanent COM failures raise ``ComError`` at once; non-COM exceptions propagate unchanged;
    running out of tries raises ``CadError("COM_TRANSIENT")``.
    """
    last: BaseException | None = None
    for attempt in range(max(1, tries)):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            kind = classify(exc)
            if kind == "other":
                raise
            if kind == "permanent":
                raise _com_error(exc) from exc
            last = exc
            if attempt < tries - 1:
                _sleep(backoff_delay(attempt, base_delay, max_delay))
    raise CadError(
        "COM_TRANSIENT",
        f"CAD kept rejecting the call after {tries} tries ({last})",
        exit_code=ExitCode.BUSY,
        hint="the CAD instance is busy or showing a modal dialog; retry later",
    ) from last


def retry_expr(expr: Callable[[], T], **kw: Any) -> T:
    """``retry`` for an expression: ``retry_expr(lambda: doc.Blocks.Item(i))``."""
    return retry(expr, **kw)


# --- helpers: processes, disk, locks, files ---------------------------------------------------
def _list_pids(image: str) -> set[int]:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CadError("PID_UNAVAILABLE", f"cannot list processes: {exc}") from exc
    pids: set[int] = set()
    for row in csv.reader(proc.stdout.splitlines()):
        if len(row) > 1 and row[0].lower() == image.lower() and row[1].isdigit():
            pids.add(int(row[1]))
    return pids


def _pid_alive(pid: int, image: str) -> bool:
    return pid in _list_pids(image)


def _taskkill(args: list[str]) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["taskkill", *args],
            capture_output=True,
            timeout=30,
            check=False,
        )


def _close_pid(pid: int) -> None:
    """Ask that PID to close its windows (no /F)."""
    _taskkill(["/PID", str(pid)])


def _kill_pid(pid: int) -> None:
    """Forced termination of that PID (and its children); never by image name."""
    _taskkill(["/PID", str(pid), "/F", "/T"])


def preflight_disk(path: Path, min_gb: float = MIN_FREE_GB) -> None:
    """``runs.preflight_disk`` (imported lazily so this module loads without it)."""
    from .runs import preflight_disk as impl

    impl(path, min_gb)


def _com_lock() -> contextlib.AbstractContextManager[None]:
    from .runs import acquire_lock

    return acquire_lock("com")


def _note_child(pid: int, image: str) -> None:
    """Record the CAD process in the run bookkeeping so ``cleanup`` can find an orphan."""
    from .runs import note_child

    note_child(pid, image, "cad")


def _assign_job(pid: int) -> tuple[Any, str | None]:
    """Put the process into a kill-on-close Job Object owned by this Python process.

    Returns ``(job handle, None)`` or ``(None, reason)``: best effort, never raises. Keeping
    the handle open for the life of the session makes Windows terminate the CAD process when
    this process dies (for example killed by a host timeout in the middle of a plot).
    """
    try:
        import win32api
        import win32con
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        handle = win32api.OpenProcess(access, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
        return job, None
    except Exception as exc:  # noqa: BLE001 - best effort by design
        return None, (
            f"the CAD process could not be tied to this process (job object: {exc}); "
            "if this process is killed the CAD instance may stay running"
        )


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Key for comparing documents: absolute, symlinks resolved, case-folded on Windows."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _dxf_save_type(version: str) -> int:
    save_type = SAVEAS_DXF.get(version)
    if save_type is None:
        raise CadError(
            "BAD_ARGS",
            f"unsupported DXF version {version!r}",
            exit_code=ExitCode.BAD_ARGS,
            hint="use one of " + ", ".join(sorted(SAVEAS_DXF)),
        )
    return save_type


def _validate_plot_args(layout: str, dst: Path, page_setup: dict[str, Any] | None) -> None:
    unknown = set(page_setup or {}) - _PAGE_SETUP_KEYS
    if unknown:
        raise CadError(
            "BAD_ARGS",
            f"unknown page_setup keys: {', '.join(sorted(unknown))}",
            exit_code=ExitCode.BAD_ARGS,
            hint="allowed: " + ", ".join(sorted(_PAGE_SETUP_KEYS)),
        )
    if layout.lower() == "model":
        raise CadError(
            "PRECONDITION_FAILED",
            "plotting model space directly can produce an empty PDF",
            exit_code=ExitCode.PRECONDITION_FAILED,
            hint="plot a paper-space layout (create one with a viewport if needed)",
        )
    if dst.exists():
        raise CadError(
            "DEST_EXISTS",
            f"{dst.name} already exists and PlotToFile never overwrites",
            exit_code=ExitCode.PRECONDITION_FAILED,
            hint="plot into a fresh run directory",
        )


def _temp_sibling(dst: Path, suffix: str) -> Path:
    return dst.parent / f"{dst.stem}.{uuid.uuid4().hex[:8]}.part{suffix}"


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _verify_fresh(path: Path, started: float) -> None:
    """Exists, non-empty and written during this call (never trust an older artifact)."""
    if not path.is_file():
        raise CadError("STALE_OUTPUT", f"expected output was not written: {path.name}")
    stat = path.stat()
    if stat.st_size <= 0:
        raise CadError("PLOT_BAD_OUTPUT", f"output is empty: {path.name}")
    if stat.st_mtime < started - 2.0:  # 2 s: coarse filesystem timestamps
        raise CadError(
            "STALE_OUTPUT",
            f"{path.name} is older than this call; refusing to treat it as the result",
        )


def _preflight_outputs(dst: Path) -> None:
    preflight_disk(dst.parent)
    preflight_disk(Path(tempfile.gettempdir()))


def scan_viewports(path: Path) -> list[dict[str, Any]] | None:
    """VIEWPORT entities of an ASCII DXF as ``{layout, id, status}`` (group codes 410/69/68).

    A streaming scan, no ezdxf. Returns None for binary DXF or files above the size cap.
    """
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return None
        found: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        with open(path, encoding="utf-8", errors="replace") as fh:
            if fh.readline().startswith("AutoCAD Binary DXF"):
                return None
            fh.seek(0)
            while True:
                code = fh.readline()
                if not code:
                    break
                value = fh.readline().strip()
                code = code.strip()
                if code == "0":
                    if current is not None:
                        found.append(current)
                    current = (
                        {"layout": None, "id": None, "status": None}
                        if (value == "VIEWPORT")
                        else None
                    )
                elif current is not None:
                    if code == "68" and current["status"] is None:
                        current["status"] = int(value)
                    elif code == "69" and current["id"] is None:
                        current["id"] = int(value)
                    elif code == "410":
                        current["layout"] = value
            if current is not None:
                found.append(current)
        return found
    except (OSError, ValueError):
        return None


def parse_media_mm(name: str) -> tuple[float, float] | None:
    """(width, height) in mm from a canonical media name like ``ISO_A3_(420.00_x_297.00_MM)``."""
    match = _MEDIA_SIZE.search(name or "")
    if not match:
        return None
    factor = 25.4 if match.group(3).upper() == "INCHES" else 1.0
    return float(match.group(1)) * factor, float(match.group(2)) * factor


def closest_iso_media(names: list[str], width_mm: float, height_mm: float) -> str | None:
    """Canonical ISO media whose size (same orientation) is closest to the given paper size.

    Prefers regular media over ``full_bleed`` variants on ties.
    """
    best: tuple[float, int, str] | None = None
    for name in names:
        if "iso" not in name.lower():
            continue
        size = parse_media_mm(name)
        if size is None:
            continue
        distance = abs(size[0] - width_mm) + abs(size[1] - height_mm)
        key = (distance, 1 if "full_bleed" in name.lower() else 0, name)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def sizes_match(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Equal within 2 % + 1 mm, in either orientation."""

    def close(x: float, y: float) -> bool:
        return abs(x - y) <= 0.02 * max(x, y) + 1.0

    return (close(a[0], b[0]) and close(a[1], b[1])) or (close(a[0], b[1]) and close(a[1], b[0]))


def _pdf_info(path: Path) -> tuple[int, tuple[float, float]] | None:
    """(pages, first page size in mm) with pypdfium2; None when it cannot be determined."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    pdf = pdfium.PdfDocument(str(path))
    try:
        pages = len(pdf)
        if pages == 0:
            return 0, (0.0, 0.0)
        width_pt, height_pt = pdf[0].get_size()
        return pages, (width_pt / 72 * 25.4, height_pt / 72 * 25.4)
    finally:
        pdf.close()


def _double_array(values: tuple[float, ...]) -> Any:
    com = _com()
    return com.client.VARIANT(com.pythoncom.VT_ARRAY | com.pythoncom.VT_R8, list(values))


def _create_app(progid: str) -> tuple[Any, Any]:
    """Start a NEW instance; returns (late-bound app, raw IDispatch)."""
    com = _com()
    clsid = com.pywintypes.IID(progid)
    disp = com.pythoncom.CoCreateInstanceEx(
        clsid, None, com.pythoncom.CLSCTX_LOCAL_SERVER, None, (com.pythoncom.IID_IDispatch,)
    )[0]
    return com.dynamic.Dispatch(disp), disp


def _get_active(progid: str) -> tuple[Any, Any]:
    com = _com()
    unknown = com.pythoncom.GetActiveObject(com.pywintypes.IID(progid))
    disp = unknown.QueryInterface(com.pythoncom.IID_IDispatch)
    return com.dynamic.Dispatch(disp), disp


def _wrap(disp: Any) -> Any:
    return _com().dynamic.Dispatch(disp)


def _app_pid(app: Any) -> int | None:
    """PID owning the application window (None when it cannot be determined)."""
    try:
        hwnd = app.HWND
        return int(_com().win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:  # noqa: BLE001 - best-effort cross-check only
        return None


# --- documents ----------------------------------------------------------------------------------
class Doc:
    """A document opened by an ``AcadSession``; ``close()`` is idempotent.

    Entity handles read through a Doc are valid only for the file version in which they were
    read: saving, re-saving or converting can renumber them, so never carry handles from one
    version of a file to another (match by content instead).
    """

    def __init__(self, session: AcadSession, raw: Any, path: Path) -> None:
        self.session = session
        self.raw = raw
        self.path = path
        self.key = normalize_path(path)
        self.closed = False

    def close(self, save: bool = False) -> None:
        if self.closed:
            return
        if save:
            raise CadError(
                "BAD_ARGS", "saving through Doc.close is not supported", exit_code=ExitCode.BAD_ARGS
            )
        retry_expr(lambda: self.raw.Close(False))
        self.closed = True
        self.session._forget(self)

    def export_dxf(
        self, dst: Path, version: str = "2013", *, activate_layouts: bool = True
    ) -> list[str]:
        """Save this open document as DXF straight to ``dst``; returns warnings.

        ``dst`` must not exist. SaveAs rebinds the document to ``dst`` and keeps that file
        locked: the document now IS the DXF, so close it (do not edit or save it further), and
        a partial file after a failure can only be removed once it is closed. Every paper-space
        layout is activated first (and the original tab restored) because a layout that was
        never current exports its viewports with status 0; the written file is scanned and any
        viewport with status <= 0 is reported in the warnings.
        """
        save_type = _dxf_save_type(version)
        dst = Path(dst)
        if dst.exists():
            raise CadError(
                "DEST_EXISTS",
                f"{dst.name} already exists",
                exit_code=ExitCode.PRECONDITION_FAILED,
                hint="export into a fresh run directory",
            )
        _preflight_outputs(dst)
        started = time.time()
        session = self.session
        with session.sysvars(self, ISAVEBAK=0, ISAVEPERCENT=0):
            if activate_layouts:
                session._activate_all_layouts(self)
            with session._deadline("SaveAs"):
                retry_expr(lambda: self.raw.SaveAs(str(dst), save_type))
        _verify_fresh(dst, started)
        return session._viewport_warnings(dst)

    def plot_layout_pdf(
        self, layout: str, dst: Path, *, page_setup: dict[str, Any] | None = None
    ) -> list[str]:
        """Plot one layout of this open document to a single-page PDF at ``dst``.

        See ``AcadSession.plot_layout_pdf``. The page setup is applied in memory to this
        document, so close it without saving afterwards. The document stays open and usable.
        """
        _validate_plot_args(layout, Path(dst), page_setup)
        dst = Path(dst)
        session = self.session
        _preflight_outputs(dst)
        started = time.time()
        warnings: list[str] = [VIEWER_WARNING]
        try:
            with session.sysvars(self, BACKGROUNDPLOT=0, ISAVEBAK=0, ISAVEPERCENT=0):
                lay, lay_name = session._find_layout(self, layout)
                retry_expr(lambda: self.raw.Activate())
                retry_expr(lambda: self.raw.SetVariable("CTAB", lay_name))
                media = session._configure_plot(lay, page_setup or {}, warnings)
                with contextlib.suppress(Exception):
                    self.raw.Plot.QuietErrorMode = True
                with session._deadline("PlotToFile"):
                    ok = retry_expr(lambda: self.raw.Plot.PlotToFile(str(dst)))
                if not ok:
                    raise CadError(
                        "PLOT_FAILED",
                        f"PlotToFile returned False for layout {lay_name!r}",
                        hint="check the layout has a PDF plotter and a valid media; "
                        "pass page_setup with device and media",
                    )
            _verify_fresh(dst, started)
            warnings += session._verify_pdf(dst, media)
        except BaseException:
            _unlink(dst)  # did not exist before this call, so it is ours (may fail if viewed)
            raise
        return warnings

    def __enter__(self) -> Doc:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AcadSession:
    """One CAD application instance driven through COM. Use as a context manager."""

    def __init__(
        self,
        app: Any,
        *,
        pid: int | None,
        progid: str,
        owned: bool,
        disp: Any = None,
        stack: contextlib.ExitStack | None = None,
        log: Callable[[str], None] | None = None,
        op_timeout: float = OP_TIMEOUT,
    ) -> None:
        self.op_timeout = op_timeout
        self._app = app
        self._disp = disp
        self.pid = pid  # type: ignore[assignment]
        self.progid = progid
        self.owned = owned
        self.image = _image_for(progid)
        self.warnings: list[str] = []
        self._stack = stack or contextlib.ExitStack()
        self._log = log or (lambda message: None)
        self._docs: list[Doc] = []
        self._dwl: dict[Path, float] = {}  # lock files created by our Open -> open time
        self._unhealthy = False
        self._dead = False  # the watchdog terminated the process
        self._job: Any = None
        self._finished = False

    # -- construction -----------------------------------------------------------------------
    @classmethod
    def start(
        cls,
        progid: str | None = None,
        *,
        visible: bool = False,
        timeout: float = OP_TIMEOUT,
        lock: bool = True,
        log: Callable[[str], None] | None = None,
        op_timeout: float = OP_TIMEOUT,
    ) -> AcadSession:
        """Start a dedicated instance and wait until it accepts calls.

        Refuses (``CadError`` BUSY) when no single new PID can be attributed to it. The process is
        recorded for ``cleanup`` and, best effort, tied to this process with a kill-on-close Job
        Object (a warning says when that was refused). ``timeout`` bounds the start; a watchdog
        terminates the new process (only that PID) if a call blocks past it.
        """
        _com()  # fail early and clearly on unsupported platforms
        emit = log or (lambda message: None)
        stack = contextlib.ExitStack()
        try:
            if lock:
                stack.enter_context(_com_lock())
            candidates = [progid] if progid else list(PROGIDS)
            app = disp = None
            chosen = ""
            before: set[int] = set()
            failures: list[str] = []
            for name in candidates:
                image = _image_for(name)
                before = _list_pids(image)
                try:
                    app, disp = _create_app(name)
                except Exception as exc:  # noqa: BLE001 - try the next ProgID
                    failures.append(f"{name}: {exc}")
                    continue
                chosen = name
                break
            if app is None:
                raise CadError(
                    "NO_BACKEND",
                    "no CAD application could be started through COM",
                    exit_code=ExitCode.MISSING_DEPENDENCY,
                    hint="; ".join(failures[:3]) or "no ProgID is registered (run doctor)",
                )
            pid = cls._attribute_pid(app, _image_for(chosen), before, min(timeout, 20.0))
            session = cls(
                app,
                pid=pid,
                progid=chosen,
                owned=True,
                disp=disp,
                stack=stack,
                log=emit,
                op_timeout=op_timeout,
            )
        except BaseException:
            stack.close()
            raise
        emit(f"started {chosen} pid={pid}")
        if is_experimental(chosen):
            session.warnings.append(
                f"{chosen} is not AutoCAD: COM support is experimental and unverified"
            )
        session._register_child()
        try:
            with session._deadline("start", timeout + _WATCHDOG_GRACE):
                session._app_call(lambda a: setattr(a, "Visible", visible))
                session._wait_quiescent(timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                session.quit(timeout=30.0)
            raise
        return session

    @staticmethod
    def _attribute_pid(app: Any, image: str, before: set[int], timeout: float) -> int:
        new: set[int] = set()

        def found() -> bool:
            new.clear()
            new.update(_list_pids(image) - before)
            return bool(new)

        _wait_until(found, timeout, interval=0.5)
        owner = _app_pid(app)
        if owner is not None and owner in new:
            return owner
        if owner is None and len(new) == 1:
            return next(iter(new))
        if new:
            # something new is running but cannot be attributed: it is hidden and still alive
            candidates = ", ".join(str(p) for p in sorted(new))
            err = CadError(
                "BUSY",
                "cannot attribute the new CAD process to this session; a CAD instance started "
                f"by this call is still running (candidate PIDs: {candidates})",
                exit_code=ExitCode.BUSY,
                hint="close it from the Task Manager or ask the user; this tool did not close it",
            )
            err.candidate_pids = sorted(new)  # type: ignore[attr-defined]
            raise err
        raise CadError(
            "BUSY",
            "no new CAD process appeared: the host reused a running instance, which was left alone",
            exit_code=ExitCode.BUSY,
            hint="single-instance hosts cannot get a dedicated session; nothing was closed",
        )

    @classmethod
    def attach_guarded(
        cls, *, confirmed: bool, log: Callable[[str], None] | None = None
    ) -> AcadSession:
        """Attach to the user's running instance. Only after the user agreed.

        Never changes ``Visible``, never closes or saves documents it did not open, warns about
        user documents with unsaved changes. ``quit()`` only closes this session's documents.
        """
        if not confirmed:
            raise CadError(
                "PRECONDITION_FAILED",
                "attaching to the user's CAD session needs explicit confirmation",
                exit_code=ExitCode.PRECONDITION_FAILED,
                hint="ask the user; prefer start() and working on copies",
            )
        _com()
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(_com_lock())
            for name in PROGIDS:
                try:
                    app, disp = _get_active(name)
                except Exception:  # noqa: BLE001, S112 - not running under this ProgID
                    continue
                break
            else:
                raise CadError(
                    "NO_INSTANCE",
                    "no running CAD instance found",
                    exit_code=ExitCode.PRECONDITION_FAILED,
                    hint="start a dedicated instance instead",
                )
            session = cls(
                app,
                pid=_app_pid(app),
                progid=name,
                owned=False,
                disp=disp,
                stack=stack,
                log=log,
            )
        except BaseException:
            stack.close()
            raise
        for doc in session._iter_raw_docs():
            if retry_expr(lambda d=doc: d.Saved) is False:
                session.warnings.append(
                    f"user document {retry_expr(lambda d=doc: d.FullName)} has unsaved changes"
                )
        return session

    # -- plumbing ----------------------------------------------------------------------------
    def _warn(self, message: str) -> None:
        self._log(message)
        if message not in self.warnings:
            self.warnings.append(message)

    def _register_child(self) -> None:
        try:
            _note_child(self.pid, self.image)  # type: ignore[arg-type]
        except ImportError:
            pass  # runs.note_child not available: nothing to record
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop the session
            self._warn(f"could not record the CAD process for cleanup: {exc}")
        self._job, reason = _assign_job(self.pid)  # type: ignore[arg-type]
        if reason:
            self._warn(reason)

    def _terminate_own(self) -> bool:
        """Close, then force-terminate, only the process this session started."""
        if not self.owned or self.pid is None or not self._alive():
            return True
        _close_pid(self.pid)
        if _wait_until(lambda: not self._alive(), _WATCHDOG_GRACE):
            return True
        _kill_pid(self.pid)
        return _wait_until(lambda: not self._alive(), _WATCHDOG_GRACE)

    @contextlib.contextmanager
    def _deadline(self, what: str, limit: float | None = None) -> Iterator[None]:
        """Watchdog for one blocking COM operation.

        If it has not finished after ``limit`` seconds, a thread terminates this session's own
        process (never any other PID); the blocked call then fails and the operation raises
        ``CadError("TIMEOUT")``. The session is unusable afterwards except ``quit()``.
        Sessions that did not start their instance (guarded attach) have no watchdog: the
        user's process is never terminated.
        """
        if self._dead:
            raise self._timeout_error(what, 0.0)
        if not self.owned or self.pid is None:
            yield
            return
        seconds = self.op_timeout if limit is None else limit
        done = threading.Event()
        fired = threading.Event()

        def watch() -> None:
            if done.wait(seconds):
                return
            self._dead = True
            fired.set()
            self._warn(f"{what} did not finish in {seconds:g}s; terminating pid {self.pid}")
            self._terminate_own()

        thread = threading.Thread(target=watch, name="cad-watchdog", daemon=True)
        thread.start()
        try:
            yield
        except Exception as exc:
            done.set()
            thread.join(3 * _WATCHDOG_GRACE)
            if fired.is_set():
                raise self._timeout_error(what, seconds) from exc
            raise
        else:
            done.set()
            thread.join(3 * _WATCHDOG_GRACE)
            if fired.is_set():
                raise self._timeout_error(what, seconds)
        finally:
            done.set()

    def _timeout_error(self, what: str, seconds: float) -> CadError:
        return CadError(
            "TIMEOUT",
            f"{what} did not finish within {seconds:g}s; the CAD process {self.pid} started by "
            "this session was terminated",
            exit_code=ExitCode.TIMEOUT,
            hint="retry; a very large drawing or a modal dialog in CAD can block a call",
        )

    def _refresh_app(self) -> None:
        """Re-wrap the original IDispatch: a stale late-bound proxy raises AttributeError."""
        if self._disp is not None:
            self._app = _wrap(self._disp)

    def _app_call(self, expr: Callable[[Any], T], **kw: Any) -> T:
        if self._dead:
            raise self._timeout_error("CAD call", 0.0)

        def attempt() -> T:
            try:
                return expr(self._app)
            except AttributeError as exc:
                if "<unknown>" in str(exc):
                    self._refresh_app()
                raise

        return retry_expr(attempt, **kw)

    def _wait_quiescent(self, timeout: float) -> None:
        def ready() -> bool:
            try:
                return bool(self._app_call(lambda a: a.GetAcadState().IsQuiescent, tries=3))
            except CadError as exc:
                if exc.code == "COM_TRANSIENT":
                    return False
                raise

        if not _wait_until(ready, timeout, interval=0.5):
            raise CadError(
                "TIMEOUT",
                f"CAD did not become idle within {timeout:g}s",
                exit_code=ExitCode.TIMEOUT,
            )

    def _idle(self, timeout: float = 30.0) -> None:
        """Best effort: wait for the host to be idle before a heavy call."""
        try:
            self._wait_quiescent(timeout)
        except CadError as exc:
            self._warn(f"proceeding although CAD is not idle: {exc.message}")

    def _iter_raw_docs(self) -> Iterator[Any]:
        count = self._app_call(lambda a: a.Documents.Count)
        for index in range(count):
            yield self._app_call(lambda a, i=index: a.Documents.Item(i))

    @property
    def documents(self) -> list[Doc]:
        """Documents opened by this session and still open."""
        return list(self._docs)

    def _forget(self, doc: Doc) -> None:
        if doc in self._docs:
            self._docs.remove(doc)

    # -- documents ------------------------------------------------------------------------------
    def open(self, path: Path, *, readonly: bool = True) -> Doc:
        """Open ``path`` by its full path; refuse a file the user already has open."""
        path = Path(path)
        if not path.is_file():
            raise CadError(
                "NOT_FOUND",
                f"file not found: {path}",
                exit_code=ExitCode.PRECONDITION_FAILED,
            )
        key = normalize_path(path)
        for existing in self._docs:
            if existing.key == key:
                raise CadError(
                    "DOC_ALREADY_OPEN",
                    f"{path.name} is already open in this session",
                    exit_code=ExitCode.BUSY,
                    hint="close the Doc first (each plot/export needs a fresh document)",
                )
        for raw in self._iter_raw_docs():
            full = retry_expr(lambda r=raw: r.FullName)
            if full and os.path.isabs(full) and normalize_path(full) == key:
                raise CadError(
                    "DOC_OPEN_BY_USER",
                    f"{path.name} is already open in the CAD instance and was not opened here",
                    exit_code=ExitCode.BUSY,
                    hint="work on a copy of the file, or ask the user to close it",
                )
        self._idle()
        dwl = [path.with_suffix(".dwl"), path.with_suffix(".dwl2")]
        preexisting = {p for p in dwl if p.exists()}
        opened_at = time.time()
        with self._deadline("Documents.Open"):
            raw = self._app_call(lambda a: a.Documents.Open(str(path), readonly))
        doc = Doc(self, raw, path)
        self._docs.append(doc)
        # Ours only if absent before the Open and present right after it.
        for lock in dwl:
            if lock not in preexisting and lock.exists():
                self._dwl[lock] = opened_at
        return doc

    def _close_all_docs(self) -> None:
        if self._dead:  # the process is gone; nothing to close
            self._docs.clear()
            return
        for doc in list(self._docs):
            try:
                doc.close()
            except Exception as exc:  # noqa: BLE001 - keep closing the others
                self._unhealthy = True
                self._warn(f"could not close {doc.path.name}: {exc}")

    def _close_quietly(self, doc: Doc) -> None:
        try:
            doc.close()
        except Exception as exc:  # noqa: BLE001 - must not mask the real error
            self._unhealthy = True
            self._warn(f"could not close {doc.path.name}: {exc}")

    # -- system variables ----------------------------------------------------------------------
    @contextlib.contextmanager
    def sysvars(self, doc: Doc | Any, **values: Any) -> Iterator[None]:
        """Set system variables for the block and restore them afterwards. Never FILEDIA."""
        if any(name.upper() == "FILEDIA" for name in values):
            raise CadError("BAD_ARGS", "FILEDIA is never changed", exit_code=ExitCode.BAD_ARGS)
        raw = doc.raw if isinstance(doc, Doc) else doc
        saved: list[tuple[str, Any]] = []
        try:
            for name, value in values.items():
                old = retry_expr(lambda n=name: raw.GetVariable(n))
                if old == value:
                    continue
                saved.append((name, old))
                retry_expr(lambda n=name, v=value: raw.SetVariable(n, v))
            yield
        finally:
            for name, old in reversed(saved):
                try:
                    retry_expr(lambda n=name, o=old: raw.SetVariable(n, o), tries=4)
                except Exception as exc:  # noqa: BLE001 - report, never mask
                    self._warn(f"could not restore {name}={old!r}: {exc}")

    # -- export ----------------------------------------------------------------------------------
    def export_dxf(
        self, src: Path, dst: Path, version: str = "2013", *, activate_layouts: bool = True
    ) -> list[str]:
        """Save ``src`` as DXF at ``dst`` (replacing it atomically). Returns warnings.

        ``src`` should be a staged copy; it is opened read-only, exported through
        ``Doc.export_dxf`` to a temporary name beside ``dst`` and closed before the move.
        """
        _dxf_save_type(version)
        src, dst = Path(src), Path(dst)
        tmp = _temp_sibling(dst, ".dxf")
        doc = self.open(src, readonly=True)
        try:
            warnings = doc.export_dxf(tmp, version, activate_layouts=activate_layouts)
        except BaseException:
            self._close_quietly(doc)
            _unlink(tmp)
            raise
        self._close_quietly(doc)
        try:
            os.replace(tmp, dst)
        except BaseException:
            _unlink(tmp)
            raise
        return warnings

    def save_dwg(self, doc: Doc, dst: Path, version: str = "2013") -> None:
        """SaveAs a DWG of ``doc`` to ``dst`` (fresh name, verified, then moved) and close it.

        SaveAs rebinds the document to the new file, which stays locked until the document is
        closed, so the document is always closed here.
        """
        save_type = SAVEAS_DWG.get(version)
        if save_type is None:
            raise CadError(
                "BAD_ARGS", f"unsupported DWG version {version!r}", exit_code=ExitCode.BAD_ARGS
            )
        dst = Path(dst)
        _preflight_outputs(dst)
        started = time.time()
        tmp = _temp_sibling(dst, ".dwg")
        try:
            retry_expr(lambda: doc.raw.SaveAs(str(tmp), save_type))
        except BaseException:
            self._close_quietly(doc)
            _unlink(tmp)
            raise
        self._close_quietly(doc)
        try:
            _verify_fresh(tmp, started)
            os.replace(tmp, dst)
        except BaseException:
            _unlink(tmp)
            raise

    def _layout_names(self, doc: Doc) -> list[str]:
        layouts = retry_expr(lambda: doc.raw.Layouts)
        count = retry_expr(lambda: layouts.Count)
        return [retry_expr(lambda i=i: layouts.Item(i).Name) for i in range(count)]

    def _activate_all_layouts(self, doc: Doc) -> None:
        original = retry_expr(lambda: doc.raw.GetVariable("CTAB"))
        retry_expr(lambda: doc.raw.Activate())
        try:
            for name in self._layout_names(doc):
                if name.lower() != "model":
                    retry_expr(lambda n=name: doc.raw.SetVariable("CTAB", n))
        finally:
            retry_expr(lambda: doc.raw.SetVariable("CTAB", original))

    def _viewport_warnings(self, dxf: Path) -> list[str]:
        found = scan_viewports(dxf)
        if found is None:
            return ["viewport status of the exported DXF was not checked (binary or very large)"]
        bad = [vp for vp in found if vp["status"] is not None and vp["status"] <= 0]
        if not bad:
            return []
        message = (
            f"{len(bad)} viewport(s) have status <= 0 in the COM-exported DXF; status is not "
            "reliable in COM exports, do not conclude that a viewport is off without a plot"
        )
        return [message]

    # -- plot --------------------------------------------------------------------------------------
    def plot_layout_pdf(
        self,
        src: Path,
        layout: str,
        dst: Path,
        *,
        page_setup: dict[str, Any] | None = None,
    ) -> list[str]:
        """Plot one layout of ``src`` to a single-page PDF at ``dst``. Returns warnings.

        A fresh document is opened for each call (thin wrapper over ``Doc.plot_layout_pdf``).
        Without ``page_setup`` the layout's own setup is used, except that a layout without a
        usable PDF device (typical for layouts derived from DXF) gets the built-in PDF plotter
        and the closest ISO media; that is reported. ``page_setup`` keys: ``device``, ``media``
        (canonical name), ``plot_area`` (display, extents, limits, view, window, layout),
        ``window`` (x1, y1, x2, y2), ``scale`` ("fit", a number, or "paper:drawing" like
        "1:50"), ``rotation`` (0/90/180/270), ``style_sheet``.

        ``dst`` must not exist (``PlotToFile`` never overwrites): use a fresh run directory.
        The plot is written straight to ``dst`` and never renamed or moved during the call,
        because the PDF device may open the result in the default viewer when it finishes
        (see the module docstring); a viewer launched on a temporary name that is then moved
        shows the user a "file not found" error. The returned warnings always say so.
        """
        src, dst = Path(src), Path(dst)
        _validate_plot_args(layout, dst, page_setup)
        doc = self.open(src, readonly=True)
        try:
            warnings = doc.plot_layout_pdf(layout, dst, page_setup=page_setup)
        except BaseException:
            self._close_quietly(doc)
            raise
        self._close_quietly(doc)
        return warnings

    def _find_layout(self, doc: Doc, name: str) -> tuple[Any, str]:
        layouts = retry_expr(lambda: doc.raw.Layouts)
        count = retry_expr(lambda: layouts.Count)
        names: list[str] = []
        for index in range(count):
            item = retry_expr(lambda i=index: layouts.Item(i))
            item_name = retry_expr(lambda it=item: it.Name)
            names.append(item_name)
            if item_name.lower() == name.lower():
                return item, item_name
        raise CadError(
            "LAYOUT_NOT_FOUND",
            f"no layout named {name!r}",
            exit_code=ExitCode.BAD_ARGS,
            hint="layouts: " + ", ".join(names[:20]),
        )

    def _configure_plot(
        self, lay: Any, ps: dict[str, Any], warnings: list[str]
    ) -> tuple[float, float] | None:
        """Apply the page setup; returns the media size in mm when it is known."""
        width, height = retry_expr(lambda: lay.GetPaperSize())
        units = _PAPER_UNITS_TO_MM.get(retry_expr(lambda: lay.PaperUnits))
        size_mm = (width * units, height * units) if units and width and height else None
        current = str(retry_expr(lambda: lay.ConfigName) or "")
        retry_expr(lambda: lay.RefreshPlotDeviceInfo())
        devices = [str(d) for d in (retry_expr(lambda: lay.GetPlotDeviceNames()) or ())]

        device = ps.get("device")
        if device:
            if device not in devices:
                raise CadError(
                    "PRECONDITION_FAILED",
                    f"plot device {device!r} is not available",
                    exit_code=ExitCode.PRECONDITION_FAILED,
                    hint="available: " + ", ".join(devices[:10]),
                )
        elif current in devices and "pdf" in current.lower():
            device = current
        else:
            if PDF_DEVICE not in devices:
                raise CadError(
                    "PRECONDITION_FAILED",
                    f"{PDF_DEVICE} is not available in this CAD installation",
                    exit_code=ExitCode.PRECONDITION_FAILED,
                    hint="pass page_setup with a PDF device from: " + ", ".join(devices[:10]),
                )
            device = PDF_DEVICE
            shown = current if current and current.lower() != "none" else "no plotter"
            warnings.append(f"layout has {shown}; plotted with the built-in {PDF_DEVICE}")
        changed = device != current
        if changed:
            retry_expr(lambda: setattr(lay, "ConfigName", device))
            retry_expr(lambda: lay.RefreshPlotDeviceInfo())

        names = [str(n) for n in (retry_expr(lambda: lay.GetCanonicalMediaNames()) or ())]
        media = ps.get("media")
        if media:
            if media not in names:
                raise CadError(
                    "PRECONDITION_FAILED",
                    f"media {media!r} is not offered by {device}",
                    exit_code=ExitCode.PRECONDITION_FAILED,
                    hint="offered (ISO): " + ", ".join([n for n in names if "ISO" in n][:8]),
                )
        elif changed:
            media = closest_iso_media(names, *size_mm) if size_mm else None
            if media is None:
                warnings.append("no ISO media matched the layout paper size; device default used")
            else:
                warnings.append(
                    f"media {media} chosen for paper {size_mm[0]:.0f}x{size_mm[1]:.0f} mm"
                )
        if media:
            retry_expr(lambda: setattr(lay, "CanonicalMediaName", media))
        self._apply_page_options(lay, ps)
        final = str(retry_expr(lambda: lay.CanonicalMediaName) or "")
        return parse_media_mm(final)

    def _apply_page_options(self, lay: Any, ps: dict[str, Any]) -> None:
        if "rotation" in ps:
            if ps["rotation"] not in _ROTATIONS:
                raise CadError(
                    "BAD_ARGS",
                    "rotation must be 0, 90, 180 or 270",
                    exit_code=ExitCode.BAD_ARGS,
                )
            retry_expr(lambda: setattr(lay, "PlotRotation", _ROTATIONS[ps["rotation"]]))
        if "style_sheet" in ps:
            retry_expr(lambda: setattr(lay, "StyleSheet", str(ps["style_sheet"])))
        if "plot_area" in ps:
            area = str(ps["plot_area"]).lower()
            if area not in _PLOT_TYPES:
                raise CadError(
                    "BAD_ARGS",
                    f"plot_area must be one of {sorted(_PLOT_TYPES)}",
                    exit_code=ExitCode.BAD_ARGS,
                )
            if area == "window":
                x1, y1, x2, y2 = (float(v) for v in ps["window"])
                retry_expr(
                    lambda: lay.SetWindowToPlot(_double_array((x1, y1)), _double_array((x2, y2)))
                )
            retry_expr(lambda: setattr(lay, "PlotType", _PLOT_TYPES[area]))
        if "scale" in ps:
            scale = ps["scale"]
            if scale == "fit":
                retry_expr(lambda: setattr(lay, "UseStandardScale", True))
                retry_expr(lambda: setattr(lay, "StandardScale", _ACSCALE_TO_FIT))
            else:
                paper, drawing = _parse_scale(scale)
                retry_expr(lambda: setattr(lay, "UseStandardScale", False))
                retry_expr(lambda: lay.SetCustomScale(paper, drawing))

    def _verify_pdf(self, path: Path, media: tuple[float, float] | None) -> list[str]:
        with open(path, "rb") as fh:
            if not fh.read(5) == b"%PDF-":
                raise CadError("PLOT_BAD_OUTPUT", "output is not a PDF (missing header)")
        info = _pdf_info(path)
        if info is None:
            return ["PDF page count and size were not verified (pypdfium2 not installed)"]
        pages, size = info
        if pages != 1:
            raise CadError("PLOT_BAD_OUTPUT", f"PDF has {pages} pages, expected 1")
        if media is None:
            return ["PDF page size was not compared with the media (media size unknown)"]
        if not sizes_match(size, media):
            raise CadError(
                "PLOT_BAD_OUTPUT",
                f"PDF page is {size[0]:.0f}x{size[1]:.0f} mm, media is "
                f"{media[0]:.0f}x{media[1]:.0f} mm",
                hint="the plotter changed the paper; pass page_setup with device and media",
            )
        return []

    # -- shutdown ----------------------------------------------------------------------------------
    def quit(self, timeout: float = 90.0) -> None:
        """Close own documents, quit the instance this session started, release everything.

        Escalation for an owned instance: Quit -> wait for the PID to vanish -> ask that PID to
        close -> forced termination of that PID. Raises ``CadError("TIMEOUT")`` only if the PID
        survives all three; own ``.dwl`` files are removed once the process is gone.
        """
        if self._finished:
            return
        self._finished = True
        gone = True
        try:
            self._close_all_docs()
            if self.owned:
                gone = self._shutdown_process(timeout)
        finally:
            if gone:
                self._remove_dwl()
                self._close_job()
            self._stack.close()
        if not gone:
            raise CadError(
                "TIMEOUT",
                f"CAD process {self.pid} did not exit after Quit, close and forced termination",
                exit_code=ExitCode.TIMEOUT,
                hint="check Task Manager for that PID",
            )

    def _alive(self) -> bool:
        return self.pid is not None and _pid_alive(self.pid, self.image)

    def _shutdown_process(self, timeout: float) -> bool:
        graceful = min(timeout * 0.2, 10.0) if self._unhealthy else timeout * 0.5
        try:
            if self._dead:
                graceful = 5.0  # the watchdog already terminated it; just confirm
            else:
                self._refresh_app()
                self._app_call(lambda a: a.Quit(), tries=4, base_delay=0.5)
        except CadError as exc:
            if not (isinstance(exc, ComError) and exc.hresult in GONE_HRESULTS):
                self._warn(f"Quit call failed: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - escalate by PID below
            self._warn(f"Quit call failed: {exc}")
        if _wait_until(lambda: not self._alive(), graceful):
            return True
        self._warn(
            f"CAD did not exit within {graceful:g}s after Quit; asking pid {self.pid} to close"
        )
        _close_pid(self.pid)  # type: ignore[arg-type]
        if _wait_until(lambda: not self._alive(), timeout * 0.25):
            return True
        self._warn(f"CAD ignored the close request; terminating pid {self.pid}")
        _kill_pid(self.pid)  # type: ignore[arg-type]
        return _wait_until(lambda: not self._alive(), max(10.0, timeout * 0.25))

    def _remove_dwl(self) -> None:
        """Delete lock files this session's Open created (and only those, and only if unchanged
        since: an mtime older than the Open means it is not the file we saw appear)."""
        for path, opened_at in self._dwl.items():
            try:
                if path.stat().st_mtime >= opened_at - 2.0:
                    path.unlink()
            except OSError:
                continue
        self._dwl.clear()

    def _close_job(self) -> None:
        """Release the kill-on-close job; only after the process is gone."""
        job, self._job = self._job, None
        if job is not None:
            with contextlib.suppress(Exception):
                job.Close()

    def __enter__(self) -> AcadSession:  # noqa: PYI034
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self.quit()
        except CadError as err:
            if exc_type is None:
                raise
            self._warn(f"quit failed while handling another error: {err.message}")


def _parse_scale(scale: Any) -> tuple[float, float]:
    try:
        if isinstance(scale, str) and ":" in scale:
            paper, drawing = (float(part) for part in scale.split(":", 1))
        else:
            paper, drawing = float(scale), 1.0
    except (TypeError, ValueError) as exc:
        raise CadError(
            "BAD_ARGS",
            f"scale must be 'fit', a number or 'paper:drawing', got {scale!r}",
            exit_code=ExitCode.BAD_ARGS,
        ) from exc
    if paper <= 0 or drawing <= 0:
        raise CadError("BAD_ARGS", "scale values must be positive", exit_code=ExitCode.BAD_ARGS)
    return paper, drawing
