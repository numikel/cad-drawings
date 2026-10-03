"""cadlib.acad: unit tests with fake COM objects (no CAD) and, marked ``com``, a real session.

COM tests need an installed CAD application, run one at a time and only when no other CAD work
is in progress: ``pytest -m com tests/test_acad.py``. Their output goes to
``CAD_DRAWINGS_COM_OUT`` (default: pytest's tmp path; prefer a drive other than the system one).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

from cadlib import acad
from cadlib.result import CadError, ExitCode

# --- fakes -----------------------------------------------------------------------------------


class FakeComError(Exception):
    """Duck-typed pywintypes.com_error: (hresult, text, excepinfo, argerr)."""

    def __init__(self, hresult: int, text: str = "", excepinfo: tuple[Any, ...] | None = None):
        super().__init__(hresult, text, excepinfo, None)
        self.hresult = hresult


class Clock:
    """Deterministic time: sleeping advances the clock."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(acad, "_now", c.now)
    monkeypatch.setattr(acad, "_sleep", c.sleep)
    return c


@pytest.fixture(autouse=True)
def no_disk_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acad, "preflight_disk", lambda path, min_gb=5.0: None)


class FakeState:
    IsQuiescent = True


class FakeRawDoc:
    """The COM document surface acad.py touches."""

    def __init__(self, path: str, events: list[str] | None = None) -> None:
        self.FullName = path
        self.events = events if events is not None else []
        self.vars: dict[str, Any] = {
            "ISAVEBAK": 1,
            "ISAVEPERCENT": 50,
            "BACKGROUNDPLOT": 2,
            "CTAB": "Model",
            "FILEDIA": 1,
        }
        self.ctab_history: list[Any] = []
        self.layouts = SimpleNamespace(Count=0, Item=lambda i: None)
        self.Plot = SimpleNamespace(QuietErrorMode=False, PlotToFile=lambda p: False)
        self.saveas_writes: str | None = ""  # content SaveAs writes; None = writes nothing
        self.close_error: Exception | None = None
        self.closed = False
        self.collection: Any = None  # the Documents collection that lists this document

    @property
    def Layouts(self) -> Any:
        return self.layouts

    def GetVariable(self, name: str) -> Any:
        return self.vars[name]

    def SetVariable(self, name: str, value: Any) -> None:
        if name == "CTAB":
            self.ctab_history.append(value)
        self.vars[name] = value

    def Activate(self) -> None:
        self.events.append("activate")

    def SaveAs(self, path: str, kind: int) -> None:
        self.events.append(f"saveas:{kind}")
        if self.saveas_writes is not None:
            Path(path).write_text(self.saveas_writes, encoding="utf-8")

    def Close(self, save: bool) -> None:
        assert save is False
        if self.close_error:
            raise self.close_error
        self.closed = True
        self.events.append("doc.close")
        if self.collection is not None and self in self.collection.items:
            self.collection.items.remove(self)  # a closed document leaves the collection


class FakeDocuments:
    def __init__(self, app: FakeApp) -> None:
        self.app = app
        self.items: list[FakeRawDoc] = []
        self.next_open: FakeRawDoc | None = None

    @property
    def Count(self) -> int:
        return len(self.items)

    def Item(self, index: int) -> FakeRawDoc:
        return self.items[index]

    def Open(self, path: str, readonly: bool) -> FakeRawDoc:
        assert readonly is True
        if self.app.open_hook:
            self.app.open_hook()
        raw = self.next_open or FakeRawDoc(path, self.app.events)
        raw.FullName = path
        raw.events = self.app.events
        raw.collection = self
        self.items.append(raw)
        for suffix in self.app.create_on_open:
            Path(path).with_suffix(suffix).write_text("lock", encoding="utf-8")
        return raw


class FakeApp:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.Documents = FakeDocuments(self)
        self.HWND = 1
        self.Visible: Any = None
        self.create_on_open: list[str] = []
        self.quit_error: Exception | None = None
        self.open_hook: Any = None

    def GetAcadState(self) -> FakeState:
        return FakeState()

    def Quit(self) -> None:
        self.events.append("quit")
        if self.quit_error:
            raise self.quit_error


def make_session(app: FakeApp | None = None, **kw: Any) -> acad.AcadSession:
    kw.setdefault("pid", 100)
    kw.setdefault("progid", "AutoCAD.Application.24.3")
    kw.setdefault("owned", True)
    return acad.AcadSession(app or FakeApp(), **kw)


# --- classification and retry ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (FakeComError(-2147418111), "transient"),
        (FakeComError(0x80010001), "transient"),  # unsigned form of the same HRESULT
        (FakeComError(-2147417846), "transient"),
        (FakeComError(-2147352567, "x", (0, "src", "d", None, 0, -2147418111)), "transient"),
        (
            FakeComError(-2147352567, "localized text", (1, "src", "d", None, 0, -2145320924)),
            "permanent",
        ),
        (FakeComError(-2147352565), "permanent"),
        (AttributeError("<unknown>.Quit"), "transient"),
        (AttributeError("'X' object has no attribute 'y'"), "other"),
        (ValueError("boom"), "other"),
        (CadError("X", "y"), "other"),
    ],
)
def test_classify_by_hresult_not_text(exc: BaseException, kind: str) -> None:
    assert acad.classify(exc) == kind


def test_classify_ignores_message_text() -> None:
    # Same words as a transient error but a different HRESULT: must stay permanent.
    assert acad.classify(FakeComError(-2147352567, "Call was rejected by callee")) == "permanent"


def test_retry_recovers_from_transient(clock: Clock) -> None:
    calls = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise FakeComError(-2147418111)
        return "ok"

    assert acad.retry(flaky) == "ok"
    assert clock.sleeps == [0.4, 0.8]


def test_retry_backoff_is_capped_and_exhaustion_is_transient_error(clock: Clock) -> None:
    def always() -> None:
        raise AttributeError("<unknown>.Item")

    with pytest.raises(CadError) as info:
        acad.retry(always, tries=9, base_delay=0.4, max_delay=5.0)
    assert info.value.code == "COM_TRANSIENT"
    assert info.value.exit_code == ExitCode.BUSY
    assert clock.sleeps == [0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0, 5.0]  # 9 tries, 8 waits


def test_retry_permanent_raises_at_once_with_codes(clock: Clock) -> None:
    calls = []

    def bad() -> None:
        calls.append(1)
        raise FakeComError(-2147352567, "msg", (1, "AutoCAD", "d", None, 0, -2145320924))

    with pytest.raises(acad.ComError) as info:
        acad.retry(bad)
    assert len(calls) == 1 and not clock.sleeps
    err = info.value
    assert err.code == "COM_ERROR" and err.hresult == -2147352567 and err.scode == -2145320924
    assert "0x80020009" in err.message and "0x" in err.message


def test_retry_does_not_wrap_non_com_errors(clock: Clock) -> None:
    with pytest.raises(ZeroDivisionError):
        acad.retry(lambda: 1 / 0)
    assert not clock.sleeps


def test_retry_expr_evaluates_the_whole_expression(clock: Clock) -> None:
    state = {"ready": False, "reads": 0}

    class Blocks:
        def Item(self, i: int) -> int:
            return i * 2

    class Doc:
        @property
        def Blocks(self) -> Blocks:
            state["reads"] += 1
            if state["reads"] < 3:
                raise FakeComError(-2147418111)
            return Blocks()

    doc = Doc()
    assert acad.retry_expr(lambda: doc.Blocks.Item(4)) == 8
    assert state["reads"] == 3


def test_retry_passes_args_and_kwargs(clock: Clock) -> None:
    assert acad.retry(lambda a, b=0: a + b, 1, b=2) == 3


