"""Package-level checks of the skill directory: SKILL.md format, references, CLI and eval files.

Files delivered by other tracks (SKILL.md, references/) may be missing while the phase is in
progress; the tests that need them skip with a reason instead of failing. The checkers are
plain functions and have their own positive and negative samples at the bottom of this file.
"""

from __future__ import annotations

import ast
import enum
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cad-drawings"
SCRIPTS = SKILL / "scripts"
SKILL_MD = SKILL / "SKILL.md"
EVALS = SKILL.parents[1] / "evals"

ALLOWED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_MD_LINES = 500
MAX_FILE_BYTES = 1_000_000
HOST_TOOLS = (
    "Read",
    "Bash",
    "Task",
    "AskUserQuestion",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "TodoWrite",
    "WebFetch",
)
# Commands that exist only in later phases; SKILL.md may mention them when it says so.
FUTURE_COMMANDS = {"plot", "edit", "measure", "register", "qa"}
FUTURE_MARKERS = ("f2", "f3", "planned", "not yet", "later", "future", "coming", "roadmap")
SHELL_FENCES = {"", "bash", "sh", "shell", "console", "powershell", "pwsh", "cmd", "bat", "zsh"}
PYTHON_LAUNCHERS = {"python", "python3", "py"}


# --------------------------------------------------------------------------------------
# checkers (pure functions)
# --------------------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter block, body); raise ValueError when the block is missing."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with a '---' frontmatter line")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    raise ValueError("frontmatter is not closed by a second '---' line")


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def parse_frontmatter(block: str) -> dict[str, Any]:
    """Minimal YAML subset: scalars, quoted scalars, folded/literal blocks, one-level mapping."""
    result: dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        match = re.match(r"^([A-Za-z][\w-]*):(?:\s+(.*))?$", line)
        if not match:
            raise ValueError(f"cannot parse frontmatter line: {line!r}")
        key, value = match.group(1), (match.group(2) or "").strip()
        i += 1
        nested: list[str] = []
        while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
            nested.append(lines[i])
            i += 1
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            parts = [n.strip() for n in nested if n.strip()]
            result[key] = (" " if value.startswith(">") else "\n").join(parts)
        elif value:
            if nested and any(n.strip() for n in nested):
                raise ValueError(f"unexpected indented lines after {key!r}")
            result[key] = _scalar(value)
        elif any(n.strip() for n in nested):
            mapping: dict[str, str] = {}
            for n in nested:
                if not n.strip():
                    continue
                sub = re.match(r"^\s+([^:\s][^:]*):\s*(.*)$", n)
                if not sub:
                    raise ValueError(f"cannot parse nested line under {key!r}: {n!r}")
                mapping[sub.group(1).strip()] = _scalar(sub.group(2))
            result[key] = mapping
        else:
            result[key] = ""
    return result


def frontmatter_problems(meta: dict[str, Any], directory_name: str) -> list[str]:
    problems: list[str] = []
    extra = sorted(set(meta) - ALLOWED_FRONTMATTER)
    if extra:
        problems.append(f"unknown top-level fields: {extra}")
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        problems.append("name is missing or empty")
    else:
        if len(name) > 64 or not NAME_RE.match(name):
            problems.append(f"name {name!r} must be 1-64 chars of [a-z0-9] and single hyphens")
        if name != directory_name:
            problems.append(f"name {name!r} must equal the directory name {directory_name!r}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("description is missing or empty")
    elif len(description) > 1024:
        problems.append(f"description has {len(description)} chars (max 1024)")
    compat = meta.get("compatibility")
    if compat is not None and (not isinstance(compat, str) or not 1 <= len(compat) <= 500):
        problems.append("compatibility must be 1..500 chars when present")
    metadata = meta.get("metadata")
    if metadata is not None and not (
        isinstance(metadata, dict) and all(isinstance(v, str) for v in metadata.values())
    ):
        problems.append("metadata must map string keys to string values")
    return problems


_TOOL_ALT = "|".join(HOST_TOOLS)
# `Read`, `Bash(...)`: a host tool name quoted as code.
_BACKTICKED_TOOL = re.compile(rf"`(?:{_TOOL_ALT})(?:\([^`]*\))?`")
# "the Read tool", "Bash tool call(s)", "Task tool", "Write tools".
_NAMED_TOOL = re.compile(rf"\b(?:{_TOOL_ALT})\s+tools?\b")


def host_tool_mentions(text: str) -> list[str]:
    """Host-specific tool names used as tool names (not ordinary words like 'read' or 'Edit')."""
    hits = [m.group(0) for m in _BACKTICKED_TOOL.finditer(text)]
    hits += [m.group(0) for m in _NAMED_TOOL.finditer(text)]
    return hits


def _fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    lang: str | None = None
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if lang is None:
                lang = stripped[3:].strip().lower()
                current = []
            else:
                blocks.append((lang, current))
                lang = None
        elif lang is not None:
            current.append(line)
    return blocks


def _looks_like_script(token: str) -> bool:
    token = token.strip("\"'")
    return token.endswith(".py") or token.startswith(("./", ".\\"))


def _first_command_token(command: str) -> str | None:
    for token in command.split():
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):  # VAR=value prefix
            continue
        return token
    return None


