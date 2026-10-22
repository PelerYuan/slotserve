# Release checklist

The release owner (a maintainer) goes through this list for every minor release. Patch releases skip the
release candidate when they only contain fixes that were verified on their own. Tick the boxes in the release
pull request.

!!! note "Skeleton"
    This checklist is being filled in; sections marked *(to be written)* get their details with the operations
    runbooks and the verification tooling.

## Image tags

| Tag pushed | Images published | GitHub release |
| --- | --- | --- |
| `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` | release, notes from the `X.Y.Z` section of `CHANGELOG.md` |
| `vX.Y.Z-rc1` (anything with a `-`) | `X.Y.Z-rc1`, `rc` | pre-release, notes from `[Unreleased]` plus `changes/unreleased/` |

A pre-release never moves `latest` or `X.Y`. `docker-compose.yml` follows a minor version (`slotserve:X.Y`), so
existing installations only pick up final releases of their minor.

To check the tag rules without publishing anything, run the **Release** workflow by hand (Actions → Release →
Run workflow) with a tag such as `v0.0.0-test`: it prints and checks the image tags and the notes, and builds,
pushes and releases nothing.

## 1. Before the release candidate

- [ ] Every change of the release has a fragment in `changes/unreleased/` or an entry under `[Unreleased]` in
      `CHANGELOG.md`; `python tools/release_notes.py check` passes and `preview` reads well.
- [ ] Changed behaviour of existing installations is listed under **Breaking changes**, with what to do.
- [ ] CI is green on `main` (lint, backend, frontend, end-to-end, docs, Docker image).
- [ ] Database migrations of the release are tested against a copy of the previous release's data directory.
- [ ] The minor version in `docker-compose.yml` and the docs is the one being released.
- [ ] Security review of the new write endpoints, credentials, container fields and published ports *(to be
      written: list of proof-of-concept checks)*.

## 2. Release candidate

- [ ] Bump the version in `backend/pyproject.toml`, `backend/slotserve/__init__.py` and `frontend/package.json`
      to `X.Y.Z` (the candidate is tagged on that commit).
- [ ] `git tag vX.Y.Z-rc1 && git push origin vX.Y.Z-rc1`; the workflow publishes `X.Y.Z-rc1` and `rc`.
- [ ] Verification on real hardware with the verification tooling; the report is attached to the release pull
      request *(to be written: verification matrix and report template)*.
- [ ] Upgrade from the previous release, backup and restore rehearsed *(to be written)*.
- [ ] The candidate runs on at least one real installation for **3 days or more** without new problems. Each fix
      gets a new candidate (`-rc2`, ...).

## 3. Release

- [ ] `python tools/release_notes.py release X.Y.Z` (moves `[Unreleased]` and the fragments under
      `## [X.Y.Z] - <today>` and deletes the fragments); review `CHANGELOG.md`.
- [ ] Commit (`Release X.Y.Z`), then `git tag vX.Y.Z && git push origin main vX.Y.Z`.
- [ ] The workflow publishes `X.Y.Z`, `X.Y` and `latest` and creates the GitHub release; check the release page and
      `docker pull ghcr.io/peleryuan/slotserve:X.Y`.

## 4. After the release

- [ ] The documentation site shows the new version's docs.
- [ ] Announce the release with its breaking changes *(to be written: where)*.
- [ ] Open the next `[Unreleased]` work; problems found after the release become `X.Y.(Z+1)` fixes.
