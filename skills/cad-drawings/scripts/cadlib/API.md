# cadlib API contract (F1)

Written by the main context before the tracks start. **Code against this document; if you need a change, say so in your report instead of editing it.** Shared, finished pieces: `result.py` (`Result`, `CadError`, `ExitCode`, `emit`), `command.py` (`Command`), `scripts/cad.py` (loads `COMMANDS` from the modules named in `__init__.MODULES`), `assets/output.schema.json`, `assets/edit-spec.schema.json`. Read them first.

## Ground rules (all code)
- Python >= 3.10, type hints, `from __future__ import annotations`. ezdxf >= 1.4.4 (verify APIs with current docs, not memory). Windows-only imports (`win32com`, `pythoncom`, `winreg`) only inside functions or behind `sys.platform == "win32"`; the package must import on Linux/macOS.
- Never name a module after the standard library. No prompts, no `input()`. No bare `except:`; catch narrowly and classify.
- stdout is reserved for the final JSON (via `cad.py`). Diagnostics go to stderr and the run log. Never print from library code except through `RunContext.log`.
- Never loop over entities through COM. Reading/searching/diffing uses ezdxf on DXF.
- Large data goes to files in the run directory; `Result.summary` holds a few numbers only.
- Generic: no client names, layer names, paths or jargon in code, comments, tests or docstrings (see `AGENTS.md`). Tests use the synthetic fixtures from `evals/make_fixtures.py` (generate into `tmp_path`).
- Lint/format/test with `.venv/Scripts/python.exe -m ruff check --fix skills && ... -m ruff format skills && ... -m pytest skills/cad-drawings/tests -q -p no:cacheprovider`; also `python skills/cad-drawings/tests/check_forbidden.py`.

## Ownership (file scope — write only your own files)

| Track | Files |
|---|---|
| T1 python-pro | `scripts/doctor.py`, `cadlib/{runs,convert,dxf,render,doctor,cleanup}.py`, `tests/test_{runs,convert,dxf,render,doctor,cleanup}.py` |
| T2 python-pro | `cadlib/acad.py`, `tests/test_acad.py` |
| T3 technical-writer | `SKILL.md`, `references/*.md`, `README.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md` (repo root) |
| T4 test-automator | `.github/workflows/ci.yml`, `evals/evals.json`, `evals/trigger_queries.json`, `tests/test_skill_package.py`, `tests/conftest.py` |
| main context | `result.py`, `command.py`, `__init__.py`, `cad.py`, this file, schemas, `AGENTS.md`, `pyproject.toml` |

Commands live in `COMMANDS` of the module that implements them. `cleanup` is its own module (add `"cleanup"` is already NOT in `MODULES`: the main context adds it when T1 reports).

## Run directory (T1: `runs.py`)
Default base: `<user temp>/cad-drawings-runs/` (override: env `CAD_DRAWINGS_RUNS`, flag `--run-dir`). Never next to the source file. Layout: `<base>/<YYYYmmdd-HHMMSS>-<command>-<4 hex>/` with `log.txt`, `status.json`, `progress.log`, and outputs.

```python
class RunContext:
    dir: Path
    @classmethod
    def create(cls, command: str, base: Path | None = None) -> "RunContext": ...
    def path(self, name: str) -> Path: ...                 # inside self.dir, parent created
    def log(self, message: str) -> None: ...               # to log.txt and stderr
    def progress(self, done: int, total: int, note: str = "") -> None: ...  # progress.log + status.json heartbeat
    def add_output(self, result: "Result", name: str, path: Path, source: Path | None = None) -> None:
        # fills Result.outputs[name] = {path, bytes, mtime, source_mtime?, sha1}; raises CadError(code="STALE_OUTPUT")
        # if the artifact is older than `source`.
def file_sha1(path: Path) -> str: ...
def free_gb(path: Path) -> float: ...
def preflight_disk(path: Path, min_gb: float = 5.0) -> None: ...   # CadError("DISK_LOW", exit_code=BUSY, hint=...)
def stage_copy(src: Path, ctx: RunContext, with_dependencies: bool = True) -> Path: ...  # working copy in run dir (+ xrefs/plot config alongside)
@contextlib.contextmanager
def acquire_lock(name: str, *, base: Path | None = None) -> Iterator[None]: ...
    # lock file with {pid, command, started}; a dead owner (PID not running) is taken over;
    # a live owner -> CadError("LOCKED", exit_code=BUSY, hint="owner pid ... command ...")
```
Cache: DXF exports are cached by `sha1(source) + converter id` under `<base>/cache/`; `cache_get(sha1, kind) -> Path | None`, `cache_put(...)`.
Every command that writes anything calls `RunContext.create`; a command never overwrites an existing file unless `--overwrite`.

