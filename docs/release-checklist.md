# Release checklist

The release owner (a maintainer) goes through this list for every minor release. Patch releases skip the
release candidate when they only contain fixes that were verified on their own. Tick the boxes in the release
pull request.

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
- [ ] The docs are current: `mkdocs build --strict` passes, `backend/tests/test_docs_anchors.py` passes, the
      [operations runbooks](operations.md) cover every new setting an admin must know, and the upgrade section
      lists what people will notice.
- [ ] Security review of what the release adds (see [Security review](#security-review) below), signed off in the
      release pull request.

## 2. Release candidate

- [ ] Bump the version in `backend/pyproject.toml`, `backend/slotserve/__init__.py` and `frontend/package.json`
      to `X.Y.Z` (the candidate is tagged on that commit).
- [ ] `git tag vX.Y.Z-rc1 && git push origin vX.Y.Z-rc1`; the workflow publishes `X.Y.Z-rc1` and `rc`.
- [ ] Verification on real hardware with `python -m tools.verify` across the [verification matrix](#verification-matrix);
      every report (`verify-report-<host>-<version>.md`) is attached to the release pull request, with the manual
      checks filled in and every failure turned into an issue.
- [ ] [Upgrade, backup and restore rehearsed](#rehearsal) on a copy of real data.
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
- [ ] Announce the release with its breaking changes: the GitHub release page (its notes come from the changelog)
      links the [upgrade runbook](operations.md#upgrade); point the issue tracker's pinned discussion or the
      project's chat group at it.
- [ ] Open the next `[Unreleased]` work; problems found after the release become `X.Y.(Z+1)` fixes.

## Security review {#security-review}

Before the first release candidate, a maintainer who did not write the change goes through what the release adds and
checks each item with a proof of concept against a running installation (most are also covered by tests; the
point is to see them fail on a real browser and a real Docker host):

- [ ] **New write endpoints** are covered by the CSRF test generated from the routes; in a browser, the CSRF page
      written by `tools/verify` changes nothing from another origin, from a direct port or from the web port.
- [ ] **Sign-in**: a cookie signed with the old default key is refused; the first admin needs the setup code;
      throttling locks per account and address; changing a password signs out other sessions.
- [ ] **Credentials**: new kinds of keys are scoped and expire; no secret appears in logs, the audit log, `doctor
      --json`, `/api/health` evidence or a saved image (`docker inspect`, `docker history`).
- [ ] **Container fields**: every new field of the container spec is in the golden spec tests and reviewed; no new
      privilege, capability, host namespace, device or bind mount outside the allowed folders; hardening holds
      (`no-new-privileges`, dropped capabilities, process and memory limits).
- [ ] **Published ports**: every port the release opens is in [Ports and firewall](operations.md#firewall), is
      behind the SlotServe login or needs an explicit confirmation, and dev environment pages cannot act in
      SlotServe or in another environment (the web origin checks of `tools/verify`).
- [ ] **Root-equivalent settings** (engine commands, mounted folders, images) ask for the password again and are
      announced to all admins.

## Verification matrix {#verification-matrix}

Run `tools/verify` (see [Verify on real hardware](operations.md#verify)) on enough installations to cover:

| Dimension | Cover |
| --- | --- |
| Machines | A Linux machine with an NVIDIA GPU; a machine without GPU |
| Deployment | Docker Compose with the published candidate image; without Docker |
| Dev environments | Every built-in template booked with a GPU, without GPU, kept and continued, saved as an image; a shared idle environment |
| Logins | SSH key, environment password, web interfaces through a reverse proxy with HTTPS |
| Storage | Host folder, NFS, SMB |
| Engines | llama.cpp; vLLM |
| Notifications | Telegram and one other chat platform; the inbox |
| Browsers | Chrome, Firefox, Safari |
| Failures | Out of memory in an environment, a fork bomb (`--destructive`), Docker restarted, host rebooted, disk nearly full: the health table and the alerts tell the truth, and SlotServe recovers |

Each report records the host, versions (SlotServe, driver, Docker) and the results; the manual browser checks are
ticked in the report by the person who ran them.

## Upgrade, backup and restore rehearsal {#rehearsal}

- [ ] A copy of a real data directory of the previous release starts on the candidate: snapshot taken, migration
      report clean, the health table as expected; then roll back to the snapshot with the previous image and
      upgrade again ([Roll back](operations.md#rollback)).
- [ ] The daily bundle is copied to a backup target on another machine (a NAS path and a managed storage).
- [ ] A bundle is restored on another machine with `slotserve restore`; the [reconciliation
      report](operations.md#reconcile) matches what really happened.
- [ ] An admin who did not write the runbooks resets an admin password and restores from a backup using only
      [Operations](operations.md); every point where they got stuck is fixed in the docs.