def bare_script_invocations(text: str) -> list[str]:
    """Shell lines that run a script directly instead of through ``python``/``python3``."""
    problems: list[str] = []
    for lang, lines in _fenced_blocks(text):
        if lang not in SHELL_FENCES:
            continue
        for raw in lines:
            line = re.sub(r"^\s*(?:\$|>|PS>)\s+", "", raw).strip()
            if not line or line.startswith("#"):
                continue
            token = _first_command_token(line)
            if token and _looks_like_script(token):
                problems.append(line)
    for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text):
        tokens = span.split()
        if len(tokens) > 1 and _looks_like_script(tokens[0]) and tokens[0] not in PYTHON_LAUNCHERS:
            problems.append(span)
    return problems


def mentioned_skill_paths(text: str) -> list[str]:
    """``references/..``, ``scripts/..``, ``assets/..`` paths written in the text."""
    paths = []
    for match in re.finditer(r"(?<![\w/.-])((?:references|scripts|assets)/[\w./-]*)", text):
        path = match.group(1).rstrip(".,;:)")
        if path.endswith("/") and path.count("/") == 1:
            continue  # a bare directory mention such as "references/"
        paths.append(path)
    return paths


def relative_links(text: str) -> list[str]:
    targets = []
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#)", target, re.IGNORECASE):
            continue
        targets.append(target.split("#", 1)[0])
    return [t for t in targets if t]


def cad_commands_mentioned(text: str) -> list[tuple[str, bool]]:
    """(command, flagged_as_future) for every ``cad.py <command>`` in the text."""
    lines = text.splitlines()
    found: list[tuple[str, bool]] = []
    for i, line in enumerate(lines):
        for match in re.finditer(r"\bcad\.py\s+([a-z][a-z0-9-]*)\b", line):
            context = " ".join(lines[max(0, i - 1) : i + 1]).lower()
            found.append((match.group(1), any(m in context for m in FUTURE_MARKERS)))
    return found


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def skill_text() -> str:
    if not SKILL_MD.is_file():
        pytest.skip("SKILL.md not delivered yet (track T3)")
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc_files() -> dict[Path, str]:
    """SKILL.md plus every markdown file under references/ (those that exist)."""
    files = {}
    if SKILL_MD.is_file():
        files[SKILL_MD] = SKILL_MD.read_text(encoding="utf-8")
    for path in sorted((SKILL / "references").glob("**/*.md")):
        files[path] = path.read_text(encoding="utf-8")
    if not files:
        pytest.skip("no SKILL.md or references yet (track T3)")
    return files


@pytest.fixture(scope="module")
def commands() -> dict[str, Any]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("cad").load_commands()  # type: ignore[no-any-return]
    finally:
        sys.path.remove(str(SCRIPTS))


# --------------------------------------------------------------------------------------
# SKILL.md
# --------------------------------------------------------------------------------------


def test_skill_md_frontmatter_is_valid(skill_text: str) -> None:
    block, _body = split_frontmatter(skill_text)
    problems = frontmatter_problems(parse_frontmatter(block), SKILL.name)
    assert not problems, problems


def test_skill_md_is_under_500_lines(skill_text: str) -> None:
    assert len(skill_text.splitlines()) < MAX_SKILL_MD_LINES


