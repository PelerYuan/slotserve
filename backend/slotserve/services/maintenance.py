"""Backups (bundles, the off-machine copy, restore and reconciliation) and data retention.

A backup is a **bundle**, a folder ``backups/slotserve-<daily|manual>-<stamp>/`` with

- ``slotserve.db``: a consistent copy of the database (SQLite backup API, safe while the server writes);
- ``secret_key``: the generated key in the data directory (without it, stored secrets cannot be decrypted and
  everyone is signed out); absent when the key comes from ``SLOTSERVE_SECRET_KEY``, which the admin keeps;
- ``devenv/``: the dev environment SSH host keys (without them every ``known_hosts`` complains);
- ``manifest.json``: SlotServe and schema version, the key's fingerprint (never the key), file sizes and
  checksums, row counts, the storages and kept environments, and what a bundle does **not** contain.

Not in a bundle: workspaces, project and kept-environment data, container layers, saved images, models and
managed storage. ``backup_target`` copies each daily bundle off this machine: an absolute path SlotServe can
write (a mounted NAS share) or ``storage:<id>``, a storage from Admin -> Storage, written through a short-lived
helper container. ``slotserve restore <bundle>`` puts a bundle back; the next start compares the restored
database with what Docker and the disk really have (``reconcile``) and shows the result until an admin dismisses
it, and until then nothing that the database does not know is removed.
"""

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import Settings
from ..orm import (
    AuditLog,
    DevProject,
    DevSession,
    DownloadTask,
    LlmModel,
    ModelLoadEvent,
    Notification,
    Storage,
    UsageDaily,
    UsageLog,
    User,
    WaitlistEntry,
)
from . import kv, usage_sampler

log = logging.getLogger(__name__)

STAMP = "%Y%m%d-%H%M%S"
BUNDLE_RE = re.compile(r"^slotserve-(daily|manual|prerestore)-(\d{8}-\d{6})$")
LEGACY_RE = re.compile(r"^slotserve-(daily|manual)-(\d{8}-\d{6})\.db$")  # before 0.10: the database alone
UPGRADE_RE = re.compile(r"^slotserve-v.+-to-v.+-(\d{8}-\d{6})$")  # db.py: snapshot before an upgrade
# Backwards compatible name (single-file backups); BUNDLE_RE and LEGACY_RE describe what list_backups returns.
BACKUP_RE = LEGACY_RE
LOAD_EVENTS_KEPT = 50  # per model; estimates only use the last few
STALE_HOURS = 36  # the newest backup older than this is an error (check backup_age)
MIN_GAP = timedelta(hours=6)  # between two daily bundles when backup_hour moves the schedule
OFFSITE_DIR = "slotserve-backups"  # folder on the backup target
STATUS_KEY = "backup_status"
PENDING_RESTORE = "restore_pending.json"
RESTORE_REPORT = "restore_report.json"
NOT_INCLUDED = ("workspaces", "project_data", "container_layers", "saved_images", "models", "managed_storage")
COPY_TIMEOUT = 1800.0
FORMAT = 1
_bundle_lock = threading.Lock()  # one bundle at a time (the daily one and a manual one may meet)


class BackupError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---- bundles -------------------------------------------------------------------


def backup_dir(settings: Settings) -> Path:
    return settings.data_dir / "backups"


def stamp_of(path: Path) -> datetime | None:
    """When a backup was taken, from its name (copies keep their names, not their modification times)."""
    for pattern in (BUNDLE_RE, LEGACY_RE):
        if m := pattern.match(path.name):
            return datetime.strptime(m.group(2), STAMP).replace(tzinfo=UTC)
    if m := UPGRADE_RE.match(path.name):
        return datetime.strptime(m.group(1), STAMP).replace(tzinfo=UTC)
    return None


def kind_of(path: Path) -> str:
    """daily | manual | prerestore | upgrade"""
    m = BUNDLE_RE.match(path.name) or LEGACY_RE.match(path.name)
    return m.group(1) if m else "upgrade"


