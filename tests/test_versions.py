"""One version number for the skill, the library and the plugin; the changelog must know it."""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "cad-drawings"
sys.path.insert(0, str(SKILL / "scripts"))

SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _skill_version() -> str:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    match = re.search(r"^\s+version:\s*\"?([^\"\s]+)\"?\s*$", front, re.MULTILINE)
    assert match, "SKILL.md frontmatter has no metadata.version"
    return match.group(1)


def test_skill_library_and_plugin_versions_agree() -> None:
    from cadlib import __version__ as library_version

    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    versions = {
        "SKILL.md metadata.version": _skill_version(),
        "cadlib.__version__": library_version,
        ".claude-plugin/plugin.json": plugin["version"],
    }
    assert len(set(versions.values())) == 1, versions
    assert SEMVER.match(versions["cadlib.__version__"]), versions


def test_changelog_has_an_unreleased_section_or_the_current_version() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    version = _skill_version()
    assert "## [Unreleased]" in changelog or f"## [{version}]" in changelog, (
        "CHANGELOG.md needs '## [Unreleased]' or a section for the current version"
    )


def test_a_released_tag_would_match_the_files() -> None:
    """The release workflow compares the tag with these files; keep that comparison honest."""
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "refs/tags/v" in workflow or "github.ref_name" in workflow
    assert "plugin.json" in workflow and "SKILL.md" in workflow