def test_skill_md_validates_with_reference_validator() -> None:
    pytest.importorskip(
        "skills_ref", reason="skills-ref not installed (CI runs `agentskills validate`)"
    )
    if not SKILL_MD.is_file():
        pytest.skip("SKILL.md not delivered yet (track T3)")
    from skills_ref import validate

    assert validate(SKILL) == []


def test_no_host_specific_tool_names(doc_files: dict[Path, str]) -> None:
    hits = {
        str(path.relative_to(SKILL)): mentions
        for path, text in doc_files.items()
        if (mentions := host_tool_mentions(text))
    }
    assert not hits, f"host tool names in agent-neutral text: {hits}"


def test_mentioned_paths_exist(doc_files: dict[Path, str]) -> None:
    missing = []
    for path, text in doc_files.items():
        for mention in mentioned_skill_paths(text):
            if not (SKILL / mention).exists():
                missing.append(f"{path.relative_to(SKILL)}: {mention}")
    assert not missing, missing


def test_relative_links_resolve(doc_files: dict[Path, str]) -> None:
    broken = []
    for path, text in doc_files.items():
        for target in relative_links(text):
            if not (path.parent / target).resolve().exists():
                broken.append(f"{path.relative_to(SKILL)}: {target}")
    assert not broken, broken


def test_shell_invocations_go_through_python(doc_files: dict[Path, str]) -> None:
    bad = {
        str(path.relative_to(SKILL)): lines
        for path, text in doc_files.items()
        if (lines := bare_script_invocations(text))
    }
    assert not bad, f"scripts must be started as `python <script>`: {bad}"


def test_listed_commands_are_registered(skill_text: str, commands: dict[str, Any]) -> None:
    unknown = sorted(
        {
            name
            for name, is_future in cad_commands_mentioned(skill_text)
            if name not in commands and not (is_future and name in FUTURE_COMMANDS)
        }
    )
    assert not unknown, f"SKILL.md mentions commands missing from cad.py: {unknown}"


# --------------------------------------------------------------------------------------
# the skill directory and scripts
# --------------------------------------------------------------------------------------


def test_no_file_over_one_megabyte() -> None:
    skipped = {"__pycache__", "fixtures", "workspace"}
    big = [
        f"{p.relative_to(SKILL)} ({p.stat().st_size} bytes)"
        for p in SKILL.rglob("*")
        if p.is_file()
        and not skipped & set(p.relative_to(SKILL).parts)
        and p.stat().st_size > MAX_FILE_BYTES
    ]
    assert not big, big