def is_backup(path: Path) -> bool:
    if path.is_dir():
        return bool(BUNDLE_RE.match(path.name) or UPGRADE_RE.match(path.name))
    return bool(LEGACY_RE.match(path.name)) or (path.suffix == ".db" and path.name.startswith("slotserve-v"))


def list_backups(settings: Settings) -> list[Path]:
    """Bundles, single-file backups from earlier versions and upgrade snapshots; newest first."""
    root = backup_dir(settings)
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if is_backup(p)]
    epoch = datetime.min.replace(tzinfo=UTC)

    def when(p: Path) -> datetime:
        return stamp_of(p) or datetime.fromtimestamp(p.stat().st_mtime, UTC) or epoch

    return sorted(found, key=when, reverse=True)


def database_of(path: Path) -> Path:
    """The database file of a backup (a bundle's ``slotserve.db``, or the file itself)."""
    return path / "slotserve.db" if path.is_dir() else path


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def newest(settings: Settings, kinds: tuple[str, ...] = ("daily", "manual")) -> datetime | None:
    stamps = [stamp_of(p) for p in list_backups(settings) if kind_of(p) in kinds]
    return max((s for s in stamps if s is not None), default=None)


def key_fingerprint(key: str | None) -> str | None:
    """A short public fingerprint of the secret key (tells two keys apart, reveals nothing)."""
    if not key:
        return None
    return hashlib.sha256(b"slotserve-secret-key:" + key.strip().encode()).hexdigest()[:16]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(db_path: Path) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    try:
        out = {}
        for table in ("users", "reservations", "models", "devenv_projects", "storages", "api_keys"):
            with contextlib.suppress(sqlite3.Error):
                out[table] = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        out["schema_version"] = con.execute("PRAGMA user_version").fetchone()[0]
        return out
    finally:
        con.close()


def make_backup(settings: Settings, kind: str = "daily", extra: dict | None = None) -> Path:
    """Writes a bundle (see the module docstring) and prunes old ones; returns its folder. Blocking."""
    with _bundle_lock:
        return _make_backup(settings, kind, extra)


