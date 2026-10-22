"""Repository files outside the package: changelog fragments (tools/release_notes.py) and docker-compose.yml."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools" / "release_notes.py")
assert _spec and _spec.loader
release_notes = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("release_notes", release_notes)  # dataclasses look their module up
_spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added

- Existing feature.

### Fixed

- Existing fix.

## [0.8.0] - 2026-10-21

### Added

- Old feature.

[Unreleased]: https://example.org/compare/v0.8.0...HEAD
[0.8.0]: https://example.org/compare/v0.7.1...v0.8.0
"""


def _fragments(tmp_path: Path, **files: str) -> Path:
    directory = tmp_path / "unreleased"
    directory.mkdir()
    for name, text in files.items():
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    return directory


def test_repository_changelog_and_fragments_are_valid():
    changelog = release_notes.Changelog.parse((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert changelog.find("Unreleased") is not None
    assert changelog.links and changelog.links[0].startswith("[Unreleased]: ")
    # raises on a malformed fragment or on text outside a section of [Unreleased]
    release_notes.unreleased_notes(changelog, ROOT / "changes" / "unreleased")


def test_parse_and_render_keep_the_file_unchanged():
    assert release_notes.Changelog.parse(CHANGELOG).render() == CHANGELOG


def test_release_merges_fragments_and_updates_links(tmp_path):
    fragments = _fragments(
        tmp_path,
        a="### Fixed\n\n- Fragment fix.\n",
        b="### Breaking changes\n\n- Limits apply.\n\n### Added\n\n- Fragment feature.\n",
    )
    changelog = release_notes.make_release("0.10.0", "2026-11-01", release_notes.Changelog.parse(CHANGELOG), fragments)
    text = changelog.render()
    assert "## [Unreleased]\n\n## [0.10.0] - 2026-11-01\n\n### Breaking changes\n\n- Limits apply.\n" in text
    assert "### Added\n\n- Existing feature.\n\n- Fragment feature.\n\n### Fixed\n" in text
    assert "### Fixed\n\n- Existing fix.\n\n- Fragment fix.\n" in text
    assert text.endswith(
        "[Unreleased]: https://example.org/compare/v0.10.0...HEAD\n"
        "[0.10.0]: https://example.org/compare/v0.8.0...v0.10.0\n"
        "[0.8.0]: https://example.org/compare/v0.7.1...v0.8.0\n"
    )
    assert release_notes.release_notes("0.10.0", changelog, fragments).startswith("### Breaking changes")
    assert release_notes.release_notes("0.8.0", changelog, fragments) == "### Added\n\n- Old feature."


def test_prerelease_notes_preview_and_never_consume(tmp_path):
    fragments = _fragments(tmp_path, a="### Fixed\n\n- Fragment fix.\n")
    changelog = release_notes.Changelog.parse(CHANGELOG)
    notes = release_notes.release_notes("0.10.0-rc1", changelog, fragments)
    assert "- Existing feature." in notes and "- Fragment fix." in notes
    with pytest.raises(release_notes.NotesError, match="pre-releases"):
        release_notes.make_release("0.10.0-rc1", "2026-11-01", changelog, fragments)
    with pytest.raises(release_notes.NotesError, match=r"no section for 0\.9\.0"):
        release_notes.release_notes("0.9.0", changelog, fragments)
    with pytest.raises(release_notes.NotesError, match="already has a section"):
        release_notes.make_release("0.8.0", "2026-11-01", changelog, fragments)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("- loose bullet\n", "before the first"),
        ("### Improved\n\n- x\n", "unknown section"),
        ("### Added\n\n#### Detail\n", "only '### <section>'"),
        ("### Added\n\n### Fixed\n\n- x\n", "'Added' is empty"),
        ("", "no '### <section>'"),
    ],
)
def test_malformed_fragments_are_rejected(tmp_path, text, error):
    with pytest.raises(release_notes.NotesError, match=re.escape(error)):
        release_notes.read_fragments(_fragments(tmp_path, bad=text))


def test_compose_limits_logs_and_pins_a_minor_version():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    logging = re.search(r"\n    logging:\n((?:      .*\n)+)", compose)
    assert logging, "the slotserve service needs a logging section"
    assert "driver: json-file" in logging.group(1)
    assert re.search(r'max-size: "?50m"?', logging.group(1)) and re.search(r'max-file: "?5"?', logging.group(1))
    image = re.search(r"image: \$\{SLOTSERVE_IMAGE:-(?P<ref>[^}]+)\}", compose)
    assert image and re.fullmatch(r"ghcr\.io/peleryuan/slotserve:\d+\.\d+", image.group("ref")), image