def _run_cad(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "cad.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def test_cad_help_prints_usage_without_traceback() -> None:
    proc = _run_cad("--help")
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()
    assert "Traceback" not in proc.stdout + proc.stderr


def test_cad_unknown_command_returns_json_and_exit_2() -> None:
    proc = _run_cad("no-such-command")
    assert proc.returncode == 2
    data = json.loads(proc.stdout)
    assert data["status"] == "error" and data["exit_code"] == 2
    assert "Traceback" not in proc.stdout + proc.stderr


def test_every_registered_command_has_help(commands: dict[str, Any]) -> None:
    assert commands, "no commands registered"
    for name in commands:
        proc = _run_cad(name, "--help")
        assert proc.returncode == 0, f"{name}: {proc.stderr}"
        assert "Traceback" not in proc.stdout + proc.stderr, name


# --------------------------------------------------------------------------------------
# eval definitions
# --------------------------------------------------------------------------------------


def _load_json(name: str) -> Any:
    return json.loads((EVALS / name).read_text(encoding="utf-8"))


def test_evals_json_structure(fixtures_dir: Path) -> None:
    data = _load_json("evals.json")
    assert data["skill_name"] == SKILL.name
    evals = data["evals"]
    assert [e["id"] for e in evals] == list(range(1, len(evals) + 1)) and len(evals) == 6
    catalog = set(data["mechanical_check_catalog"])
    for e in evals:
        assert e["prompt"].strip() and e["expected_output"].strip(), e["id"]
        assert len(e["assertions"]) >= 5 and all(isinstance(a, str) for a in e["assertions"])
        assert e["mechanical_checks"] and set(e["mechanical_checks"]) <= catalog, e["id"]
        for rel in e["files"]:
            assert rel.startswith("evals/fixtures/"), rel
            name = Path(rel).name
            if e.get("requires_cad"):  # a DWG made locally from the DXF; never committed
                assert e["fixture_setup"].strip() and name.endswith(".dwg")
                name = Path(name).with_suffix(".dxf").name
            assert (fixtures_dir / name).is_file(), f"{rel} is not made by make_fixtures"
            assert rel in e["prompt"], f"prompt of eval {e['id']} must name {rel}"


def test_evals_json_numbers_match_ground_truth(
    truth: dict[str, Any], changes: dict[str, Any]
) -> None:
    files = truth["files"]
    texts = {e["id"]: " ".join(e["assertions"]) for e in _load_json("evals.json")["evals"]}

    occurrences = {o["id"]: o for o in files["sheet_set.dxf"]["search"]["occurrences"]}
    assert len(occurrences) == 6 and "exactly 6 occurrences" in texts[1]
    assert occurrences["attrib_value"]["prints_on"] == ["Sheet-A", "Sheet-B"]
    assert occurrences["text_model_visible"]["prints_on"] == []
    assert occurrences["mtext_paper_space"]["prints_on"] == ["Sheet-A"]
    assert occurrences["text_on_frozen_layer"]["prints_on"] == []
    assert occurrences["text_in_unused_block"]["prints_on"] == []
    assert occurrences["layer_name"]["prints_on"] is None
    for sheet in files["sheet_set.dxf"]["layouts"]:
        for field in sheet["title_block"].values():
            assert field["value"] in texts[1]
            assert field["value"] in texts[6]
        (viewport,) = sheet["viewports"]
        assert viewport["view_center_wcs"] == [4000.0, 3250.0]
        assert viewport["view_center_is_dcs"] is True

    assert len(changes["real_changes"]) == 3 and "exactly 3 real changes" in texts[3]
    assert [c["kind"] for c in changes["real_changes"]] == [
        "text_changed",
        "entity_deleted",
        "entity_moved",
    ]
    assert (changes["real_changes"][0]["before"], changes["real_changes"][0]["after"]) == (
        "STORE",
        "ARCHIVE",
    )
    assert changes["real_changes"][2]["vector"] == [1500.0, -750.0]

    assert files["hatch_assoc.dxf"]["area_m2"] == 21.0 and "21.0 m2" in texts[4]
    assert files["hatch_assoc.dxf"]["decoy_polyline"]["area_m2"] == 1.0
    for name in ("plan_cm_v1.dxf", "plan_mm_v1.dxf"):
        room_a = next(r for r in files[name]["rooms"] if r["name"] == "ROOM A")
        assert room_a["area_m2"] == 19.0
    assert "19.0 m2" in texts[4]


def test_trigger_queries_follow_the_guide() -> None:
    queries = _load_json("trigger_queries.json")
    assert len(queries) == 20
    assert len({q["query"] for q in queries}) == 20
    positives = [q for q in queries if q["should_trigger"]]
    assert len(positives) == 10 and len(queries) - len(positives) == 10
    for q in queries:
        assert set(q) == {"query", "should_trigger", "split"} and q["split"] in {
            "train",
            "validation",
        }
    train = [q for q in queries if q["split"] == "train"]
    assert len(train) == 12  # 60 / 40
    for label in (True, False):  # both splits keep the positive/negative balance
        assert sum(q["should_trigger"] is label for q in train) == 6


# --------------------------------------------------------------------------------------
# self-tests of the checkers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Use the `Read` tool to open the file.",
        "Call the Bash tool with the command.",
        "Spawn a subagent with the Task tool.",
        "Allowed: `Bash(git:*)`.",
        "Ask through AskUserQuestion tool calls.",
        "Prefer Grep tools over shell greps.",
    ],
)
def test_host_tool_checker_flags_tool_names(text: str) -> None:
    assert host_tool_mentions(text)


@pytest.mark.parametrize(
    "text",
    [
        "Read the file header first, then edit the layer table.",
        "Edit the DATE field. Write the result to a new file.",
        "A task list and a glob pattern are ordinary words.",
        "Use the interactive question tool of the host.",
        "The bash script is not needed; run it in a shell.",
        "Taskbar and Readme are not tools.",
        "Use `edit` and `cad.py read-only` commands.",
    ],
)
def test_host_tool_checker_ignores_ordinary_words(text: str) -> None:
    assert host_tool_mentions(text) == []