## `convert.py` (T1) — command `convert`
```python
class Converter(Protocol):
    name: str                       # "com" | "oda" | "libredwg"
    approximate: bool               # True for libredwg
    def available(self) -> tuple[bool, str]: ...
    def convert(self, src: Path, dst: Path, fmt: str, ctx: RunContext) -> list[str]: ...  # returns warnings; fmt: dxf|dwg
def detect_converters() -> list[Converter]: ...         # priority: com, oda, libredwg
def convert(src: Path, dst: Path, fmt: str, *, prefer: str | None = None, ctx: RunContext) -> tuple[Converter, list[str]]: ...
```
- COM backend delegates to `cadlib.acad` (T2) via `from .acad import export_dxf` imported lazily; **do not run COM in T1 tests** (T1 uses a fake). Success = output file exists AND is newer than the start of the call AND (for DXF) opens with `ezdxf.readfile(..., recover=True)`; never trust exit codes or `ezdxf.addons.odafc` silence.
- ODA: detect `ODAFileConverter` via PATH, `%ProgramFiles%\ODA\*\ODAFileConverter.exe` (versioned directory), `/usr/bin`, `/Applications/ODAFileConverter.app/...`. Linux needs Xvfb (report it); macOS opens a window (warn).
- LibreDWG: `dwg2dxf`/`dxf2dwg` subprocess; always `approximate=True` and a warning that some objects may be dropped; propose ODA.
- No converter at all: `CadError("NO_BACKEND", exit_code=MISSING_DEPENDENCY, hint=<install options from doctor>)`.
- `.dxf` input is returned as is (copy to the run dir only when asked).
- Flags: `convert SRC --to dxf|dwg [--out PATH] [--backend auto|com|oda|libredwg] [--overwrite] [--timeout S]`. (`--to pdf` arrives in F2 with `plot`.)

## `dxf.py` (T1) — commands `info`, `find`, `dump`, `fingerprint`, `diff`
Helper: `load_dxf(path: Path, ctx) -> ezdxf.document.Drawing` (DWG input is converted first through `convert`, cached; `ezdxf.readfile` with `recover` fallback; never `ezdxf.addons.dwg`).

- `info FILE [--conventions]`: units (`$INSUNITS`, `$MEASUREMENT`), extents, layouts in tab order with page setup and viewports (center, size, scale, twist, frozen layers, **status**), layers (count, frozen/off), block stats, xrefs (attach/overlay/unresolved/bound; note `$0$` prefixes), missing SHX fonts (best effort), `.dwl` lock files next to the source. `--conventions` adds text styles/heights, title block guess, naming patterns. Summary small; full report to `info.json`.
- `find FILE [FILE ...] --pattern REGEX [-i] [--where text,attrib,layer,block] [--hidden include|only|exclude]`: scans TEXT, MTEXT (plain text and raw), ATTRIB/ATTDEF, layer names and block names in **all spaces and in block definitions**; each hit: file, handle, type, space/layout/block, layer, plain+raw text, insertion point, `visible_in_space` (layer on/thawed, block instantiated), and `prints_on` (layouts whose active viewport window contains it and whose viewport does not freeze its layer; null when unknown). Hits to `find.jsonl`; summary has counts per type/space.
- `dump FILE [--space model|paper|all|NAME] [--type T ...] [--layer L ...] [--window X1,Y1,X2,Y2] [--handle H ...] [--limit N]`: JSONL of entities to `dump.jsonl`; default limit 5000 with a warning when cut.
- `fingerprint FILE [--out PATH]`: JSON of graphic entities (handle, type, layout or block, layer, bbox, sha1 of plain text, insertion point) + per-type counts + viewport table; written to the run dir; the temporary DXF is deleted after use.
- `diff A B [--full]`: semantic diff of two fingerprints (or two drawings, fingerprinting them first). Match entities by content signature when handles differ; ignore save noise (anonymous block names `*U<n>`, `*Paper_Space<n>`, viewport ids, `$HANDSEED`, dictionaries); report added/removed/changed/moved with old/new values; list `unmatched` as "to confirm" and **never claim a removal's cause**. Summary = counts and the first ~10 changes; full list in `diff.json`. Must score 3/3 true changes with 0 false alarms on `plan_v2.dxf` vs `sheet_set_v1.dxf` (`changes.json` is the ground truth).