def _make_backup(settings: Settings, kind: str, extra: dict | None) -> Path:
    root = backup_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC)
    final = root / f"slotserve-{kind}-{stamp.strftime(STAMP)}"
    n = 1
    while (
        final.exists() or final.with_name(final.name + ".partial").exists()
    ):  # two in the same second (tests, a double click)
        final = root / f"slotserve-{kind}-{(stamp + timedelta(seconds=n)).strftime(STAMP)}"
        n += 1
    partial = final.with_name(final.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()
    try:
        src = sqlite3.connect(settings.data_dir / "slotserve.db")
        dst = sqlite3.connect(partial / "slotserve.db")
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()
        key_file = settings.data_dir / "secret_key"
        key_in_file = key_file.is_file() and key_file.read_text(encoding="utf-8").strip() == settings.secret_key
        if key_in_file:
            shutil.copy2(key_file, partial / "secret_key")
        if (settings.data_dir / "devenv").is_dir():
            shutil.copytree(settings.data_dir / "devenv", partial / "devenv")
        files = {
            p.relative_to(partial).as_posix(): {"size": p.stat().st_size, "sha256": _sha256(p)}
            for p in sorted(partial.rglob("*"))
            if p.is_file()
        }
        counts = _counts(partial / "slotserve.db")
        manifest = {
            "format": FORMAT,
            "kind": kind,
            "created_at": stamp.isoformat(timespec="seconds"),
            "app_version": __version__,
            "schema_version": counts.pop("schema_version"),
            "secret_key": {
                "fingerprint": key_fingerprint(settings.secret_key),
                "included": key_in_file,
                "source": "data_dir" if key_in_file else "environment",
            },
            "files": files,
            "rows": counts,
            "not_included": list(NOT_INCLUDED),
            **(extra or {}),
        }
        (partial / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8", newline="\n"
        )
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    prune_backups(settings)
    return final


def read_manifest(path: Path) -> dict:
    try:
        return json.loads((path / "manifest.json").read_text(encoding="utf-8")) if path.is_dir() else {}
    except (OSError, ValueError):
        return {}


def prune_backups(settings: Settings) -> None:
    """Keeps the newest ``backup_keep`` daily and manual backups (bundles and old single files alike); upgrade
    snapshots and pre-restore copies are never removed."""
    keep = settings.backup_keep
    if keep <= 0:
        return
    for kind in ("daily", "manual"):
        for p in [p for p in list_backups(settings) if kind_of(p) == kind][keep:]:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def backup_due(settings: Settings, now: datetime) -> bool:
    """A daily bundle is due when none was taken since the last ``backup_hour`` (local time; -1: every 24 h)."""
    if settings.backup_keep <= 0 or not (settings.data_dir / "slotserve.db").is_file():
        return False
    last = newest(settings, ("daily",))
    if last is None:
        return True
    hour = getattr(settings, "backup_hour", -1)
    if hour is None or hour < 0:
        return now - last >= timedelta(hours=24)
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return last < slot.astimezone(UTC) and now - last >= MIN_GAP


async def inventory(db: AsyncSession) -> dict[str, Any]:
    """What lives outside the bundle, for its manifest: storages and kept environments."""
    storages = (await db.execute(select(Storage).order_by(Storage.id))).scalars().all()
    projects = (await db.execute(select(DevProject).order_by(DevProject.id))).scalars().all()
    owners = dict((await db.execute(select(User.id, User.username))).all())
    return {
        "storages": [{"id": s.id, "name": s.name, "kind": s.kind, "purpose": s.purpose} for s in storages],
        "kept_environments": [
            {"id": p.id, "name": p.name, "owner": owners.get(p.owner_id, "?"), "state": p.state, "size": p.size_bytes}
            for p in projects
        ],
    }


# ---- the off-machine copy ----------------------------------------------------------


def parse_target(value: str | None) -> tuple[str, Any] | None:
    """``backup_target``: empty (no copy), ``storage:<id>`` or an absolute path. Raises BackupError."""
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("storage:"):
        ident = value.split(":", 1)[1].strip()
        if not ident.isdigit():
            raise BackupError("backup_target_invalid", value)
        return ("storage", int(ident))
    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
        return ("path", Path(value))
    raise BackupError("backup_target_invalid", value)


def copy_to_path(bundle: Path, target: Path, keep: int) -> Path:
    """Copies a bundle into ``<target>/slotserve-backups/`` (whole or not at all) and prunes old copies."""
    if not target.is_dir():
        raise BackupError("backup_target_missing", str(target))
    folder = target / OFFSITE_DIR
    folder.mkdir(exist_ok=True)
    dest = folder / bundle.name
    partial = folder / (bundle.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(bundle, partial)
    shutil.rmtree(dest, ignore_errors=True)
    partial.rename(dest)
    if keep > 0:
        for kind in ("daily", "manual"):
            copies = sorted(
                (p for p in folder.iterdir() if p.is_dir() and kind_of(p) == kind and BUNDLE_RE.match(p.name)),
                key=lambda p: p.name,
                reverse=True,
            )
            for old in copies[keep:]:
                shutil.rmtree(old, ignore_errors=True)
    return dest


def _tar_dir(path: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(path, arcname=arcname)
    return buf.getvalue()


def _q(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


async def copy_to_storage(docker, settings: Settings, st: Storage, bundle: Path, keep: int) -> str:
    """Copies a bundle onto a managed storage through a helper container (the storage helper image); the bundle
    goes in as an archive, so SlotServe never needs the storage mounted itself."""
    from . import storage as storage_mod

    image = settings.storage_helper_image
    folder = f"/dst/{OFFSITE_DIR}"
    name = bundle.name
    script = "\n".join(
        [
            "set -e",
            f"mkdir -p {_q(folder)}",
            f"rm -rf {_q(f'{folder}/{name}.partial')}",
            f"cp -R {_q(f'/tmp/incoming/{name}')} {_q(f'{folder}/{name}.partial')}",
            f"rm -rf {_q(f'{folder}/{name}')}",
            f"mv {_q(f'{folder}/{name}.partial')} {_q(f'{folder}/{name}')}",
            f"cd {_q(folder)}",
            *(
                [
                    "for kind in daily manual; do",
                    "  ls -1d slotserve-$kind-* 2>/dev/null | grep -v '\\.partial$' | sort -r"
                    f' | tail -n +{keep + 1} | while read -r d; do rm -rf "$d"; done',
                    "done",
                ]
                if keep > 0
                else []
            ),
            "echo copied",
        ]
    )
    if not await docker.image_exists(image):
        await docker.pull(image)
    mount, volumes = storage_mod._helper_mount(st, settings.secret_key, "/dst")
    await storage_mod.ensure_volumes(docker, volumes)
    archive = await asyncio.to_thread(_tar_dir, bundle, f"incoming/{name}")
    config = {
        "Image": image,
        "Entrypoint": ["/bin/sh", "-c", script],
        "Cmd": [],
        "User": "root",
        "Labels": {storage_mod.LABEL: "helper"},
        "HostConfig": {"Mounts": [mount]},
    }
    container = "slotserve-backup-copy"
    await docker.remove(container)
    cid = await docker.create(container, config)
    try:
        await docker.put_archive(cid, "/tmp", archive)
        await docker.start(cid)
        code = await docker.wait(cid, COPY_TIMEOUT)
        output = await docker.logs(cid, 200)
    finally:
        with contextlib.suppress(Exception):
            await docker.remove(cid)
    if code != 0:
        raise BackupError("backup_copy_failed", f"exit code {code}: {output.strip()[-500:]}")
    return f"{st.name}:{OFFSITE_DIR}/{name}"


async def copy_offsite(settings: Settings, docker, bundle: Path) -> str | None:
    """Copies a bundle to ``backup_target``; returns where it went, ``None`` without a target."""
    target = parse_target(getattr(settings, "backup_target", ""))
    if target is None:
        return None
    kind, where = target
    keep = settings.backup_keep
    if kind == "path":
        try:
            dest = await asyncio.to_thread(copy_to_path, bundle, where, keep)
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError("backup_copy_failed", f"{type(exc).__name__}: {exc}") from exc
        return str(dest)
    from ..db import SessionLocal

    async with SessionLocal() as db:
        st = await db.get(Storage, where)
    if st is None:
        raise BackupError("backup_target_missing", f"storage:{where}")
    if docker is None:
        raise BackupError("backup_copy_failed", "Docker is needed to copy to a managed storage")
    try:
        return await copy_to_storage(docker, settings, st, bundle, keep)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("backup_copy_failed", f"{type(exc).__name__}: {exc}") from exc


# ---- status (what the backup checks read) ---------------------------------------------


async def load_status(db: AsyncSession) -> dict[str, Any]:
    return await kv.get(db, STATUS_KEY) or {}


async def save_status(db: AsyncSession, status: dict[str, Any]) -> None:
    await kv.put(db, STATUS_KEY, status)
    await db.commit()


async def run_backup(settings: Settings, docker=None, kind: str = "daily", *, offsite: bool = True) -> dict[str, Any]:
    """Takes a bundle and records how it went (``backup_status``); with ``offsite`` also copies it off the
    machine (``run_offsite``). Raises when the bundle could not be written."""
    from ..db import SessionLocal, utcnow

    async with SessionLocal() as db:
        status = await load_status(db)
        extra = await inventory(db)
    now = utcnow()
    status["last_attempt_at"] = now.isoformat()
    try:
        bundle = await asyncio.to_thread(make_backup, settings, kind, extra)
    except Exception as exc:
        status["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        status["last_error_at"] = now.isoformat()
        async with SessionLocal() as db:
            await save_status(db, status)
        raise
    status.update(last_ok_at=now.isoformat(), last_bundle=bundle.name, last_error=None, last_error_at=None)
    async with SessionLocal() as db:
        await save_status(db, status)
    if offsite:
        status = await run_offsite(settings, docker, bundle)
    return status


async def run_offsite(settings: Settings, docker, bundle: Path) -> dict[str, Any]:
    """Copies a bundle to ``backup_target`` (if set) and records the outcome under ``backup_status.offsite``."""
    from ..db import SessionLocal, utcnow

    target = getattr(settings, "backup_target", "") or ""
    result: dict[str, Any] = {"target": target, "last_attempt_at": utcnow().isoformat()}
    if target:
        try:
            where = await copy_offsite(settings, docker, bundle)
            result.update(last_ok_at=utcnow().isoformat(), last_bundle=bundle.name, last_error=None, where=where)
        except BackupError as exc:
            result.update(last_error=exc.detail or exc.code, code=exc.code, last_error_at=utcnow().isoformat())
            log.warning("copying backup %s to %s failed: %s", bundle.name, target, exc)
    async with SessionLocal() as db:
        status = await load_status(db)
        previous = status.get("offsite") or {}
        if target and not result.get("last_ok_at") and previous.get("target") == target:
            result.setdefault("last_ok_at", previous.get("last_ok_at"))  # when it last worked
            result.setdefault("where", previous.get("where"))
        status["offsite"] = result if target else None
        await save_status(db, status)
    return status


# ---- restore ------------------------------------------------------------------------


class RestoreError(Exception):
    pass


def restore(settings: Settings, source: Path, *, restore_key: bool = True) -> dict[str, Any]:
    """Puts a bundle (or an old single-file backup) back into the data directory. The server must be stopped.

    Refuses a backup from a newer schema or one that fails SQLite's integrity check; saves what is there now as a
    ``prerestore`` bundle first; writes ``restore_pending.json`` so the next start reconciles. Returns what it did
    (``warnings`` lists what the admin must know).
    """
    from ..db import SCHEMA_VERSION

    source = Path(source)
    db_file = database_of(source)
    if not db_file.is_file():
        raise RestoreError(f"{source} is not a SlotServe backup (no slotserve.db)")
    manifest = read_manifest(source)
    con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    try:
        check = con.execute("PRAGMA quick_check").fetchone()[0]
        version = con.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"{db_file} is not a readable SQLite database: {exc}") from exc
    finally:
        con.close()
    if check != "ok":
        raise RestoreError(f"{db_file} failed SQLite's integrity check: {check}")
    if version > SCHEMA_VERSION:
        raise RestoreError(
            f"{source} comes from a newer SlotServe (schema {version}, this version knows {SCHEMA_VERSION}); "
            "restore it with that version"
        )
    warnings: list[str] = []
    data = settings.data_dir
    data.mkdir(parents=True, exist_ok=True)
    key_src = source / "secret_key" if source.is_dir() else None
    current_key = (data / "secret_key").read_text(encoding="utf-8").strip() if (data / "secret_key").is_file() else ""
    bundle_fp = (manifest.get("secret_key") or {}).get("fingerprint")
    if key_src is not None and key_src.is_file():
        bundle_key = key_src.read_text(encoding="utf-8").strip()
        bundle_fp = key_fingerprint(bundle_key)
    else:
        bundle_key = None
    effective = settings.secret_key if settings.secret_key not in ("", "change-me") else current_key
    if bundle_key is not None and not restore_key:
        warnings.append("the secret key was not restored (--keep-key): secrets stored by the backup cannot be read")
    elif bundle_key is None and bundle_fp and bundle_fp != key_fingerprint(effective):
        warnings.append(
            "this backup was made with a different secret key that is not in the bundle (it came from "
            "SLOTSERVE_SECRET_KEY): set that key again, or stored secrets cannot be decrypted"
        )
    prerestore = None
    if (data / "slotserve.db").is_file():
        prerestore = make_backup(settings, "prerestore")
    for suffix in ("-wal", "-shm", "-journal"):
        (data / f"slotserve.db{suffix}").unlink(missing_ok=True)
    tmp = data / "slotserve.db.restoring"
    shutil.copy2(db_file, tmp)
    os.replace(tmp, data / "slotserve.db")
    key_restored = False
    if bundle_key is not None and restore_key:
        if bundle_key != current_key:
            (data / "secret_key").unlink(missing_ok=True)
            from ..security.secret_key import write_private

            write_private(data / "secret_key", bundle_key + "\n")
        key_restored = True
        if settings.secret_key not in ("", "change-me") and settings.secret_key != bundle_key:
            warnings.append(
                "SLOTSERVE_SECRET_KEY is set and overrides the restored data/secret_key; remove it (or set it to "
                "the restored key) so stored secrets can be decrypted"
            )
    if source.is_dir() and (source / "devenv").is_dir():
        shutil.copytree(source / "devenv", data / "devenv", dirs_exist_ok=True)
    if version < SCHEMA_VERSION:
        warnings.append(f"the backup has schema {version}: the next start upgrades it (after its own snapshot)")
    (data / RESTORE_REPORT).unlink(missing_ok=True)
    info = {
        "bundle": source.name,
        "path": str(source),
        "restored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "backup_created_at": manifest.get("created_at") or (stamp_of(source) or datetime.now(UTC)).isoformat(),
        "app_version": manifest.get("app_version"),
        "schema_version": version,
        "secret_key_restored": key_restored,
        "prerestore": str(prerestore) if prerestore else None,
        "warnings": warnings,
    }
    (data / PENDING_RESTORE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def restore_pending(settings: Settings) -> dict | None:
    try:
        return json.loads((settings.data_dir / PENDING_RESTORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def restore_hold(settings: Settings) -> bool:
    """True from a restore until an admin dismissed its report: nothing the database does not know is removed."""
    return (settings.data_dir / PENDING_RESTORE).is_file()


def restore_report(settings: Settings) -> dict | None:
    try:
        return json.loads((settings.data_dir / RESTORE_REPORT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def finish_restore(settings: Settings) -> None:
    """The admin has seen the reconciliation report: normal cleanup resumes."""
    (settings.data_dir / PENDING_RESTORE).unlink(missing_ok=True)


async def reconcile(settings: Settings, docker, db: AsyncSession) -> dict[str, Any]:
    """Compares the database with Docker and the disk; removes and changes nothing.

    ``containers_missing``: environments the database has a container for that Docker does not have;
    ``containers_unknown``: SlotServe containers the database does not know (made after the backup);
    ``models_missing``: model files that are not where the database says. Storages are listed for a re-check.
    """
    from ..db import utcnow

    report: dict[str, Any] = {"at": utcnow().isoformat(timespec="seconds"), "docker": "off"}
    expected: dict[str, dict] = {}
    for s in (
        await db.execute(
            select(DevSession).where(
                DevSession.container_id.is_not(None), DevSession.state.in_(("starting", "running", "stopping"))
            )
        )
    ).scalars():
        expected[s.container_id] = {"kind": "session", "id": s.id, "name": s.container_name or "", "state": s.state}
    for p in (await db.execute(select(DevProject).where(DevProject.container_id.is_not(None)))).scalars():
        expected[p.container_id] = {"kind": "kept_environment", "id": p.id, "name": p.name, "state": p.state}
    missing, unknown = [], []
    if docker is not None and (settings.devenv_enabled or expected):
        try:
            containers = await docker.list("slotserve.devenv=1")
            report["docker"] = "ok"
        except Exception as exc:  # noqa: BLE001
            containers = None
            report["docker"] = "unreachable"
            report["docker_error"] = f"{type(exc).__name__}: {exc}"[:300]
        if containers is not None:
            present = {c["Id"] for c in containers}
            missing = [dict(v, container=k[:12]) for k, v in expected.items() if k not in present]
            for c in containers:
                labels = c.get("Labels") or {}
                if labels.get("slotserve.devenv") == "helper" or c["Id"] in expected:
                    continue
                unknown.append(
                    {
                        "container": c["Id"][:12],
                        "name": ((c.get("Names") or ["?"])[0]).lstrip("/"),
                        "state": c.get("State"),
                        "session": labels.get("slotserve.session"),
                        "kept_environment": labels.get("slotserve.project"),
                    }
                )
    models_missing = []
    for m in (await db.execute(select(LlmModel).where(LlmModel.deleted_at.is_(None)))).scalars():
        path = Path(m.file_path)
        if (m.engine is None or path.is_absolute()) and m.status == "ready" and not path.exists():
            models_missing.append({"id": m.id, "name": m.name, "path": m.file_path})
    storages = [
        {"id": s.id, "name": s.name, "kind": s.kind}
        for s in (await db.execute(select(Storage).where(Storage.enabled.is_(True)))).scalars()
    ]
    report.update(
        containers_missing=missing,
        containers_unknown=unknown,
        models_missing=models_missing,
        storages_to_check=storages,
        not_checked=["volumes", "ports"],
        summary={
            "containers_missing": len(missing),
            "containers_unknown": len(unknown),
            "models_missing": len(models_missing),
        },
    )
    return report


def write_restore_report(settings: Settings, report: dict) -> None:
    (settings.data_dir / RESTORE_REPORT).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


# ---- retention -----------------------------------------------------------------


async def apply_retention(db: AsyncSession, settings: Settings, now: datetime) -> dict[str, int]:
    """Rolls old request rows up into ``usage_daily`` and deletes rows past their retention."""
    stats: dict[str, int] = {}
    if settings.usage_retention_days > 0:
        cutoff = now - timedelta(days=settings.usage_retention_days)
        day = func.strftime("%Y-%m-%d", UsageLog.created_at)
        rows = (
            await db.execute(
                select(
                    day,
                    UsageLog.user_id,
                    UsageLog.model_name,
                    func.count(UsageLog.id),
                    func.sum(UsageLog.status_code >= 400),
                    func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                    func.coalesce(func.sum(UsageLog.duration_ms), 0),
                )
                .where(UsageLog.created_at < cutoff)
                .group_by(day, UsageLog.user_id, UsageLog.model_name)
            )
        ).all()
        for d, uid, model, n, failed, prompt, completion, ms in rows:
            model = model or ""
            existing = await db.get(UsageDaily, (d, uid, model))
            if existing is None:
                existing = UsageDaily(
                    date=d,
                    user_id=uid,
                    model_name=model,
                    requests=0,
                    failed=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    duration_ms=0,
                )
                db.add(existing)
            existing.requests += n
            existing.failed += int(failed or 0)
            existing.prompt_tokens += prompt
            existing.completion_tokens += completion
            existing.duration_ms += ms
        result = await db.execute(delete(UsageLog).where(UsageLog.created_at < cutoff))
        stats["usage_rows"] = result.rowcount or 0
    stats["usage_samples"] = await usage_sampler.roll_up(db, settings.usage_sample_days, now)
    if settings.audit_retention_days > 0:
        cutoff = now - timedelta(days=settings.audit_retention_days)
        result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        stats["audit_rows"] = result.rowcount or 0

    result = await db.execute(
        delete(DownloadTask).where(
            DownloadTask.status.in_(("done", "failed", "cancelled")),
            DownloadTask.finished_at < now - timedelta(days=90),
        )
    )
    stats["downloads"] = result.rowcount or 0
    result = await db.execute(
        delete(WaitlistEntry).where(
            WaitlistEntry.status.in_(("booked", "expired", "cancelled", "notified")),
            WaitlistEntry.end_at < now - timedelta(days=30),
        )
    )
    stats["waitlist"] = result.rowcount or 0

    model_ids = (await db.execute(select(ModelLoadEvent.model_id).distinct())).scalars().all()
    pruned = 0
    for mid in model_ids:
        keep = (
            select(ModelLoadEvent.id)
            .where(ModelLoadEvent.model_id == mid)
            .order_by(ModelLoadEvent.started_at.desc())
            .limit(LOAD_EVENTS_KEPT)
        )
        result = await db.execute(
            delete(ModelLoadEvent).where(ModelLoadEvent.model_id == mid, ModelLoadEvent.id.not_in(keep))
        )
        pruned += result.rowcount or 0
    stats["load_events"] = pruned
    # the in-app inbox: read messages after 90 days, everything after a year
    result = await db.execute(
        delete(Notification).where(
            ((Notification.read_at.is_not(None)) & (Notification.created_at < now - timedelta(days=90)))
            | (Notification.created_at < now - timedelta(days=365))
        )
    )
    stats["notifications"] = result.rowcount or 0
    await db.commit()
    return stats