def test_bare_script_checker() -> None:
    bad = "```bash\nscripts/cad.py info a.dxf\n```\nand `./doctor.py --json` or `cad.py info x`"
    assert len(bare_script_invocations(bad)) == 3
    good = (
        "```bash\n# comment\n$ python scripts/cad.py info a.dxf\nCAD_X=1 python3 scripts/doctor.py\n"
        "pip install ezdxf\n```\nSee `scripts/cad.py` and `python scripts/cad.py find x`."
    )
    assert bare_script_invocations(good) == []
    assert bare_script_invocations("```text\nscripts/cad.py info a.dxf\n```") == []


def test_path_and_link_checkers() -> None:
    text = "See references/dxf.md, `scripts/cad.py`, references/ and scripts/<name>.py (x)."
    assert mentioned_skill_paths(text) == ["references/dxf.md", "scripts/cad.py"]
    links = "[a](references/x.md#top) [b](https://x.org) [c](#here) [d](mailto:a@b.c)"
    assert relative_links(links) == ["references/x.md"]


def test_command_mention_checker() -> None:
    text = (
        "Run `python scripts/cad.py info FILE`.\nLater (F2): `python scripts/cad.py plot FILE`.\n"
    )
    assert cad_commands_mentioned(text) == [("info", False), ("plot", True)]
    assert cad_commands_mentioned("cad.py <command> --help and cad.py --version") == []


def test_frontmatter_parser_and_validator() -> None:
    text = (
        "---\nname: cad-drawings\ndescription: >\n  First line.\n  Second line.\n"
        'compatibility: "Needs Python 3.10+"\nmetadata:\n  version: "0.1"\n---\n# Body\n'
    )
    block, body = split_frontmatter(text)
    meta = parse_frontmatter(block)
    assert body.strip() == "# Body"
    assert meta["description"] == "First line. Second line."
    assert meta["metadata"] == {"version": "0.1"}
    assert frontmatter_problems(meta, "cad-drawings") == []
    assert frontmatter_problems(meta, "other-name")
    for bad in (
        {"name": "Cad", "description": "x"},
        {"name": "a--b", "description": "x"},
        {"name": "-a", "description": "x"},
        {"name": "a", "description": ""},
        {"name": "a", "description": "x" * 1025},
        {"name": "a", "description": "x", "compatibility": "y" * 501},
        {"name": "a", "description": "x", "version": "1"},
    ):
        assert frontmatter_problems(bad, "a"), bad
    with pytest.raises(ValueError):
        split_frontmatter("no frontmatter")


# --------------------------------------------------------------------------------------
# documentation against the code (flags, exit codes, README, third-party notices)
# --------------------------------------------------------------------------------------

REPO = SKILL.parents[1]
README = REPO / "README.md"
NOTICES = REPO / "THIRD_PARTY_NOTICES.md"
FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")

# import name -> distribution name, for third-party packages the code may import
DISTRIBUTIONS = {
    "ezdxf": "ezdxf",
    "PIL": "Pillow",
    "pypdfium2": "pypdfium2",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "win32com": "pywin32",
    "pythoncom": "pywin32",
    "pywintypes": "pywin32",
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32process": "pywin32",
    "winreg": None,  # standard library on Windows
    "pymupdf": "PyMuPDF",
    "fitz": "PyMuPDF",
    "jsonschema": "jsonschema",
    "shapely": "shapely",
}
LOCAL_MODULES = {"cadlib", "cad", "doctor"}


def command_spans(text: str) -> list[str]:
    """Text that looks like a command line: shell/untagged fenced lines and inline code."""
    spans: list[str] = []
    for lang, lines in _fenced_blocks(text):
        if lang in SHELL_FENCES:
            spans += [re.sub(r"^\s*(?:\$|>)\s+", "", line) for line in lines]
    spans += re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text)
    return spans


