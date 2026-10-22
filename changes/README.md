# Changelog fragments

Do not edit `CHANGELOG.md` for a change. Add a file to `changes/unreleased/` instead, named after your branch or
topic (`changes/unreleased/quickbook.md`), so parallel branches never touch the same file:

```markdown
### Added

- **Quick booking**: one dialog with an intent, a duration and a preview of what you get.

### Fixed

- The schedule kept a cancelled booking until the page was reloaded.
```

- Sections: `Breaking changes`, `Security`, `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed` (only `###`
  headings, no text outside a section). Write for the people who run and use SlotServe, as in `CHANGELOG.md`.
- One fragment per branch is usual; add to it as the branch grows. Several files are fine too.
- `python tools/release_notes.py check` validates the fragments, `preview` prints the notes of the next release.

At release time the maintainer runs `python tools/release_notes.py release X.Y.Z`: it merges the `[Unreleased]`
section of `CHANGELOG.md` and all fragments into a new `## [X.Y.Z] - date` section, updates the comparison links
and deletes the fragments. Release candidates (`vX.Y.Z-rc1`) take their notes from the preview and consume
nothing. See `docs/release-checklist.md`.