## `render.py` (T1) — command `render`
`render FILE [--layout NAME ...] [--backend auto|com|ezdxf] [--dpi 150] [--max-px 2000] [--crop X1,Y1,X2,Y2] [--tiles COLSxROWS]`
- Always a fresh run directory; PNG per layout; result lists each PNG with `source_mtime`; never reuse an older artifact.
- COM path (preferred when available and the user agreed): per layout plot from a freshly opened document via `cadlib.acad.plot_layout_pdf` (T2), then PDF -> PNG with **pypdfium2**. ezdxf path: `ezdxf.addons.drawing`, `approximate=True` and a warning; implements the seven workarounds listed in `_local`-independent form: (1) normalise viewports (ezdxf drops status<=0 and the first status==1; treat the "overall" viewport with `view_center_point == center and view_height == height` specially), (2) explicit CTB via `RenderContext(ctb=<path>)` only if a path is given, (3) SHX/support dirs if provided, `lineweight_scaling≈0.87`, (4) 1:1 with `Settings(fit_page=False)` and `render_box` from paper limits, (5) layer exclusion in layouts with viewports through `set_layer_properties_override`, (6) check `importlib.util.find_spec("pymupdf")` before importing the matplotlib/pymupdf backend (ezdxf prints an AGPL warning to stdout otherwise; use pillow/matplotlib backend), (7) xrefs through `ezdxf.xref.embed` with `\` -> `/` normalisation, loop for nested.
- `--max-px` caps the longest side; `--crop` and `--tiles` produce extra PNGs at full resolution of the crop.
- PDF -> PNG: pypdfium2 (default). PyMuPDF only if installed and `--raster pymupdf`; Poppler/pdf2image last resort. Lazy imports only.

## `doctor.py` (T1) — `scripts/doctor.py` standalone (stdlib only) + command `doctor`
`scripts/doctor.py` must run before anything is installed: `python scripts/doctor.py [--probe-com]` prints the capability matrix as the same JSON contract (copy the tiny emit logic if `cadlib` is not importable). `cadlib/doctor.py` registers the `doctor` command and calls it. Reports: platform, Python, packages (ezdxf, pypdfium2, pywin32, pillow, matplotlib, pymupdf, jsonschema) with versions and minimums, converters (ODA, LibreDWG), CAD hosts through the registry (read-only `winreg`; ProgIDs `AutoCAD.Application[.NN[.N]]`, `BricscadApp.AcadApplication`, `ZWCAD.Application`, `GStarCAD.Application[.NN]`; installs under `HKLM\SOFTWARE\Autodesk\AutoCAD\R*`; `tasklist`/`pgrep` for running instances), free disk on the temp dir, Linux: Xvfb. Output: `summary.capabilities` = {read_dxf, read_dwg, render, plot_deliverable, edit_dwg, ...} each with `status` (available|degraded|missing), `via`, and for missing/degraded an `install` list of ready commands per OS. **Never installs anything.** `--probe-com` starts a CAD instance (may take a licence): only run it when the caller says the user agreed. Start from the prototype in `_local/env_detect_probe.py` (read it; re-write cleanly, generic).

## `cleanup.py` (T1) — command `cleanup`
`cleanup [--list] [--older-than DAYS] [--run RUN_ID ...] [--cache] [--orphans DIR] [--yes] [--dry-run]`: lists runs with sizes; deletes only files the tool itself created (tracked in the run's manifest), file by file (no recursive remove helpers beyond the run's own directory tree after its files are gone); `--orphans DIR` only **reports** `.dwl/.dwl2` files whose owner PID/host is not running, never deletes them. Requires `--yes` to delete.

## `acad.py` (T2) — COM session library (Windows only; import must not fail elsewhere)
```python
PROGIDS: list[str]                                   # discovered, newest first (registry), generic fallbacks
class AcadSession:
    pid: int
    @classmethod
    def start(cls, progid: str | None = None, *, visible: bool = False, timeout: float = 120.0,
              lock: bool = True) -> "AcadSession": ...   # own instance via DispatchEx; PID = tasklist diff before/after;
                                                          # refuses (CadError BUSY) if it cannot attribute a new PID;
                                                          # uses win32com.client.dynamic (no gen_py); waits for IsQuiescent
    @classmethod
    def attach_guarded(cls, *, confirmed: bool) -> "AcadSession": ...  # user's instance; only with confirmed=True; read-mostly
    def open(self, path: Path, *, readonly: bool = True) -> "Doc": ...  # by normalised full path, never ActiveDocument
    def quit(self, timeout: float = 90.0) -> None: ...  # re-fetch the app object, Quit, WAIT for own PID to disappear,
                                                        # then terminate only that PID; remove own .dwl files
    def __enter__/__exit__                              # exit always quits an instance it started
    def export_dxf(self, src: Path, dst: Path, version: str = "2013") -> None: ...
    def plot_layout_pdf(self, src: Path, layout: str, dst: Path, *, page_setup: dict | None = None) -> None: ...
        # fresh document per call; activates the layout before plotting; fails (CadError) if dst exists and is older than the call
    @contextlib.contextmanager
    def sysvars(self, doc: "Doc", **values: int) -> Iterator[None]: ...   # set, restore in finally; FILEDIA is never touched