def command_flag_uses(text: str, names: set[str]) -> list[tuple[str, str]]:
    """(command, flag) for every flag written after a registered command name in one span.

    The command is the token after ``cad.py`` or, in a bare reference line, the first token.
    """
    uses: list[tuple[str, str]] = []
    for span in command_spans(text):
        tokens = span.split()
        start = next(
            (i + 1 for i, t in enumerate(tokens[:-1]) if t.rstrip("`").endswith("cad.py")), 0
        )
        if start >= len(tokens) or tokens[start] not in names:
            continue
        uses += [(tokens[start], f) for f in FLAG_RE.findall(" ".join(tokens[start + 1 :]))]
    return uses


def documented_exit_codes(text: str) -> list[tuple[int, str]]:
    """(code, description) from 'Exit codes: 0 = ok, 1 = ...' lines and from tables in an
    'Exit codes' section."""
    pairs: list[tuple[int, str]] = []
    in_section = False
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s", line):
            in_section = bool(re.search(r"exit codes?", line, re.IGNORECASE))
        if re.match(r"^\W*Exit codes?\b", line, re.IGNORECASE):
            pairs += [
                (int(n), d.strip().lower()) for n, d in re.findall(r"(\d)\s*=\s*([^,;.]+)", line)
            ]
        elif in_section and (row := re.match(r"^\|\s*(\d)\s*\|\s*(.+?)\s*\|", line)):
            pairs.append((int(row.group(1)), row.group(2).lower()))
    return pairs


def exit_code_problems(pairs: list[tuple[int, str]], exit_code_enum: Any) -> list[str]:
    problems = []
    members = {int(c): c for c in exit_code_enum}
    missing = sorted(set(members) - {n for n, _ in pairs})
    extra = sorted({n for n, _ in pairs} - set(members))
    if missing:
        problems.append(f"codes not documented: {missing}")
    if extra:
        problems.append(f"documented codes that are not ExitCode members: {extra}")
    extra_words = {
        "OK": {"ok", "success"},
        "BAD_ARGS": {"argument"},
        "MISSING_DEPENDENCY": {"backend"},
    }
    for number, description in pairs:
        member = members.get(number)
        if member is None:
            continue
        words = set(member.name.lower().split("_")) | extra_words.get(member.name, set())
        if not any(w in description for w in words):
            problems.append(f"{number} is described as {description!r}, but is {member.name}")
    return problems


def readme_unregistered_commands(text: str, registered: set[str]) -> list[str]:
    """Commands the README advertises that do not exist, unless the line says 'planned'."""
    bad = []
    for line in text.splitlines():
        if "planned" in line.lower():
            continue
        names = {n for n, _ in cad_commands_mentioned(line)}
        names |= {n for n in FUTURE_COMMANDS if f"`{n}`" in line}
        bad += [f"{n}: {line.strip()}" for n in sorted(names) if n not in registered]
    return bad