def test_backoff_delay() -> None:
    assert [acad.backoff_delay(n, 0.4, 5.0) for n in range(6)] == [0.4, 0.8, 1.6, 3.2, 5.0, 5.0]


# --- platform and discovery -------------------------------------------------------------------


def test_com_raises_missing_dependency_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(CadError) as info:
        acad._com()
    assert info.value.code == "MISSING_DEPENDENCY"
    assert info.value.exit_code == ExitCode.MISSING_DEPENDENCY


def test_com_raises_missing_dependency_without_pywin32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    for name in ("pythoncom", "pywintypes", "win32com", "win32com.client", "win32process"):
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
    with pytest.raises(CadError) as info:
        acad._com()
    assert info.value.code == "MISSING_DEPENDENCY" and "pywin32" in info.value.message


def test_start_off_windows_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(CadError) as info:
        acad.AcadSession.start()
    assert info.value.exit_code == ExitCode.MISSING_DEPENDENCY


def test_module_imports_without_any_win32_module(tmp_path: Path) -> None:
    script = tmp_path / "import_check.py"
    script.write_text(
        "import sys\n"
        "for m in ('pythoncom', 'pywintypes', 'win32com', 'win32com.client', "
        "'win32process', 'winreg'):\n"
        "    sys.modules[m] = None\n"
        "import cadlib.acad as a\n"
        "assert a.discover_progids()\n"
        "try:\n"
        "    a._com()\n"
        "except a.CadError as e:\n"
        "    print(e.code)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(SKILL / "scripts")}
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "MISSING_DEPENDENCY"


def test_discover_progids_orders_versions_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [
        "AutoCAD.Application.23",
        "Other.Thing",
        "AutoCAD.Application.24.3",
        "AutoCAD.Application.24",
        "AutoCAD.Application.9",
        "AutoCAD.Application",
    ]

    class Key:
        def __enter__(self) -> Key:  # noqa: PYI034
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def enum_key(_root: Key, index: int) -> str:
        if index >= len(names):
            raise OSError
        return names[index]

    fake = SimpleNamespace(HKEY_CLASSES_ROOT=0, OpenKey=lambda *a: Key(), EnumKey=enum_key)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    result = acad.discover_progids()
    assert result[:4] == [
        "AutoCAD.Application.24.3",
        "AutoCAD.Application.24",
        "AutoCAD.Application.23",
        "AutoCAD.Application.9",
    ]
    assert result[4:] == [
        "AutoCAD.Application",
        "BricscadApp.AcadApplication",
        "ZWCAD.Application",
        "GStarCAD.Application",
    ]


def test_experimental_hosts() -> None:
    assert not acad.is_experimental("AutoCAD.Application.24.3")
    assert acad.is_experimental("ZWCAD.Application")
    assert acad._image_for("BricscadApp.AcadApplication") == "bricscad.exe"


# --- start: PID attribution ----------------------------------------------------------------------


class LockProbe:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __call__(self) -> contextlib.AbstractContextManager[None]:
        probe = self

        @contextlib.contextmanager
        def cm() -> Iterator[None]:
            probe.entered += 1
            try:
                yield
            finally:
                probe.exited += 1

        return cm()


@pytest.fixture
def start_env(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> SimpleNamespace:
    env = SimpleNamespace(
        app=FakeApp(),
        lock=LockProbe(),
        before={1},
        after={1, 2},
        owner=None,
        created=[],
    )
    monkeypatch.setattr(acad, "_com", lambda: SimpleNamespace())
    monkeypatch.setattr(acad, "_com_lock", env.lock)
    monkeypatch.setattr(acad, "PROGIDS", ["AutoCAD.Application.24.3", "AutoCAD.Application"])
    calls = {"n": 0}

    def list_pids(image: str) -> set[int]:
        calls["n"] += 1
        return set(env.before) if not env.created else set(env.after)

    def create(progid: str) -> tuple[Any, Any]:
        env.created.append(progid)
        return env.app, None

    monkeypatch.setattr(acad, "_list_pids", list_pids)
    monkeypatch.setattr(acad, "_create_app", create)
    monkeypatch.setattr(acad, "_app_pid", lambda app: env.owner)
    env.noted = []
    env.job = (object(), None)
    monkeypatch.setattr(acad, "_note_child", lambda pid, image: env.noted.append((pid, image)))
    monkeypatch.setattr(acad, "_assign_job", lambda pid: env.job)
    return env


def test_start_attributes_the_new_pid(start_env: SimpleNamespace) -> None:
    session = acad.AcadSession.start(timeout=30)
    assert session.pid == 2 and session.owned
    assert start_env.created == ["AutoCAD.Application.24.3"]
    assert start_env.app.Visible is False  # never visible unless asked
    assert start_env.lock.entered == 1 and start_env.lock.exited == 0


def test_start_prefers_window_owner_among_several_new_pids(start_env: SimpleNamespace) -> None:
    start_env.after = {1, 2, 3}
    start_env.owner = 3
    assert acad.AcadSession.start(timeout=30).pid == 3


def test_start_refuses_when_no_new_pid(start_env: SimpleNamespace) -> None:
    start_env.after = {1}  # nothing new: an already running instance was reused
    with pytest.raises(CadError) as info:
        acad.AcadSession.start(timeout=30)
    assert info.value.code == "BUSY" and info.value.exit_code == ExitCode.BUSY
    assert "Quit" not in start_env.app.events and "quit" not in start_env.app.events
    assert start_env.lock.exited == 1  # lock released on refusal


def test_start_refuses_when_window_belongs_to_a_foreign_process(
    start_env: SimpleNamespace,
) -> None:
    start_env.owner = 1  # the window we got is owned by the pre-existing process
    with pytest.raises(CadError) as info:
        acad.AcadSession.start(timeout=30)
    assert info.value.exit_code == ExitCode.BUSY
    assert "quit" not in start_env.app.events


def test_start_refuses_ambiguous_attribution(start_env: SimpleNamespace) -> None:
    start_env.after = {1, 2, 3}
    with pytest.raises(CadError) as info:
        acad.AcadSession.start(timeout=30)
    assert info.value.code == "BUSY"


def test_start_tries_next_progid_and_marks_experimental(
    start_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acad, "PROGIDS", ["Broken.Application", "ZWCAD.Application"])
    original = acad._create_app

    def create(progid: str) -> tuple[Any, Any]:
        if progid == "Broken.Application":
            raise FakeComError(-2147221005)
        return original(progid)

    monkeypatch.setattr(acad, "_create_app", create)
    session = acad.AcadSession.start(timeout=30)
    assert session.progid == "ZWCAD.Application"
    assert any("experimental" in w for w in session.warnings)


def test_start_without_any_backend(
    start_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def create(progid: str) -> tuple[Any, Any]:
        raise FakeComError(-2147221005)

    monkeypatch.setattr(acad, "_create_app", create)
    with pytest.raises(CadError) as info:
        acad.AcadSession.start(timeout=30)
    assert info.value.code == "NO_BACKEND" and info.value.exit_code == ExitCode.MISSING_DEPENDENCY
    assert start_env.lock.exited == 1


def test_attach_guarded_requires_confirmation() -> None:
    with pytest.raises(CadError) as info:
        acad.AcadSession.attach_guarded(confirmed=False)
    assert info.value.exit_code == ExitCode.PRECONDITION_FAILED


def test_attach_guarded_warns_about_unsaved_user_documents(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    dirty = FakeRawDoc("C:/work/user.dwg")
    dirty.Saved = False  # type: ignore[attr-defined]
    app.Documents.items.append(dirty)
    monkeypatch.setattr(acad, "_com", lambda: SimpleNamespace())
    monkeypatch.setattr(acad, "_com_lock", LockProbe())
    monkeypatch.setattr(acad, "_get_active", lambda progid: (app, None))
    monkeypatch.setattr(acad, "_app_pid", lambda a: 77)
    session = acad.AcadSession.attach_guarded(confirmed=True)
    assert not session.owned and session.pid == 77
    assert any("unsaved" in w for w in session.warnings)
    assert app.Visible is None  # never touched
    session.quit()
    assert "quit" not in app.events and not dirty.closed  # the user's session is left alone


# --- quit escalation ---------------------------------------------------------------------------


class Process:
    """Fake process table: alive until the named event happened."""

    def __init__(self, app: FakeApp, dies_on: str | None) -> None:
        self.app = app
        self.dies_on = dies_on
        self.events = app.events

    def alive(self, pid: int, image: str) -> bool:
        if self.dies_on is None:
            return True
        return self.dies_on not in self.events

    def close(self, pid: int) -> None:
        self.events.append("close")

    def kill(self, pid: int) -> None:
        self.events.append("kill")


def patch_process(monkeypatch: pytest.MonkeyPatch, app: FakeApp, dies_on: str | None) -> None:
    proc = Process(app, dies_on)
    monkeypatch.setattr(acad, "_pid_alive", proc.alive)
    monkeypatch.setattr(acad, "_close_pid", proc.close)
    monkeypatch.setattr(acad, "_kill_pid", proc.kill)


@pytest.mark.parametrize(
    ("dies_on", "expected"),
    [
        ("quit", ["quit"]),
        ("close", ["quit", "close"]),
        ("kill", ["quit", "close", "kill"]),
    ],
)
def test_quit_escalation_order(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, dies_on: str, expected: list[str]
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, dies_on)
    session = make_session(app)
    session.quit(timeout=40)
    assert app.events == expected


def test_quit_reports_a_process_that_survives_everything(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, None)
    lock = tmp_path / "x.dwl"
    lock.write_text("lock")
    session = make_session(app)
    session._dwl[lock] = time.time() - 1
    with pytest.raises(CadError) as info:
        session.quit(timeout=20)
    assert info.value.code == "TIMEOUT" and info.value.exit_code == ExitCode.TIMEOUT
    assert app.events == ["quit", "close", "kill"]
    assert lock.exists()  # a live process may still own it


def test_quit_closes_documents_first_and_removes_only_own_lock_files(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    preexisting = drawing.with_suffix(".dwl")
    preexisting.write_text("someone else")
    app.create_on_open = [".dwl2"]
    session = make_session(app)
    session.open(drawing)
    assert drawing.with_suffix(".dwl2").exists()
    session.quit()
    assert app.events == ["doc.close", "quit"]
    assert not drawing.with_suffix(".dwl2").exists()  # created by this session: removed
    assert preexisting.exists()  # was there before: left alone


def test_quit_is_idempotent_and_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    lock = LockProbe()
    import contextlib as cl

    stack = cl.ExitStack()
    stack.enter_context(lock())
    session = make_session(app, stack=stack)
    session.quit()
    session.quit()
    assert app.events == ["quit"] and lock.exited == 1


def test_quit_tolerates_server_gone_during_quit(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    app.quit_error = FakeComError(-2147417848)  # RPC_E_DISCONNECTED: it died while quitting
    patch_process(monkeypatch, app, "quit")
    session = make_session(app)
    session.quit()
    assert not session.warnings


def test_quit_refetches_the_application_object(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    stale, fresh = FakeApp(), FakeApp()
    stale.Quit = lambda: (_ for _ in ()).throw(AttributeError("<unknown>.Quit"))  # type: ignore[method-assign]
    patch_process(monkeypatch, fresh, "quit")
    monkeypatch.setattr(acad, "_wrap", lambda disp: fresh)
    session = make_session(stale, disp=object())
    session.quit()
    assert fresh.events == ["quit"]


def test_unhealthy_session_shortens_the_polite_wait(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "kill")
    session = make_session(app)
    session._unhealthy = True
    session.quit(timeout=90)
    assert app.events == ["quit", "close", "kill"]
    assert clock.t < 90 * 0.5 + 90 * 0.25 + 5  # did not wait the full graceful phase


def test_context_manager_always_quits(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    with pytest.raises(RuntimeError), make_session(app):
        raise RuntimeError("work failed")
    assert app.events == ["quit"]


# --- sysvars ---------------------------------------------------------------------------------------


def test_sysvars_restore_on_exception(clock: Clock) -> None:
    raw = FakeRawDoc("x.dwg")
    session = make_session()
    with pytest.raises(RuntimeError), session.sysvars(raw, ISAVEBAK=0, BACKGROUNDPLOT=0):
        assert raw.vars["ISAVEBAK"] == 0 and raw.vars["BACKGROUNDPLOT"] == 0
        raise RuntimeError("boom")
    assert raw.vars["ISAVEBAK"] == 1 and raw.vars["BACKGROUNDPLOT"] == 2


def test_sysvars_never_touches_filedia(clock: Clock) -> None:
    raw = FakeRawDoc("x.dwg")
    with pytest.raises(CadError) as info, make_session().sysvars(raw, FileDia=0):
        pass
    assert info.value.exit_code == ExitCode.BAD_ARGS and raw.vars["FILEDIA"] == 1


def test_sysvars_unchanged_value_is_not_written(clock: Clock) -> None:
    raw = FakeRawDoc("x.dwg")
    sets: list[tuple[str, Any]] = []
    original = raw.SetVariable
    raw.SetVariable = lambda n, v: (sets.append((n, v)), original(n, v))[1]  # type: ignore[method-assign]
    with make_session().sysvars(raw, ISAVEBAK=1):
        pass
    assert sets == []


def test_sysvars_restore_failure_is_reported_not_raised(clock: Clock) -> None:
    raw = FakeRawDoc("x.dwg")
    session = make_session()
    original = raw.SetVariable

    def set_variable(name: str, value: Any) -> None:
        if value == 1:  # the restore
            raise FakeComError(-2147352567)
        original(name, value)

    raw.SetVariable = set_variable  # type: ignore[method-assign]
    with session.sysvars(raw, ISAVEBAK=0):
        pass
    assert any("could not restore ISAVEBAK" in w for w in session.warnings)


# --- documents -------------------------------------------------------------------------------------


def test_open_refuses_a_document_the_user_has_open(clock: Clock, tmp_path: Path) -> None:
    drawing = tmp_path / "user.dwg"
    drawing.write_text("x")
    app = FakeApp()
    app.Documents.items.append(FakeRawDoc(str(drawing)))
    session = make_session(app)
    with pytest.raises(CadError) as info:
        session.open(drawing)
    assert info.value.code == "DOC_OPEN_BY_USER" and info.value.exit_code == ExitCode.BUSY
    assert session.documents == []


def test_open_ignores_unsaved_untitled_documents(clock: Clock, tmp_path: Path) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app = FakeApp()
    app.Documents.items.append(FakeRawDoc("Drawing1.dwg"))  # the new instance's blank document
    doc = make_session(app).open(drawing)
    assert doc.path == drawing


def test_open_missing_file(clock: Clock, tmp_path: Path) -> None:
    with pytest.raises(CadError) as info:
        make_session().open(tmp_path / "nope.dwg")
    assert info.value.code == "NOT_FOUND"


def test_doc_registry_cleanup_and_reopen(clock: Clock, tmp_path: Path) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    session = make_session()
    doc = session.open(drawing)
    assert session.documents == [doc]
    with pytest.raises(CadError) as info:
        session.open(drawing)
    assert info.value.code in {"DOC_ALREADY_OPEN", "DOC_OPEN_BY_USER"}
    doc.close()
    doc.close()  # idempotent
    assert session.documents == [] and doc.raw.closed


def test_failed_close_keeps_doc_registered_and_quit_escalates(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app = FakeApp()
    patch_process(monkeypatch, app, "kill")
    raw = FakeRawDoc(str(drawing))
    raw.close_error = FakeComError(-2147352567)
    app.Documents.next_open = raw
    session = make_session(app)
    session.open(drawing)
    session.quit(timeout=40)
    assert any("could not close" in w for w in session.warnings)
    assert app.events[-3:] == ["quit", "close", "kill"]


# --- pure helpers ----------------------------------------------------------------------------------

MEDIA = [
    "ISO_A4_(210.00_x_297.00_MM)",
    "ISO_A4_(297.00_x_210.00_MM)",
    "ISO_A3_(297.00_x_420.00_MM)",
    "ISO_A3_(420.00_x_297.00_MM)",
    "ISO_full_bleed_A3_(420.00_x_297.00_MM)",
    "ANSI_A_(8.50_x_11.00_INCHES)",
    "UserDefinedMetric_(420.00_x_297.00_MM)",
]


def test_parse_media_mm() -> None:
    assert acad.parse_media_mm("ISO_A3_(420.00_x_297.00_MM)") == (420.0, 297.0)
    size = acad.parse_media_mm("ANSI_A_(8.50_x_11.00_INCHES)")
    assert size is not None and abs(size[0] - 215.9) < 0.01 and abs(size[1] - 279.4) < 0.01
    assert acad.parse_media_mm("Custom") is None


def test_closest_iso_media_keeps_orientation_and_prefers_regular() -> None:
    assert acad.closest_iso_media(MEDIA, 420, 297) == "ISO_A3_(420.00_x_297.00_MM)"
    assert acad.closest_iso_media(MEDIA, 297, 420) == "ISO_A3_(297.00_x_420.00_MM)"
    assert acad.closest_iso_media(MEDIA, 418, 295) == "ISO_A3_(420.00_x_297.00_MM)"
    assert acad.closest_iso_media(MEDIA, 200, 290) == "ISO_A4_(210.00_x_297.00_MM)"
    assert acad.closest_iso_media(["ANSI_A_(8.50_x_11.00_INCHES)"], 210, 297) is None


def test_sizes_match_either_orientation_with_tolerance() -> None:
    assert acad.sizes_match((420.0, 297.0), (297.0, 420.0))
    assert acad.sizes_match((419.0, 296.5), (420.0, 297.0))
    assert not acad.sizes_match((210.0, 297.0), (420.0, 297.0))


def test_temp_sibling_is_unique_and_beside_destination(tmp_path: Path) -> None:
    dst = tmp_path / "out.pdf"
    a, b = acad._temp_sibling(dst, ".pdf"), acad._temp_sibling(dst, ".pdf")
    assert a != b and a.parent == tmp_path and a.suffix == ".pdf"


def test_verify_fresh_rejects_missing_empty_and_stale(tmp_path: Path) -> None:
    started = time.time()
    with pytest.raises(CadError):
        acad._verify_fresh(tmp_path / "none.pdf", started)
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(CadError) as info:
        acad._verify_fresh(empty, started)
    assert info.value.code == "PLOT_BAD_OUTPUT"
    old = tmp_path / "old.pdf"
    old.write_bytes(b"%PDF-1.4")
    os.utime(old, (started - 3600, started - 3600))
    with pytest.raises(CadError) as info:
        acad._verify_fresh(old, started)
    assert info.value.code == "STALE_OUTPUT"
    fresh = tmp_path / "fresh.pdf"
    fresh.write_bytes(b"%PDF-1.4")
    acad._verify_fresh(fresh, started)


def _dxf_with_viewports(path: Path, statuses: list[int]) -> None:
    rows = ["0", "SECTION", "2", "ENTITIES"]
    for index, status in enumerate(statuses):
        rows += ["0", "VIEWPORT", "5", f"A{index}", "68", str(status), "69", str(index + 1)]
    rows += ["0", "ENDSEC", "0", "EOF"]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_scan_viewports(tmp_path: Path) -> None:
    path = tmp_path / "v.dxf"
    _dxf_with_viewports(path, [1, 2, 0, -1])
    found = acad.scan_viewports(path)
    assert found is not None
    assert [(v["id"], v["status"]) for v in found] == [(1, 1), (2, 2), (3, 0), (4, -1)]


def test_scan_viewports_skips_binary_dxf(tmp_path: Path) -> None:
    path = tmp_path / "b.dxf"
    path.write_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 20)
    assert acad.scan_viewports(path) is None


def test_scan_viewports_on_generated_fixture(fixtures_dir: Path) -> None:
    found = acad.scan_viewports(fixtures_dir / "sheet_set.dxf")
    assert found is not None and len(found) >= 2
    assert all(v["status"] is not None for v in found)


# --- export and plot flows (fake documents) ------------------------------------------------------


class FakeLayout:
    def __init__(self, name: str, config: str = "None", size: tuple[float, float] = (420, 297)):
        self.Name = name
        self.ConfigName = config
        self._size = size
        self.PaperUnits = 1
        self.CanonicalMediaName = "None"
        self.refreshed = 0
        self.devices = ("DWG To PDF.pc3", "Some Printer")
        self.media = tuple(MEDIA)
        self.set_calls: list[tuple[str, Any]] = []

    def GetPaperSize(self) -> tuple[float, float]:
        return self._size

    def RefreshPlotDeviceInfo(self) -> None:
        self.refreshed += 1

    def GetPlotDeviceNames(self) -> tuple[str, ...]:
        return self.devices

    def GetCanonicalMediaNames(self) -> tuple[str, ...]:
        return self.media

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"ConfigName", "CanonicalMediaName", "PlotRotation", "StyleSheet"}:
            self.__dict__.setdefault("set_calls", []).append((name, value))
        object.__setattr__(self, name, value)


def test_configure_plot_falls_back_to_pdf_device_with_closest_media(clock: Clock) -> None:
    lay = FakeLayout("Sheet-A", config="None", size=(297, 210))
    warnings: list[str] = []
    media = make_session()._configure_plot(lay, {}, warnings)
    assert lay.ConfigName == "DWG To PDF.pc3"
    assert lay.CanonicalMediaName == "ISO_A4_(297.00_x_210.00_MM)"
    assert media == (297.0, 210.0)
    assert any("no plotter" in w for w in warnings) and any("media" in w for w in warnings)


def test_configure_plot_keeps_a_working_pdf_setup(clock: Clock) -> None:
    lay = FakeLayout("Sheet-A", config="DWG To PDF.pc3")
    lay.CanonicalMediaName = "ISO_A3_(420.00_x_297.00_MM)"
    lay.set_calls.clear()
    warnings: list[str] = []
    media = make_session()._configure_plot(lay, {}, warnings)
    # regression found on a real drawing: RefreshPlotDeviceInfo() resets the layout's plot setup to
    # the device defaults and the PDF came out (almost) empty; it is only needed after a device change
    assert lay.refreshed == 0
    assert lay.set_calls == [] and warnings == [] and media == (420.0, 297.0)


def test_configure_plot_replaces_a_non_pdf_device(clock: Clock) -> None:
    lay = FakeLayout("Sheet-A", config="Some Printer")
    warnings: list[str] = []
    make_session()._configure_plot(lay, {}, warnings)
    assert lay.ConfigName == "DWG To PDF.pc3"


def test_configure_plot_explicit_page_setup(clock: Clock) -> None:
    lay = FakeLayout("Sheet-A")
    media = make_session()._configure_plot(
        lay,
        {"device": "DWG To PDF.pc3", "media": "ISO_A4_(210.00_x_297.00_MM)", "rotation": 90},
        [],
    )
    assert media == (210.0, 297.0)
    assert ("PlotRotation", 1) in lay.set_calls


@pytest.mark.parametrize(
    "setup",
    [{"device": "Missing.pc3"}, {"media": "ISO_A0_(841.00_x_1189.00_MM)"}],
)
def test_configure_plot_rejects_unknown_device_or_media(
    clock: Clock, setup: dict[str, Any]
) -> None:
    with pytest.raises(CadError) as info:
        make_session()._configure_plot(FakeLayout("Sheet-A"), setup, [])
    assert info.value.exit_code == ExitCode.PRECONDITION_FAILED and info.value.hint


def test_configure_plot_without_pdf_plotter_installed(clock: Clock) -> None:
    lay = FakeLayout("Sheet-A")
    lay.devices = ("Some Printer",)
    with pytest.raises(CadError) as info:
        make_session()._configure_plot(lay, {}, [])
    assert "DWG To PDF.pc3" in info.value.message


def plot_session(
    tmp_path: Path, plot_to_file: Any, layout: FakeLayout | None = None
) -> tuple[acad.AcadSession, FakeApp, FakeRawDoc, Path]:
    drawing = tmp_path / "sheet.dwg"
    drawing.write_text("x")
    lay = layout or FakeLayout("Sheet-A")
    app = FakeApp()
    raw = FakeRawDoc(str(drawing), app.events)
    raw.layouts = SimpleNamespace(Count=2, Item=lambda i: [SimpleNamespace(Name="Model"), lay][i])
    raw.Plot = SimpleNamespace(QuietErrorMode=False, PlotToFile=plot_to_file)
    app.Documents.next_open = raw
    return make_session(app), app, raw, drawing


def test_plot_false_return_is_an_error_and_cleans_up(clock: Clock, tmp_path: Path) -> None:
    session, _app, raw, drawing = plot_session(tmp_path, lambda p: False)
    dst = tmp_path / "out.pdf"
    with pytest.raises(CadError) as info:
        session.plot_layout_pdf(drawing, "sheet-a", dst)
    assert info.value.code == "PLOT_FAILED"
    assert raw.closed and session.documents == []
    assert raw.vars["BACKGROUNDPLOT"] == 2 and raw.vars["ISAVEBAK"] == 1  # restored
    assert not dst.exists() and not list(tmp_path.glob("*.part.*"))


def test_plot_writes_straight_to_the_final_name_and_warns_about_the_viewer(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    targets: list[str] = []

    def plot(path: str) -> bool:
        targets.append(path)
        Path(path).write_bytes(b"%PDF-1.7 fake")
        return True

    monkeypatch.setattr(acad, "_pdf_info", lambda p: (1, (420.0, 297.0)))
    session, _app, raw, drawing = plot_session(tmp_path, plot)
    dst = tmp_path / "out.pdf"
    warnings = session.plot_layout_pdf(drawing, "Sheet-A", dst)
    # no temporary name that a viewer could be started on and that is then moved away
    assert targets == [str(dst)] and dst.read_bytes().startswith(b"%PDF-1.7")
    assert raw.closed and raw.ctab_history == ["Sheet-A"]
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".pdf"] == ["out.pdf"]
    assert any("no plotter" in w for w in warnings)
    assert acad.VIEWER_WARNING in warnings
    assert raw.Plot.QuietErrorMode is True


def test_plot_refuses_an_existing_destination(clock: Clock, tmp_path: Path) -> None:
    session, _app, raw, drawing = plot_session(tmp_path, lambda p: True)
    dst = tmp_path / "out.pdf"
    dst.write_bytes(b"%PDF-1.4 OLD")
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(drawing, "Sheet-A", dst)
    assert err.value.code == "DEST_EXISTS" and dst.read_bytes() == b"%PDF-1.4 OLD"
    assert session.documents == [] and not raw.closed  # nothing was even opened


@pytest.mark.parametrize(
    ("info", "code"),
    [((2, (420.0, 297.0)), "PLOT_BAD_OUTPUT"), ((1, (210.0, 297.0)), "PLOT_BAD_OUTPUT")],
)
def test_plot_rejects_wrong_page_count_or_size(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    tmp_path: Path,
    info: tuple[int, tuple[float, float]],
    code: str,
) -> None:
    def plot(path: str) -> bool:
        Path(path).write_bytes(b"%PDF-1.7 fake")
        return True

    monkeypatch.setattr(acad, "_pdf_info", lambda p: info)
    session, _app, raw, drawing = plot_session(tmp_path, plot)
    dst = tmp_path / "out.pdf"
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(drawing, "Sheet-A", dst)
    assert err.value.code == code and not dst.exists() and raw.closed


def test_plot_rejects_non_pdf_output(clock: Clock, tmp_path: Path) -> None:
    def plot(path: str) -> bool:
        Path(path).write_bytes(b"not a pdf")
        return True

    session, _app, raw, drawing = plot_session(tmp_path, plot)
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(drawing, "Sheet-A", tmp_path / "out.pdf")
    assert err.value.code == "PLOT_BAD_OUTPUT" and raw.closed


def test_plot_unknown_layout_lists_alternatives_and_closes_doc(
    clock: Clock, tmp_path: Path
) -> None:
    session, _app, raw, drawing = plot_session(tmp_path, lambda p: True)
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(drawing, "Nope", tmp_path / "out.pdf")
    assert err.value.code == "LAYOUT_NOT_FOUND" and "Sheet-A" in (err.value.hint or "")
    assert raw.closed


def test_plot_refuses_model_space_and_unknown_page_setup_keys(clock: Clock, tmp_path: Path) -> None:
    session, *_ = plot_session(tmp_path, lambda p: True)
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(tmp_path / "sheet.dwg", "Model", tmp_path / "o.pdf")
    assert err.value.exit_code == ExitCode.PRECONDITION_FAILED
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(
            tmp_path / "sheet.dwg", "Sheet-A", tmp_path / "o.pdf", page_setup={"bogus": 1}
        )
    assert err.value.exit_code == ExitCode.BAD_ARGS


def test_plot_close_failure_marks_session_unhealthy(clock: Clock, tmp_path: Path) -> None:
    session, _app, raw, drawing = plot_session(tmp_path, lambda p: False)
    raw.close_error = FakeComError(-2147352567)
    with pytest.raises(CadError) as err:
        session.plot_layout_pdf(drawing, "Sheet-A", tmp_path / "out.pdf")
    assert err.value.code == "PLOT_FAILED"  # the real error is not masked by the close error
    assert session._unhealthy


def export_session(
    tmp_path: Path, content: str | None
) -> tuple[acad.AcadSession, FakeRawDoc, Path]:
    drawing = tmp_path / "sheet.dwg"
    drawing.write_text("x")
    app = FakeApp()
    raw = FakeRawDoc(str(drawing), app.events)
    raw.saveas_writes = content
    names = ["Model", "Sheet-A", "Sheet-B"]
    raw.layouts = SimpleNamespace(Count=3, Item=lambda i: SimpleNamespace(Name=names[i]))
    raw.vars["CTAB"] = "Sheet-A"
    app.Documents.next_open = raw
    return make_session(app), raw, drawing


def test_export_dxf_activates_every_layout_and_restores_the_tab(
    clock: Clock, tmp_path: Path
) -> None:
    dxf = tmp_path / "v.dxf"
    _dxf_with_viewports(dxf, [1, 2])
    session, raw, drawing = export_session(tmp_path, dxf.read_text("utf-8"))
    dst = tmp_path / "out.dxf"
    warnings = session.export_dxf(drawing, dst)
    assert raw.ctab_history == ["Sheet-A", "Sheet-B", "Sheet-A"]
    assert raw.events.index("activate") < raw.events.index("saveas:61")
    assert warnings == [] and dst.exists() and raw.closed
    assert raw.vars["ISAVEBAK"] == 1  # restored
    assert not list(tmp_path.glob("*.part.*"))


def test_export_dxf_warns_when_viewport_status_is_unreliable(clock: Clock, tmp_path: Path) -> None:
    dxf = tmp_path / "v.dxf"
    _dxf_with_viewports(dxf, [1, 0, 0])
    session, _raw, drawing = export_session(tmp_path, dxf.read_text("utf-8"))
    warnings = session.export_dxf(drawing, tmp_path / "out.dxf")
    assert len(warnings) == 1 and "2 viewport(s)" in warnings[0]


def test_export_dxf_without_activation_skips_ctab(clock: Clock, tmp_path: Path) -> None:
    session, raw, drawing = export_session(tmp_path, "0\nEOF\n")
    session.export_dxf(drawing, tmp_path / "out.dxf", activate_layouts=False)
    assert raw.ctab_history == []


def test_export_dxf_that_writes_nothing_leaves_the_old_destination(
    clock: Clock, tmp_path: Path
) -> None:
    session, raw, drawing = export_session(tmp_path, None)
    dst = tmp_path / "out.dxf"
    dst.write_text("OLD")
    with pytest.raises(CadError) as err:
        session.export_dxf(drawing, dst)
    assert err.value.code == "STALE_OUTPUT" and dst.read_text() == "OLD" and raw.closed


def test_export_dxf_rejects_unknown_version(clock: Clock, tmp_path: Path) -> None:
    session, *_ = export_session(tmp_path, "")
    with pytest.raises(CadError) as err:
        session.export_dxf(tmp_path / "sheet.dwg", tmp_path / "o.dxf", version="1999")
    assert err.value.exit_code == ExitCode.BAD_ARGS


def test_save_dwg_closes_the_document_before_moving(clock: Clock, tmp_path: Path) -> None:
    session, raw, drawing = export_session(tmp_path, "DWG")
    doc = session.open(drawing)
    dst = tmp_path / "copy.dwg"
    session.save_dwg(doc, dst)
    assert raw.events.index("saveas:60") < raw.events.index("doc.close")
    assert dst.read_text() == "DWG" and session.documents == []


# --- COM (real CAD) ------------------------------------------------------------------------------


def _cad_installed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "AutoCAD.Application"))
    except OSError:
        return False
    return True


@pytest.mark.com
@pytest.mark.skipif(not _cad_installed(), reason="no AutoCAD COM server registered")
def test_com_session_end_to_end(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One instance: DWG made from a fixture, DXF export, viewport check, plots, quit."""
    import ezdxf
    import pypdfium2

    out = Path(os.environ.get("CAD_DRAWINGS_COM_OUT", tmp_path)) / "com-test"
    out.mkdir(parents=True, exist_ok=True)
    image = "acad.exe"
    if acad._list_pids(image):
        pytest.skip("another acad.exe is running; refusing to touch it")
    names = ("ISAVEBAK", "ISAVEPERCENT", "BACKGROUNDPLOT", "FILEDIA", "CTAB")
    produced: list[Path] = []
    session = acad.AcadSession.start(timeout=120)
    pid = session.pid
    try:
        assert pid in acad._list_pids(image)
        # a DWG for the tests, created through our own session
        dwg = out / "sheet_set.dwg"
        session.save_dwg(session.open(fixtures_dir / "sheet_set.dxf"), dwg)
        produced.append(dwg)

        def read_vars() -> dict[str, Any]:
            with session.open(dwg) as doc:
                return {n: doc.raw.GetVariable(n) for n in names}

        before = read_vars()

        # the user's own document is refused (simulated: opened behind the session's back)
        user_dwg = out / "user_copy.dwg"
        user_dwg.write_bytes(dwg.read_bytes())
        produced.append(user_dwg)
        session._app_call(lambda a: a.Documents.Open(str(user_dwg), True))
        with pytest.raises(CadError) as refusal:
            session.open(user_dwg)
        assert refusal.value.code == "DOC_OPEN_BY_USER"
        session._app_call(lambda a: a.Documents.Item(1).Close(False))

        # export with activated layouts: both viewports on, frozen layer intact
        dxf = out / "sheet_set_export.dxf"
        warnings = session.export_dxf(dwg, dxf)
        produced.append(dxf)
        assert warnings == []
        drawing = ezdxf.readfile(dxf)
        frozen = {}
        for layout in drawing.layouts:
            if layout.name == "Model":
                continue
            viewports = list(layout.viewports())
            assert viewports and all(vp.dxf.status > 0 for vp in viewports), layout.name
            frozen[layout.name] = {n for vp in viewports for n in vp.frozen_layers}
        assert not frozen["Sheet-A"] and frozen["Sheet-B"]

        # plots: one single-page PDF each, A3 landscape from the built-in PDF plotter
        for name in ("Sheet-A", "Sheet-B"):
            pdf = out / f"{name}.pdf"
            started = time.time()
            session.plot_layout_pdf(dwg, name, pdf)
            produced.append(pdf)
            assert pdf.stat().st_mtime >= started - 2 and pdf.stat().st_size > 0
            doc = pypdfium2.PdfDocument(str(pdf))
            try:
                assert len(doc) == 1
                width, height = doc[0].get_size()
            finally:
                doc.close()
            assert acad.sizes_match((width / 72 * 25.4, height / 72 * 25.4), (420.0, 297.0))

        # an error path leaves nothing open
        with pytest.raises(CadError):
            session.plot_layout_pdf(dwg, "No-such-layout", out / "x.pdf")
        assert session.documents == []

        assert read_vars() == before  # system variables restored, FILEDIA untouched
    finally:
        try:
            session.quit()
        finally:
            for path in produced:
                with contextlib.suppress(OSError):
                    path.unlink()
    assert not acad._list_pids(image), "an acad.exe was left running"
    assert pid not in acad._list_pids(image)
    assert not list(out.glob("*.dwl*")) and not list(out.glob("*.part.*"))


# --- hardening: orphan protection, watchdog, lock ownership, messages ----------------------------


def test_start_default_timeout_is_90() -> None:
    import inspect

    params = inspect.signature(acad.AcadSession.start).parameters
    assert params["timeout"].default == 90.0 and params["op_timeout"].default == 90.0


def test_start_records_child_and_ties_it_to_a_job(start_env: SimpleNamespace) -> None:
    session = acad.AcadSession.start(timeout=30)
    assert start_env.noted == [(2, "acad.exe")]
    assert session._job is start_env.job[0] and not session.warnings


def test_start_survives_a_refused_job_assignment(start_env: SimpleNamespace) -> None:
    start_env.job = (None, "the CAD process could not be tied to this process (job object: x)")
    session = acad.AcadSession.start(timeout=30)
    assert session.pid == 2 and session._job is None
    assert any("could not be tied" in w for w in session.warnings)


def test_start_tolerates_missing_or_failing_bookkeeping(
    start_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(pid: int, image: str) -> None:
        raise ImportError("cannot import name 'note_child'")

    monkeypatch.setattr(acad, "_note_child", missing)
    assert not acad.AcadSession.start(timeout=30).warnings

    def failing(pid: int, image: str) -> None:
        raise OSError("disk")

    start_env.created.clear()  # a fresh start sees the original process table
    monkeypatch.setattr(acad, "_note_child", failing)
    session = acad.AcadSession.start(timeout=30)
    assert any("could not record" in w for w in session.warnings)


class FakeJob:
    def __init__(self) -> None:
        self.closed = False

    def Close(self) -> None:
        self.closed = True


def test_job_is_released_only_once_the_process_is_gone(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    session = make_session(app)
    session._job = job = FakeJob()
    session.quit()
    assert job.closed

    app2 = FakeApp()
    patch_process(monkeypatch, app2, None)  # survives everything
    session2 = make_session(app2)
    session2._job = job2 = FakeJob()
    with pytest.raises(CadError):
        session2.quit(timeout=20)
    assert not job2.closed  # closing it would kill the process; python exit does that


def test_assign_job_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    job, reason = acad._assign_job(2**31 - 5)  # no such process: refused or unsupported
    assert job is None and reason and "could not be tied" in reason


def test_attribution_failure_names_the_hidden_instance(start_env: SimpleNamespace) -> None:
    start_env.after = {1, 2, 3}
    with pytest.raises(CadError) as info:
        acad.AcadSession.start(timeout=30)
    err = info.value
    assert "still running" in err.message and "2, 3" in err.message
    assert "Task Manager" in (err.hint or "") and err.candidate_pids == [2, 3]  # type: ignore[attr-defined]
    assert err.exit_code == ExitCode.BUSY


def test_attribution_with_no_new_process_says_nothing_was_started(
    start_env: SimpleNamespace,
) -> None:
    start_env.after = {1}
    with pytest.raises(CadError) as info:
        acad.AcadSession.start(timeout=30)
    assert "left alone" in info.value.message and "still running" not in info.value.message


class Watched:
    """Process table for watchdog tests: alive until killed; records every PID it is asked about."""

    def __init__(self, app: FakeApp, foreign: int = 999) -> None:
        self.app = app
        self.unblock = threading.Event()
        self.killed = False
        self.calls: list[tuple[str, int]] = []
        self.foreign = foreign

    def alive(self, pid: int, image: str) -> bool:
        return not self.killed

    def close(self, pid: int) -> None:
        self.calls.append(("close", pid))

    def kill(self, pid: int) -> None:
        self.calls.append(("kill", pid))
        self.killed = True
        self.unblock.set()  # the blocked COM call fails once its server dies


def block_until_killed(watched: Watched) -> Any:
    def hook() -> None:
        assert watched.unblock.wait(10), "watchdog never fired"
        raise FakeComError(-2147417848)  # RPC_E_DISCONNECTED

    return hook


def test_watchdog_terminates_only_own_pid_in_escalation_order(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app = FakeApp()
    watched = Watched(app)
    app.open_hook = block_until_killed(watched)
    monkeypatch.setattr(acad, "_pid_alive", watched.alive)
    monkeypatch.setattr(acad, "_close_pid", watched.close)
    monkeypatch.setattr(acad, "_kill_pid", watched.kill)
    session = make_session(app, op_timeout=0.05)
    with pytest.raises(CadError) as info:
        session.open(drawing)
    assert info.value.code == "TIMEOUT" and info.value.exit_code == ExitCode.TIMEOUT
    assert watched.calls == [("close", 100), ("kill", 100)]  # own pid only, graceful first
    assert all(pid != watched.foreign for _, pid in watched.calls)
    assert session._dead and any("terminating pid 100" in w for w in session.warnings)
    with pytest.raises(CadError) as again:  # a dead session fails fast, never touching COM
        session.open(drawing)
    assert again.value.code == "TIMEOUT"
    session.quit()  # no Quit call on a dead process, no hang
    assert "quit" not in app.events and session.documents == []


def test_watchdog_stays_quiet_when_the_call_finishes_in_time(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app = FakeApp()
    watched = Watched(app)
    monkeypatch.setattr(acad, "_pid_alive", watched.alive)
    monkeypatch.setattr(acad, "_close_pid", watched.close)
    monkeypatch.setattr(acad, "_kill_pid", watched.kill)
    session = make_session(app, op_timeout=5.0)
    session.open(drawing)
    assert watched.calls == [] and not session._dead


def test_watchdog_never_terminates_a_process_it_did_not_start(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app = FakeApp()
    watched = Watched(app)
    app.open_hook = lambda: time.sleep(0.2)  # slower than op_timeout
    monkeypatch.setattr(acad, "_pid_alive", watched.alive)
    monkeypatch.setattr(acad, "_close_pid", watched.close)
    monkeypatch.setattr(acad, "_kill_pid", watched.kill)
    session = make_session(app, owned=False, op_timeout=0.05)
    session.open(drawing)
    assert watched.calls == [] and not session._dead


def test_watchdog_does_not_kill_a_process_that_already_exited(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    watched = Watched(app)
    watched.killed = True  # gone before the watchdog acts (and its PID may be reused)
    monkeypatch.setattr(acad, "_pid_alive", watched.alive)
    monkeypatch.setattr(acad, "_close_pid", watched.close)
    monkeypatch.setattr(acad, "_kill_pid", watched.kill)
    session = make_session(app)
    assert session._terminate_own() is True and watched.calls == []


def test_dwl_that_appeared_after_open_is_not_ours(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    session = make_session(app)
    session.open(drawing)
    elsewhere = drawing.with_suffix(".dwl")  # the user opens the original in another session
    elsewhere.write_text("user")
    session.quit()
    assert elsewhere.exists()


def test_dwl_with_an_older_mtime_than_the_open_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app.create_on_open = [".dwl2"]
    session = make_session(app)
    session.open(drawing)
    lock = drawing.with_suffix(".dwl2")
    assert lock in session._dwl
    old = time.time() - 3600
    os.utime(lock, (old, old))  # replaced by something older than our open: not the file we saw
    session.quit()
    assert lock.exists()


def test_dwl_created_by_open_is_removed(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    app.create_on_open = [".dwl", ".dwl2"]
    session = make_session(app)
    session.open(drawing)
    session.quit()
    assert not drawing.with_suffix(".dwl").exists() and not drawing.with_suffix(".dwl2").exists()


def test_doc_export_dxf_leaves_the_document_open(clock: Clock, tmp_path: Path) -> None:
    dxf = tmp_path / "v.dxf"
    _dxf_with_viewports(dxf, [1, 2])
    session, raw, drawing = export_session(tmp_path, dxf.read_text("utf-8"))
    doc = session.open(drawing)
    dst = tmp_path / "direct.dxf"
    assert doc.export_dxf(dst) == []
    assert dst.exists() and not raw.closed and session.documents == [doc]
    assert raw.ctab_history == ["Sheet-A", "Sheet-B", "Sheet-A"]
    with pytest.raises(CadError) as err:  # never overwrites
        doc.export_dxf(dst)
    assert err.value.code == "DEST_EXISTS"
    doc.close()


def test_doc_plot_leaves_the_document_open(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    def plot(path: str) -> bool:
        Path(path).write_bytes(b"%PDF-1.7 fake")
        return True

    monkeypatch.setattr(acad, "_pdf_info", lambda p: (1, (420.0, 297.0)))
    session, _app, raw, drawing = plot_session(tmp_path, plot)
    doc = session.open(drawing)
    warnings = doc.plot_layout_pdf("Sheet-A", tmp_path / "direct.pdf")
    assert acad.VIEWER_WARNING in warnings and not raw.closed
    assert session.documents == [doc]
    with pytest.raises(CadError) as err:
        doc.plot_layout_pdf("Model", tmp_path / "m.pdf")
    assert err.value.exit_code == ExitCode.PRECONDITION_FAILED


@pytest.mark.parametrize(
    "call",
    [
        lambda: acad._parse_scale("x"),
        lambda: acad._parse_scale("0:5"),
        lambda: make_session()._apply_page_options(FakeLayout("L"), {"rotation": 45}),
        lambda: make_session()._apply_page_options(FakeLayout("L"), {"plot_area": "moon"}),
        lambda: acad.AcadSession.save_dwg(make_session(), None, Path("x"), "1999"),  # type: ignore[arg-type]
    ],
)
def test_bad_arguments_carry_the_bad_args_exit_code(call: Any) -> None:
    with pytest.raises(CadError) as err:
        call()
    assert err.value.code == "BAD_ARGS" and err.value.exit_code == ExitCode.BAD_ARGS


def test_doc_close_with_save_is_a_bad_arg(clock: Clock, tmp_path: Path) -> None:
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    doc = make_session().open(drawing)
    with pytest.raises(CadError) as err:
        doc.close(save=True)
    assert err.value.exit_code == ExitCode.BAD_ARGS


# --- COM: orphan protection and the watchdog on a real instance ----------------------------------

_CHILD = """
import json, sys, time
sys.path.insert(0, {scripts!r})
from cadlib import acad
s = acad.AcadSession.start()
print(json.dumps({{"pid": s.pid, "warnings": s.warnings}}), flush=True)
time.sleep(600)
"""


def _wait_gone(pid: int, image: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pid not in acad._list_pids(image):
            return True
        time.sleep(1)
    return pid not in acad._list_pids(image)


@pytest.mark.com
@pytest.mark.skipif(not _cad_installed(), reason="no AutoCAD COM server registered")
def test_com_cad_dies_with_its_python_process(tmp_path: Path) -> None:
    """Hard-kill the Python process that owns a session: the CAD process must vanish."""
    image = "acad.exe"
    if acad._list_pids(image):
        pytest.skip("another acad.exe is running; refusing to touch it")
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(scripts=str(SKILL / "scripts")), encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True, env=os.environ.copy()
    )
    cad_pid = None
    try:
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line, "child did not start a session"
        info = json.loads(line)
        cad_pid = info["pid"]
        assert cad_pid in acad._list_pids(image)
        assert not any("could not be tied" in w for w in info["warnings"]), info["warnings"]
        subprocess.run(["taskkill", "/PID", str(child.pid), "/F"], capture_output=True, check=False)
        child.wait(timeout=30)
        assert _wait_gone(cad_pid, image, 30), f"CAD pid {cad_pid} outlived its Python process"
    finally:
        if child.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/F"], capture_output=True, check=False
            )
        if cad_pid is not None and cad_pid in acad._list_pids(image):
            acad._kill_pid(cad_pid)  # our own pid only


@pytest.mark.com
@pytest.mark.skipif(not _cad_installed(), reason="no AutoCAD COM server registered")
def test_com_watchdog_terminates_own_instance_when_a_call_blocks(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """A 1 ms deadline on Documents.Open makes the watchdog fire on a real call."""
    image = "acad.exe"
    if acad._list_pids(image):
        pytest.skip("another acad.exe is running; refusing to touch it")
    session = acad.AcadSession.start(timeout=120)
    pid = session.pid
    try:
        session.op_timeout = 0.001
        with pytest.raises(CadError) as err:
            session.open(fixtures_dir / "sheet_set.dxf")
        assert err.value.code == "TIMEOUT" and err.value.exit_code == ExitCode.TIMEOUT
        assert pid not in acad._list_pids(image)
    finally:
        session.quit()
    assert not acad._list_pids(image)


def _pdf_file(tmp_path: Path) -> Path:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"%PDF-1.7 fake")
    return path


def test_verify_pdf_rejects_a_page_with_almost_no_drawing_content(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acad, "_pdf_info", lambda p: (1, (420.0, 297.0)))
    monkeypatch.setattr(acad, "_pdf_object_count", lambda p: 3)
    with pytest.raises(CadError) as err:
        make_session()._verify_pdf(_pdf_file(tmp_path), (420.0, 297.0))
    assert err.value.code == "PLOT_BAD_OUTPUT" and "content" in err.value.message


def test_verify_pdf_accepts_a_page_with_content_and_an_unknown_count(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acad, "_pdf_info", lambda p: (1, (420.0, 297.0)))
    for count in (5000, None):  # None: the count could not be determined, do not fail on that
        monkeypatch.setattr(acad, "_pdf_object_count", lambda p, c=count: c)
        assert make_session()._verify_pdf(_pdf_file(tmp_path), (420.0, 297.0)) == []


def test_pdf_object_count_is_none_for_a_file_it_cannot_read(tmp_path: Path) -> None:
    assert acad._pdf_object_count(_pdf_file(tmp_path)) is None


# --- closing documents whose first COM proxy has gone stale ---------------------------------------


def test_close_falls_back_to_a_fresh_proxy_when_the_opened_one_has_gone_stale(
    clock: Clock, tmp_path: Path
) -> None:
    """Real case: Documents.Open returned a proxy that later raised "Open.Close" (late binding).

    The document then stayed open with unsaved page-setup changes and Quit raised the
    "Save changes?" dialog in a hidden CAD instance.
    """
    app = FakeApp()
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    session = make_session(app)
    doc = session.open(drawing)
    fresh = FakeRawDoc(str(drawing), app.events)
    fresh.collection = app.Documents
    app.Documents.items[0] = fresh  # the application hands out a working proxy for the same file
    doc.raw.close_error = AttributeError("Open.Close")
    doc.close()
    assert fresh.closed and doc.closed and doc not in session.documents
    assert not session._unhealthy


def test_quit_sweeps_remaining_documents_of_an_owned_instance_before_quitting(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tmp_path: Path
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    drawing = tmp_path / "a.dwg"
    drawing.write_text("x")
    session = make_session(app)
    doc = session.open(drawing)
    doc.raw.close_error = AttributeError("Open.Close")  # the ordinary close cannot work
    leftover = FakeRawDoc(str(drawing), app.events)  # still open in the instance, dirty
    leftover.collection = app.Documents
    app.Documents.items = [leftover]
    session.quit()
    assert leftover.closed, "a dirty document left open makes Quit raise a modal dialog"
    assert app.events.index("doc.close") < app.events.index("quit")


def test_quit_does_not_sweep_documents_of_a_user_session(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    app = FakeApp()
    patch_process(monkeypatch, app, "quit")
    users = FakeRawDoc("D:/work/own.dwg", app.events)
    app.Documents.items = [users]
    session = make_session(app, owned=False)
    session.quit()
    assert not users.closed


# --- layout proxies that lose members ("Item.RefreshPlotDeviceInfo") ---------------------------------


class _StaleLayout:
    """A late-bound proxy whose members vanished (real case on a sheet-set drawing)."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"Item.{name}")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"Item.{name}")


def test_fresh_layout_refetches_when_the_proxy_loses_a_method() -> None:
    good = FakeLayout("Sheet-A", config="DWG To PDF.pc3")
    fetches: list[int] = []

    def fetch() -> Any:
        fetches.append(1)
        return good

    lay = acad._FreshLayout(_StaleLayout(), fetch)
    lay.RefreshPlotDeviceInfo()
    assert good.refreshed == 1 and len(fetches) == 1
    lay.RefreshPlotDeviceInfo()
    assert good.refreshed == 2 and len(fetches) == 1  # the fresh proxy is kept


def test_fresh_layout_refetches_on_assignment_too() -> None:
    good = FakeLayout("Sheet-A")
    lay = acad._FreshLayout(_StaleLayout(), lambda: good)
    lay.ConfigName = "DWG To PDF.pc3"
    assert good.ConfigName == "DWG To PDF.pc3"


def test_fresh_layout_gives_up_when_even_the_fresh_proxy_fails() -> None:
    lay = acad._FreshLayout(_StaleLayout(), lambda: _StaleLayout())
    with pytest.raises(AttributeError):
        lay.RefreshPlotDeviceInfo()