def retry(fn, *args, tries: int = 12, base_delay: float = 0.4, max_delay: float = 5.0, **kwargs): ...
    # wraps the WHOLE call (retry(doc.Blocks.Item, i) evaluates doc.Blocks first: provide retry_expr(lambda: ...));
    # transient: HRESULT -2147418111 (RPC_E_CALL_REJECTED), -2147417846, late-binding AttributeError "<unknown>.X";
    # permanent: DISP_E_EXCEPTION with AutoCAD codes -> raise at once as CadError("COM_ERROR", ...)
def retry_expr(expr: Callable[[], T], **kw) -> T: ...
```
- Preflight: `preflight_disk` from `runs` (import lazily; if T1 is not merged yet use a local stub guarded by `try/except ImportError`).
- `ISAVEBAK=0`, `ISAVEPERCENT=0` only inside `sysvars()` for automatic runs, `BACKGROUNDPLOT=0` during plots, all restored.
- Lock: `runs.acquire_lock("com")` for the whole session (same lazy import rule).
- Facts verified on this machine (use them): `DispatchEx("AutoCAD.Application.24.3")` starts a NEW process even when another instance runs; plain `Dispatch` attaches to the first one; a new instance starts with one empty document; `Quit` is asynchronous (the process may stay up for tens of seconds, and can hang after a failed plot — escalate to graceful close of that PID, then forced termination of that PID only); after documents are closed, re-fetch the app object (`AttributeError: <unknown>.Quit` on the stale proxy); COM DXF exports report viewport `status` unreliably unless every layout is activated first — export must activate each layout before `SaveAs` (verify with a test drawing that has two layouts) or document the limitation in the result warnings; never `taskkill /IM`.
- **COM tests** are marked `@pytest.mark.com`, run only when the lead says no CAD work is in progress, one at a time, on synthetic fixtures converted by the test itself, with all output on a drive other than the system drive when possible. Unit tests use fake COM objects and must not start CAD.

## Command flags shared by all commands
`--run-dir PATH`, `--timeout SECONDS` (default 240 for blocking commands), `--dry-run` (mutating commands), `--overwrite` (replace an existing destination), `--yes` (confirm deletion). All paths accepted as given, normalised internally; non-ASCII paths must work.

## Not in F1
`plot`, `edit`, `measure`, `register`, `qa` (F2/F3). `edit-spec.schema.json` exists for F2.
