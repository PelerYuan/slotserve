"""Assemble CHANGELOG.md from the fragments in changes/unreleased/.

Every change records itself in its own file, `changes/unreleased/<branch-or-topic>.md`, instead of appending to
CHANGELOG.md (which every branch would touch). A fragment holds Keep-a-Changelog sections:

    ### Added

    - **Something new**: what it does for people.

    ### Fixed

    - What was broken.

CHANGELOG.md stays the published file: at release time `release` merges the fragments into its Unreleased
section, renames that section to the version and deletes the fragments.

    python tools/release_notes.py check                 # validate the fragments (CI, pre-commit)
    python tools/release_notes.py preview               # Unreleased + fragments, as the next release would read
    python tools/release_notes.py notes 0.10.0          # notes of a released version (GitHub release body)
    python tools/release_notes.py notes 0.10.0-rc1      # pre-release: the preview (fragments are not consumed)
    python tools/release_notes.py release 0.10.0 [--date 2026-11-01] [--dry-run]

Standard library only, so it runs anywhere (including the release workflow) without installing anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
FRAGMENTS = ROOT / "changes" / "unreleased"

# Order of the sections in a release. "Breaking changes" comes first so upgrade notes are not missed.
SECTIONS = ("Breaking changes", "Security", "Added", "Changed", "Deprecated", "Removed", "Fixed")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
RELEASE_RE = re.compile(r"^## \[(?P<name>[^\]]+)\]")
LINK_RE = re.compile(r"^\[(?P<name>[^\]]+)\]: (?P<url>\S+)\s*$")


class NotesError(Exception):
    pass


@dataclass
class Notes:
    """Text per section, in the order the sections first appeared."""

    sections: dict[str, list[str]] = field(default_factory=dict)

    def add(self, section: str, text: str) -> None:
        if text.strip():
            self.sections.setdefault(section, []).append(text.strip("\n"))

    def merge(self, other: Notes) -> None:
        for section, texts in other.sections.items():
            for text in texts:
                self.add(section, text)

    def render(self) -> str:
        known = [s for s in SECTIONS if s in self.sections]
        extra = [s for s in self.sections if s not in SECTIONS]
        blocks = [f"### {s}\n\n" + "\n\n".join(self.sections[s]) for s in known + extra]
        return "\n\n".join(blocks)


def parse_sections(text: str, source: str, *, strict: bool) -> Notes:
    """Split markdown into `### Section` blocks; text outside a section is an error.

    Strict (fragments): only the known sections, no other headings, no empty sections.
    """
    notes = Notes()
    current: str | None = None
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip("\n")
        if current is None:
            if body.strip():
                raise NotesError(f"{source}: text before the first '### <section>' heading")
        elif strict and not body.strip():
            raise NotesError(f"{source}: section '{current}' is empty")
        else:
            notes.add(current, body)

    for line in text.splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            flush()
            current, lines = heading.group(1), []
            if strict and current not in SECTIONS:
                raise NotesError(f"{source}: unknown section '{current}' (use one of: {', '.join(SECTIONS)})")
        elif line.startswith("#") and strict:
            raise NotesError(f"{source}: only '### <section>' headings are allowed, found {line!r}")
        else:
            lines.append(line)
    flush()
    return notes


def fragment_files(directory: Path = FRAGMENTS) -> list[Path]:
    return sorted(p for p in directory.glob("*.md") if p.name.lower() != "readme.md") if directory.is_dir() else []


def read_fragments(directory: Path = FRAGMENTS) -> Notes:
    notes = Notes()
    for path in fragment_files(directory):
        fragment = parse_sections(path.read_text(encoding="utf-8"), path.name, strict=True)
        if not fragment.sections:
            raise NotesError(f"{path.name}: no '### <section>' with content")
        notes.merge(fragment)
    return notes


@dataclass
class Changelog:
    header: str  # everything before the first "## [" heading
    releases: list[tuple[str, str]]  # (heading line, body) in file order
    links: list[str]  # "[name]: url" lines at the end

    @classmethod
    def parse(cls, text: str) -> Changelog:
        lines = text.splitlines()
        links: list[str] = []
        while lines and (not lines[-1].strip() or LINK_RE.match(lines[-1])):
            line = lines.pop()
            if line.strip():
                links.insert(0, line)
        header: list[str] = []
        releases: list[tuple[str, list[str]]] = []
        for line in lines:
            if RELEASE_RE.match(line):
                releases.append((line, []))
            elif releases:
                releases[-1][1].append(line)
            else:
                header.append(line)
        return cls("\n".join(header).strip("\n"), [(h, "\n".join(b).strip("\n")) for h, b in releases], links)

    def render(self) -> str:
        parts = [self.header] + [f"{h}\n\n{b}" if b else h for h, b in self.releases]
        text = "\n\n".join(parts) + "\n"
        if self.links:
            text += "\n" + "\n".join(self.links) + "\n"
        return text

    def find(self, name: str) -> int | None:
        for i, (heading, _) in enumerate(self.releases):
            match = RELEASE_RE.match(heading)
            if match and match.group("name").lower() == name.lower():
                return i
        return None

    def body(self, name: str) -> str | None:
        i = self.find(name)
        return None if i is None else self.releases[i][1]


def load_changelog(path: Path = CHANGELOG) -> Changelog:
    return Changelog.parse(path.read_text(encoding="utf-8"))


def unreleased_notes(changelog: Changelog, fragments: Path = FRAGMENTS) -> Notes:
    notes = parse_sections(changelog.body("Unreleased") or "", "CHANGELOG.md [Unreleased]", strict=False)
    notes.merge(read_fragments(fragments))
    return notes


def release_notes(version: str, changelog: Changelog, fragments: Path = FRAGMENTS) -> str:
    body = changelog.body(version)
    if body is not None:
        return body
    if "-" in version:  # a release candidate: what the next release will contain so far
        return unreleased_notes(changelog, fragments).render()
    raise NotesError(f"CHANGELOG.md has no section for {version}; run `release {version}` before tagging")


def make_release(version: str, date: str, changelog: Changelog, fragments: Path = FRAGMENTS) -> Changelog:
    if not VERSION_RE.match(version) or "-" in version:
        raise NotesError(f"{version!r} is not a release version (X.Y.Z); pre-releases do not consume fragments")
    if changelog.find(version) is not None:
        raise NotesError(f"CHANGELOG.md already has a section for {version}")
    notes = unreleased_notes(changelog, fragments)
    if not notes.sections:
        raise NotesError("nothing to release: Unreleased and changes/unreleased/ are empty")
    i = changelog.find("Unreleased")
    releases = list(changelog.releases)
    new = (f"## [{version}] - {date}", notes.render())
    if i is None:
        releases.insert(0, ("## [Unreleased]", ""))
        i = 0
    releases[i] = ("## [Unreleased]", "")
    releases.insert(i + 1, new)
    return Changelog(changelog.header, releases, update_links(changelog.links, version))


def update_links(links: list[str], version: str) -> list[str]:
    """`[Unreleased]: .../compare/vPREV...HEAD` becomes `.../compare/vX...HEAD` plus `[X]: .../compare/vPREV...vX`."""
    out = list(links)
    for i, line in enumerate(out):
        match = LINK_RE.match(line)
        if not match or match.group("name").lower() != "unreleased":
            continue
        url = match.group("url")
        compare = re.match(r"^(?P<base>.+/compare/)(?P<prev>[^.][^/]*?)\.\.\.HEAD$", url)
        if not compare:
            return out
        base, prev = compare.group("base"), compare.group("prev")
        out[i] = f"[Unreleased]: {base}v{version}...HEAD"
        out.insert(i + 1, f"[{version}]: {base}{prev}...v{version}")
        return out
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate changes/unreleased/*.md")
    sub.add_parser("preview", help="print the notes the next release would get")
    notes_cmd = sub.add_parser("notes", help="print the notes of a version (pre-release: the preview)")
    notes_cmd.add_argument("version")
    release_cmd = sub.add_parser("release", help="move Unreleased + fragments under a version heading")
    release_cmd.add_argument("version")
    release_cmd.add_argument("--date", default=dt.date.today().isoformat())
    release_cmd.add_argument("--dry-run", action="store_true", help="print the new CHANGELOG.md, change nothing")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the notes are UTF-8 whatever the console uses

    try:
        if args.command == "check":
            read_fragments()
            print(f"{len(fragment_files())} fragment(s) OK")
        elif args.command == "preview":
            print(unreleased_notes(load_changelog()).render())
        elif args.command == "notes":
            print(release_notes(args.version, load_changelog()))
        else:
            changelog = make_release(args.version, args.date, load_changelog())
            if args.dry_run:
                print(changelog.render(), end="")
            else:
                CHANGELOG.write_text(changelog.render(), encoding="utf-8", newline="\n")
                for path in fragment_files():
                    path.unlink()
                print(f"CHANGELOG.md: released {args.version}; removed the fragments (commit both)")
    except NotesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
