"""Every docs link the backend hands out must land on a heading, in English and in Chinese.

The web UI turns ``doc`` of a diagnosis (``services/diagnostics/patterns.json``) and ``doc_anchor`` / ``docs``
actions of the health checks (``services/health_checks.py``) into links to the published docs, with the same
anchor for both languages (``frontend/src/features/admin-setup/api.ts``, ``docsUrl``). So ``docs/<page>.md`` and
``docs/<page>.zh.md`` must both define the anchor; Chinese headings need an explicit ``{#id}``.

``mkdocs build --strict`` (with ``validation: anchors: warn``) checks the links between docs pages; this test
covers the links that live in code.
"""

import json
import re
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
BACKEND = ROOT / "backend" / "slotserve"

# Anchors other code and the operations runbooks rely on, besides the ones found below.
REQUIRED = ("operations#backups", "operations#restore", "operations#secret-key", "operations#upgrade")

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
EXPLICIT_ID = re.compile(r"\{\s*:?\s*#([^\s}]+)[^}]*\}\s*$")
HTML_ID = re.compile(r"""\bid=["']([^"']+)["']""")


def slugify(text: str) -> str:
    """Python-Markdown's default ``toc`` slugify (ASCII only), applied to the heading's visible text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = re.sub(r"[`*_]", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    fence = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif stripped.startswith(fence * 3):
                fence = None
            continue
        if fence is not None:
            continue
        found.update(HTML_ID.findall(line))
        explicit = EXPLICIT_ID.search(line)
        heading = HEADING.match(line)
        if explicit:
            found.add(explicit.group(1))
        elif heading:
            found.add(slugify(heading.group(2)))
    found.discard("")  # a Chinese heading without {#id} gets a numbered id from Python-Markdown, never a usable one
    return found


def code_refs() -> dict[str, str]:
    """``page#anchor`` (or a bare page) -> where it comes from."""
    refs: dict[str, str] = {}
    patterns = json.loads((BACKEND / "services" / "diagnostics" / "patterns.json").read_text(encoding="utf-8"))
    for p in patterns["patterns"]:
        if p.get("doc"):
            refs[p["doc"]] = f"patterns.json {p['id']}"
    source = (BACKEND / "services" / "health_checks.py").read_text(encoding="utf-8")
    for ref in re.findall(r"""doc_anchor\s*=\s*["']([^"']+)["']""", source):
        refs[ref] = "health_checks.py doc_anchor"
    for ref in re.findall(r"""["']kind["']\s*:\s*["']docs["']\s*,\s*["']target["']\s*:\s*["']([^"']+)["']""", source):
        refs[ref] = "health_checks.py docs action"
    for ref in REQUIRED:
        refs.setdefault(ref, "required")
    return refs


def _split(ref: str) -> tuple[str, str]:
    page, _, anchor = ref.partition("#")
    return re.sub(r"\.md$", "", page.strip("/")) or "index", anchor


def test_the_scan_finds_the_known_references():
    refs = code_refs()
    assert {"operations#backups", "operations#restore", "operations#secret-key"} <= set(refs)
    assert "dev-environments#docker" in refs  # health check doc_anchor
    assert any(ref.startswith("faq.md") for ref in refs)  # diagnosis patterns


def test_slugify_matches_python_markdown():
    assert slugify("Setting it up (admin)") == "setting-it-up-admin"
    assert slugify("2. Configure") == "2-configure"
    assert slugify("Will it fit?") == "will-it-fit"
    assert slugify("Health checks and `slotserve doctor`") == "health-checks-and-slotserve-doctor"


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("ref", sorted(code_refs()))
def test_docs_anchor_exists(ref: str, lang: str):
    page, anchor = _split(ref)
    path = DOCS / (f"{page}.zh.md" if lang == "zh" else f"{page}.md")
    assert path.is_file(), f"{ref} ({code_refs()[ref]}): {path.relative_to(ROOT)} does not exist"
    if anchor:
        assert anchor in anchors(path), (
            f"{ref} ({code_refs()[ref]}): no heading with the anchor #{anchor} in {path.relative_to(ROOT)}"
            + (" (give the Chinese heading an explicit {#" + anchor + "})" if lang == "zh" else "")
        )