def module_level_imports(source: str) -> set[str]:
    """Top-level package names imported when the module is imported (not in functions,
    not under ``if TYPE_CHECKING``)."""
    names: set[str] = set()

    def visit(body: list[Any]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
            elif isinstance(node, ast.If):
                if "TYPE_CHECKING" not in ast.unparse(node.test):
                    visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(ast.parse(source).body)
    return names


def third_party_distributions(source: str) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    result = set()
    for name in module_level_imports(source) - stdlib - LOCAL_MODULES:
        dist = DISTRIBUTIONS.get(name, name)
        if dist:
            result.add(dist)
    return result


def _help_flags(command: str, cache: dict[str, set[str]]) -> set[str]:
    if command not in cache:
        cache[command] = set(FLAG_RE.findall(_run_cad(command, "--help").stdout)) | {"--help"}
    return cache[command]


def test_documented_flags_exist_in_command_help(commands: dict[str, Any]) -> None:
    docs = {}
    if SKILL_MD.is_file():
        docs[SKILL_MD] = SKILL_MD.read_text(encoding="utf-8")
    if README.is_file():
        docs[README] = README.read_text(encoding="utf-8")
    if not docs:
        pytest.skip("neither SKILL.md nor README.md delivered yet (track T3)")
    cache: dict[str, set[str]] = {}
    unknown = []
    for path, text in docs.items():
        for command, flag in command_flag_uses(text, set(commands)):
            if flag not in _help_flags(command, cache):
                unknown.append(f"{path.name}: {command} {flag}")
    assert not unknown, f"flags missing from `cad.py <command> --help`: {sorted(set(unknown))}"


def test_documented_exit_codes_match_exit_code_enum() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        exit_code = importlib.import_module("cadlib.result").ExitCode
    finally:
        sys.path.remove(str(SCRIPTS))
    checked = 0
    for path in (SKILL_MD, README):
        if not path.is_file():
            continue
        pairs = documented_exit_codes(path.read_text(encoding="utf-8"))
        if path == SKILL_MD:
            assert pairs, "SKILL.md lists no exit codes"
        if pairs:
            checked += 1
            problems = exit_code_problems(pairs, exit_code)
            assert not problems, (path.name, problems)
    if not checked:
        pytest.skip("neither SKILL.md nor README.md delivered yet (track T3)")


def test_readme_advertises_only_registered_commands(commands: dict[str, Any]) -> None:
    if not README.is_file():
        pytest.skip("README.md not delivered yet (track T3)")
    bad = readme_unregistered_commands(README.read_text(encoding="utf-8"), set(commands))
    assert not bad, f"README names commands that are not registered (mark them 'planned'): {bad}"


def test_third_party_notices_cover_imported_distributions() -> None:
    if not NOTICES.is_file():
        pytest.skip("THIRD_PARTY_NOTICES.md not delivered yet (track T3)")
    notices = NOTICES.read_text(encoding="utf-8").lower()
    needed: dict[str, list[str]] = {}
    for path in [*(SCRIPTS / "cadlib").glob("*.py"), *SCRIPTS.glob("*.py")]:
        for dist in third_party_distributions(path.read_text(encoding="utf-8")):
            needed.setdefault(dist, []).append(path.name)
    assert needed, "scan found no third-party imports; the scanner is broken"
    missing = {d: files for d, files in needed.items() if d.lower() not in notices}
    assert not missing, f"THIRD_PARTY_NOTICES.md does not mention: {missing}"


def test_doc_checkers_samples() -> None:
    names = {"info", "render", "find"}
    text = (
        "```bash\npython scripts/cad.py render a.dxf --dpi 150 --nope\n```\n"
        "Reference lines:\n```\nfind <file> [--pattern RE] [-i]\n```\n"
        "Inline `info <file> --conventions` and prose --ignored here and `ls --all`."
    )
    assert command_flag_uses(text, names) == [
        ("render", "--dpi"),
        ("render", "--nope"),
        ("find", "--pattern"),
        ("info", "--conventions"),
    ]

    class Codes(enum.IntEnum):
        OK = 0
        BAD_ARGS = 2
        PARTIAL = 7

    good = documented_exit_codes("Exit codes: 0 = success, 2 = bad arguments, 7 = partial success.")
    assert good == [(0, "success"), (2, "bad arguments"), (7, "partial success")]
    assert exit_code_problems(good, Codes) == []
    assert exit_code_problems(good[:2], Codes)  # 7 not documented
    assert exit_code_problems([*good, (9, "other")], Codes)  # not a member
    assert exit_code_problems([(0, "success"), (2, "timeout"), (7, "partial")], Codes)
    table = "## Exit codes\n\n| Code | Meaning |\n|---|---|\n| 0 | Success |\n| 7 | Partial |\n"
    assert documented_exit_codes(table) == [(0, "success"), (7, "partial")]
    assert documented_exit_codes("## Other\n| 1 | x |\n") == []

    readme = "Use `plot` soon.\n- `plot` (planned)\nRun cad.py info and cad.py bogus.\n"
    assert readme_unregistered_commands(readme, {"info"}) == [
        "plot: Use `plot` soon.",
        "bogus: Run cad.py info and cad.py bogus.",
    ]

    source = (
        "import os, ezdxf.bbox\nfrom PIL import Image\nfrom . import x\n"
        "try:\n    import win32com.client\nexcept ImportError:\n    pass\n"
        "if TYPE_CHECKING:\n    import numpy\n"
        "def f():\n    import matplotlib\n"
    )
    assert module_level_imports(source) == {"os", "ezdxf", "PIL", "win32com"}
    assert third_party_distributions(source) == {"ezdxf", "Pillow", "pywin32"}
