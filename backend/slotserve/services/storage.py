"""Storage admins manage in the panel: a directory on the Docker host, an NFS export or an SMB/CIFS share.

Each storage has one purpose:

* ``shared``: mounted into the environments of the people allowed to use it, at its mount path (read-only or not);
* ``personal``: every person gets ``users/u<id>`` on it as ``/workspace`` instead of a Docker volume per person
  (shared idle environments see the whole storage at ``/workspaces``, each person's folder only open to them);
* ``projects``: the kept directories of kept environments and templates (``projects/p<id>/<dir>``,
  ``templates/u<user>-t<template>/<dir>``); it takes over from the legacy ``devenv_storage_dir`` setting;
* ``models``: a model library, scanned for models and offered as a download target (SlotServe reads it at its
  local path) and mounted into environments at its mount path.

NFS and SMB storages are mounted through Docker named volumes of the ``local`` driver (``type=nfs`` / ``type=cifs``
driver options), so nothing has to be set up on the host except the usual mount helpers; a person's or project's
folder is a volume of its own whose device points at that sub-directory. Volume names carry a hash of the
storage's settings, so changing a storage gives new volumes (the old ones are removed once nothing uses them).

Short-lived helper containers do the work SlotServe cannot do itself (it may run in a container and reach host
paths only through Docker): checking a storage (``df``, a write test), measuring each person's folder (``du``)
and creating folders before an environment starts.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import posixpath
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from ..db import SessionLocal, utcnow
from ..orm import Storage, User
from ..security import decrypt_api_key, encrypt_api_key
from .docker_api import DockerClient, DockerError

log = logging.getLogger(__name__)

KINDS = ("host", "nfs", "smb")
PURPOSES = ("shared", "personal", "projects", "models")
ACCESS = ("everyone", "users", "admins")
LABEL = "slotserve.storage"
ROOT = "/slotserve-storage"  # where helper containers see a storage
SHARED_WORKSPACES = "/workspaces"  # personal storage in shared idle environments
TICK_SECONDS = 60.0
UID_BASE = 20000  # same as devenv.UID_BASE: a person's Unix uid in shared environments

PATH_RE = re.compile(r"^/[A-Za-z0-9._+@/-]{0,499}$")
SERVER_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})|\[[0-9A-Fa-f:.]{2,45}\])$")
SHARE_RE = re.compile(r"^[A-Za-z0-9._$-]+(?:/[A-Za-z0-9._@+-]+)*$")
OPTIONS_RE = re.compile(r"^[A-Za-z0-9_=.:/@+-]+(?:,[A-Za-z0-9_=.:/@+-]+)*$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\\-]{1,128}$")
# Host directories that cannot be a storage; mount paths that cannot be used in containers.
SYSTEM_DIRS = ("/proc", "/sys", "/dev", "/etc", "/boot", "/run", "/var/run", "/var/lib/docker")
CONTAINER_DIRS = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/run",
    "/etc",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/usr",
    "/boot",
    "/tmp",
    "/root",
    "/home",
    "/opt/slotserve",
    "/workspace",
    SHARED_WORKSPACES,
    "/models",
    ROOT,
)
# Mount options SlotServe sets itself.
OWN_OPTIONS = {"addr", "username", "user", "password", "pass", "credentials"}

CHECK_SCRIPT = r"""set -e
[ -d /s ] || { echo "the storage is not a directory" >&2; exit 3; }
echo "==DF=="
df -kP /s | tail -n 1
if [ "$W" = 1 ]; then
  f="/s/.slotserve-check-$$"
  if ( echo ok > "$f" ) 2>/dev/null; then rm -f "$f"; echo "==WRITE== ok"; else echo "==WRITE== no"; fi
fi
if [ "$DU" = 1 ] && [ -d /s/users ]; then
  echo "==DU=="
  cd /s/users
  for d in u*; do [ -d "$d" ] && du -sk "$d" 2>/dev/null || true; done
fi
"""


class StorageError(Exception):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


# ---- values -------------------------------------------------------------------


def load_json(value: str | None, default):
    try:
        data = json.loads(value) if value else default
    except ValueError:
        return default
    return data if isinstance(data, type(default)) else default


def user_ids(st: Storage) -> list[int]:
    return [int(i) for i in load_json(st.user_ids, []) if isinstance(i, int)]


def node_ids(st: Storage) -> list[int]:
    return [int(i) for i in load_json(st.node_ids, []) if isinstance(i, int)]


def reachable(st: Storage, node_id: int | None) -> bool:
    """Whether a container on a machine can use a storage: NFS/SMB volumes are mounted by that machine's Docker;
    a host directory exists on the control plane's machine (``node_id`` None) and the machines listed with it."""
    return node_id is None or st.kind != "host" or node_id in node_ids(st)


def allows(st: Storage, user: User | None) -> bool:
    """Whether a person may use a storage (disabled accounts never)."""
    if user is None or user.disabled_at is not None or not st.enabled:
        return False
    if st.access == "admins":
        return bool(user.is_admin)
    if st.access == "users":
        return user.id in user_ids(st)
    return True


def source(st: Storage) -> str:
    """How the storage is shown: the host path, ``server:/export`` or ``//server/share``."""
    if st.kind == "nfs":
        return f"{st.server}:{st.remote_path}"
    if st.kind == "smb":
        return f"//{st.server}/{st.remote_path}"
    return st.host_path or ""


def container_path(st: Storage) -> str | None:
    """Where an environment sees the storage."""
    if st.purpose == "personal":
        return "/workspace"
    if st.purpose in ("shared", "models"):
        return st.mount_path
    return None


def _clean_path(value: str, code: str) -> str:
    path = (value or "").strip()
    norm = posixpath.normpath(path) if path else ""
    if not PATH_RE.match(path) or norm != (path.rstrip("/") or "/") or "//" in path:
        raise StorageError(code, path=path[:200])
    return norm


def _under(path: str, dirs) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def check_host_path(value: str) -> str:
    path = _clean_path(value, "storage_path_invalid")
    if path == "/" or _under(path, SYSTEM_DIRS):
        raise StorageError("storage_path_invalid", path=path)
    return path


def check_mount_path(value: str) -> str:
    path = _clean_path(value, "storage_mount_invalid")
    if path == "/" or _under(path, CONTAINER_DIRS) or any(d.startswith(path + "/") for d in CONTAINER_DIRS):
        raise StorageError("storage_mount_invalid", path=path)
    return path


def check_server(value: str) -> str:
    server = (value or "").strip()
    if not SERVER_RE.match(server):
        raise StorageError("storage_server_invalid")
    return server


def check_remote_path(kind: str, value: str) -> str:
    raw = (value or "").strip()
    if kind == "nfs":
        return _clean_path(raw, "storage_remote_invalid")
    share = raw.strip("/")
    if not SHARE_RE.match(share) or ".." in share.split("/"):
        raise StorageError("storage_remote_invalid", path=raw[:200])
    return share


def check_options(value: str | None) -> str | None:
    options = (value or "").strip().strip(",")
    if not options:
        return None
    if len(options) > 500 or not OPTIONS_RE.match(options):
        raise StorageError("storage_options_invalid")
    for item in options.split(","):
        if item.split("=", 1)[0].lower() in OWN_OPTIONS:
            raise StorageError("storage_options_invalid")
    return options


def check_username(value: str | None) -> str | None:
    name = (value or "").strip()
    if not name:
        return None
    if not USERNAME_RE.match(name):
        raise StorageError("storage_username_invalid")
    return name


def check_password(value: str) -> str:
    # The cifs options are one comma-separated string, so a comma cannot be passed safely.
    if len(value) > 256 or any(c in value for c in ",\n\r\0"):
        raise StorageError("storage_password_invalid")
    return value


def check_local_path(value: str | None) -> str | None:
    path = (value or "").strip()
    if not path:
        return None
    if len(path) > 500 or not (path.startswith("/") or Path(path).is_absolute()) or ".." in re.split(r"[\\/]", path):
        raise StorageError("storage_local_invalid")
    return path


def password(st: Storage, secret_key: str) -> str:
    return (decrypt_api_key(st.password_enc, secret_key) or "") if st.password_enc else ""


def encrypt_password(value: str, secret_key: str) -> str:
    return encrypt_api_key(value, secret_key)


def driver_opts(st: Storage, secret_key: str, rel: str = "") -> dict[str, str] | None:
    """The ``local`` driver options of an NFS/SMB storage (or one of its folders); None for a host directory."""
    sub = f"/{rel}" if rel else ""
    if st.kind == "nfs":
        options = st.options or "nfsvers=4"
        remote = st.remote_path.rstrip("/") if st.remote_path != "/" else ""
        return {
            "type": "nfs",
            "o": f"addr={st.server},{options}",
            "device": f":{remote}{sub}" if remote or sub else ":/",
        }
    if st.kind == "smb":
        auth = f"username={st.username},password={password(st, secret_key)}" if st.username else "guest"
        options = st.options or "vers=3.0"
        return {
            "type": "cifs",
            "o": f"addr={st.server},{auth},{options}",
            "device": f"//{st.server}/{st.remote_path}{sub}",
        }
    return None


def config_hash(st: Storage, secret_key: str) -> str:
    """Changes whenever the way the storage is mounted changes."""
    key = json.dumps(driver_opts(st, secret_key) or {"host": st.host_path}, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def volume_name(st: Storage, secret_key: str, rel: str = "") -> str:
    base = f"slotserve-st{st.id}-{config_hash(st, secret_key)}"
    return f"{base}-{hashlib.sha256(rel.encode()).hexdigest()[:8]}" if rel else base


# ---- what a container gets ------------------------------------------------------


@dataclass
class Prep:
    rel: str  # folder on the storage
    seed: str | None = None  # copy this directory of the image into a new folder
    owner: int | None = None  # chown a new folder to this uid and make it private (shared environments)


@dataclass
class StoragePlan:
    """The managed storage of one container: mounts, volumes to create and folders to prepare."""

    secret_key: str
    personal: Storage | None = None
    projects: Storage | None = None
    mounts: list[dict] = field(default_factory=list)  # every managed mount (also the ones devenv adds)
    extras: list[dict] = field(default_factory=list)  # shared storages and model libraries
    volumes: dict[str, tuple[dict, dict]] = field(default_factory=dict)  # name -> (driver opts, labels)
    preps: dict[int, list[Prep]] = field(default_factory=dict)
    storages: dict[int, Storage] = field(default_factory=dict)

    def _mount(self, st: Storage, target: str, read_only: bool, rel: str = "") -> dict:
        self.storages[st.id] = st
        if st.kind == "host":
            src = f"{st.host_path.rstrip('/')}/{rel}" if rel else st.host_path
            mount = {"Type": "bind", "Source": src, "Target": target, "ReadOnly": read_only}
        else:
            name = volume_name(st, self.secret_key, rel)
            labels = {LABEL: str(st.id), f"{LABEL}.sub": rel}
            self.volumes[name] = (driver_opts(st, self.secret_key, rel), labels)
            mount = {"Type": "volume", "Source": name, "Target": target, "ReadOnly": read_only}
        self.mounts.append(mount)
        return mount

    def sub(self, st: Storage, rel: str, target: str, seed: str | None = None, owner: int | None = None) -> dict:
        """A folder of a storage (created when missing) mounted at ``target``."""
        self.preps.setdefault(st.id, []).append(Prep(rel, seed, owner))
        return self._mount(st, target, False, rel)

    def root(self, st: Storage, target: str, read_only: bool) -> dict:
        return self._mount(st, target, read_only)

    @property
    def signature(self) -> str:
        """Identifies the managed mounts; a kept environment's container is recreated when it changes."""
        if not self.mounts:
            return ""
        return hashlib.sha256(json.dumps(self.mounts, sort_keys=True).encode()).hexdigest()[:32]


async def enabled_storages(db) -> list[Storage]:
    return list((await db.execute(select(Storage).where(Storage.enabled.is_(True)).order_by(Storage.id))).scalars())


async def plan_for(
    db, secret_key: str, user: User | None, shared: bool = False, node_id: int | None = None
) -> StoragePlan:
    """The storages a container gets: a person's own environment those the person may use; a shared idle
    environment only those open to everyone, plus every person's folder of the personal storage. On another
    machine (``node_id``) only the storages it can reach (see ``reachable``)."""
    rows = [st for st in await enabled_storages(db) if reachable(st, node_id)]
    plan = StoragePlan(secret_key)
    if shared:
        usable = [st for st in rows if st.access == "everyone"]
        personal = next((st for st in rows if st.purpose == "personal"), None)
        if personal is not None:
            users = (await db.execute(select(User).where(User.disabled_at.is_(None)).order_by(User.id))).scalars()
            plan.root(personal, SHARED_WORKSPACES, False)
            for u in users:
                if allows(personal, u):
                    plan.preps.setdefault(personal.id, []).append(Prep(f"users/u{u.id}", owner=UID_BASE + u.id))
    else:
        usable = [st for st in rows if allows(st, user)]
        plan.personal = next((st for st in usable if st.purpose == "personal"), None)
        plan.projects = next((st for st in usable if st.purpose == "projects"), None)
    for st in usable:
        if st.purpose in ("shared", "models") and st.mount_path:
            plan.extras.append(plan.root(st, st.mount_path, bool(st.read_only)))
    return plan


def _helper_mount(st: Storage, secret_key: str, target: str, read_only: bool = False) -> tuple[dict, dict]:
    """A storage's root for a helper container, and the volume it needs (if any)."""
    if st.kind == "host":
        return {"Type": "bind", "Source": st.host_path, "Target": target, "ReadOnly": read_only}, {}
    name = volume_name(st, secret_key)
    volumes = {name: (driver_opts(st, secret_key), {LABEL: str(st.id), f"{LABEL}.sub": ""})}
    return {"Type": "volume", "Source": name, "Target": target, "ReadOnly": read_only}, volumes


async def ensure_volumes(docker: DockerClient, volumes: dict[str, tuple[dict, dict]]) -> None:
    for name, (opts, labels) in volumes.items():
        if await docker.volume_inspect(name) is None:
            await docker.volume_create(name, opts, labels)


async def run_helper(
    docker: DockerClient,
    name: str,
    image: str,
    mounts: list[dict],
    script: str,
    env: dict[str, str] | None = None,
    timeout: float = 600,
) -> tuple[int, str]:
    """Runs a script in a short-lived container; returns (exit code, output)."""
    config = {
        "Image": image,
        "Entrypoint": ["/bin/sh", "-c", script],
        "Cmd": [],
        "User": "root",
        "Env": [f"{k}={v}" for k, v in (env or {}).items()],
        "Labels": {LABEL: "helper"},
        "HostConfig": {"Mounts": mounts},
    }
    await docker.remove(name)
    cid = await docker.create(name, config)
    try:
        await docker.start(cid)
        code = await docker.wait(cid, timeout)
        return code, await docker.logs(cid, 5000)
    finally:
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
            await docker.remove(cid)


def _q(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


async def prepare(docker: DockerClient, image: str, plan: StoragePlan, name: str) -> None:
    """Creates the volumes and missing folders a container needs (folders seeded from the image like a new volume
    would be). Raises StorageError("devenv_storage_failed")."""
    try:
        for sid, preps in plan.preps.items():
            st = plan.storages[sid]
            mount, volumes = _helper_mount(st, plan.secret_key, ROOT)
            await ensure_volumes(docker, volumes)
            lines = ["set -e"]
            for p in preps:
                d = _q(f"{ROOT}/{p.rel}")
                steps = [f"mkdir -p {d}"]
                if p.seed:
                    steps.append(f"if [ -d {_q(p.seed)} ]; then cp -a {_q(p.seed + '/.')} {d}/ || true; fi")
                lines.append(f"if [ ! -d {d} ]; then {'; '.join(steps)}; fi")
                if p.owner is not None:
                    # the folder (not its content) belongs to the person's account in shared environments
                    lines.append(f"chown {p.owner}:{p.owner} {d} && chmod 700 {d} || true")
            code, output = await run_helper(docker, f"{name}-st{sid}", image, [mount], "\n".join(lines))
            if code != 0:
                raise StorageError(
                    "devenv_storage_failed", detail=f"{st.name}: exit code {code}: {output.strip()[-500:]}"
                )
        await ensure_volumes(docker, plan.volumes)
    except DockerError as exc:
        raise StorageError("devenv_storage_failed", detail=exc.message[:500]) from exc


async def remove_folder(docker: DockerClient, st: Storage, secret_key: str, image: str, rel: str, name: str) -> None:
    """Deletes a folder of a storage (a kept environment's data)."""
    mount, volumes = _helper_mount(st, secret_key, ROOT)
    await ensure_volumes(docker, volumes)
    code, output = await run_helper(docker, name, image, [mount], f"rm -rf {_q(f'{ROOT}/{rel}')}")
    if code != 0:
        raise StorageError("devenv_storage_failed", detail=f"exit code {code}: {output.strip()[-500:]}")
    with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
        for v in await docker.volumes(f"{LABEL}={st.id}"):
            sub = (v.get("Labels") or {}).get(f"{LABEL}.sub") or ""
            if sub == rel or sub.startswith(rel + "/"):
                with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                    await docker.remove_volume(v["Name"])


async def projects_storage(db, user: User | None) -> Storage | None:
    return next((st for st in await enabled_storages(db) if st.purpose == "projects" and allows(st, user)), None)


# ---- model libraries -------------------------------------------------------------


def library_path(st: Storage) -> Path | None:
    """Where SlotServe itself reads and writes a model library."""
    local = st.local_path or (st.host_path if st.kind == "host" else None)
    return Path(local) if local else None


def library_nodes(st: Storage) -> list[int]:
    """Other nodes a model library is available on (Admin -> Storage)."""
    return [int(i) for i in load_json(st.node_ids, []) if isinstance(i, int)]


def node_library_spec(st: Storage, secret_key: str) -> dict:
    """What a node agent needs to use a model library: its path (host directory) or how to mount it (NFS / SMB)."""
    spec: dict = {"storage_id": st.id, "name": st.name}
    opts = driver_opts(st, secret_key)
    if opts is None:
        spec["path"] = st.host_path
    else:
        spec["volume"] = {
            "name": volume_name(st, secret_key),
            "driver_opts": opts,
            "labels": {LABEL: str(st.id), f"{LABEL}.sub": ""},
        }
    return spec


async def model_library_dirs(db) -> list[Path]:
    rows = await enabled_storages(db)
    return [p for st in rows if st.purpose == "models" and (p := library_path(st)) is not None]


async def library_dir(db, storage_id: int, user: User) -> Path:
    """The directory of a model library a person may download into."""
    st = await db.get(Storage, storage_id)
    if st is None or st.purpose != "models" or not allows(st, user):
        raise StorageError("storage_library_invalid")
    path = library_path(st)
    if path is None:
        raise StorageError("storage_library_invalid")
    return path


# ---- checks ------------------------------------------------------------------------


def parse_check(output: str) -> tuple[dict, dict[int, int] | None]:
    """The helper's output: df line, write test, ``du -sk`` per person folder."""
    status: dict = {"ok": True, "error": None, "total": None, "used": None, "free": None, "writable": None}
    usage: dict[int, int] | None = None
    section = ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("==WRITE=="):
            status["writable"] = line.endswith("ok")
            continue
        if line in ("==DF==", "==DU=="):
            section = line
            if line == "==DU==":
                usage = {}
            continue
        parts = line.split()
        if section == "==DF==" and len(parts) >= 6 and parts[-5].isdigit():
            total, used, free = (int(x) * 1024 for x in parts[-5:-2])
            status.update(total=total, used=used, free=free)
            section = ""
        elif section == "==DU==" and len(parts) == 2 and parts[0].isdigit() and re.fullmatch(r"u\d+", parts[1]):
            usage[int(parts[1][1:])] = int(parts[0]) * 1024
    return status, usage


async def check_storage(docker: DockerClient, settings, st: Storage) -> tuple[dict, dict[int, int] | None]:
    """Mounts a storage in a helper container: reachable? total/used/free, writable, per-person usage."""
    image = settings.storage_helper_image
    try:
        if not await docker.image_exists(image):
            await docker.pull(image)
        mount, volumes = _helper_mount(st, settings.secret_key, "/s", read_only=bool(st.read_only))
        await ensure_volumes(docker, volumes)
        env = {"W": "0" if st.read_only else "1", "DU": "1" if st.purpose == "personal" else "0"}
        code, output = await run_helper(docker, f"slotserve-storage-check-{st.id}", image, [mount], CHECK_SCRIPT, env)
    except DockerError as exc:
        return _failed(exc.message), None
    except (httpx.HTTPError, OSError) as exc:
        return _failed(f"Docker is not reachable: {exc or type(exc).__name__}"), None
    except Exception as exc:  # whatever goes wrong is shown on the storage
        log.warning("checking storage %s failed", st.id, exc_info=True)
        return _failed(f"{type(exc).__name__}: {exc}"), None
    if code != 0:
        tail = "\n".join(line for line in output.strip().splitlines() if not line.startswith("=="))[-500:]
        return _failed(f"exit code {code}: {tail}".strip()), None
    status, usage = parse_check(output)
    if status["total"] is None:
        return _failed(f"could not read the free space: {output.strip()[-300:]}"), None
    return status, usage


def _failed(message: str) -> dict:
    return {"ok": False, "error": message[:1000], "total": None, "used": None, "free": None, "writable": None}


def gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


class StorageService:
    """Checks storages in the background (every ``storage_check_minutes``), keeps per-person usage, warns people
    over their soft limit and admins about storages that stopped working, and removes unused volumes."""

    def __init__(self, app_state):
        self.state = app_state
        self.settings = app_state.settings
        self._wake = asyncio.Event()
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def docker(self) -> DockerClient:
        return self.state.devenv.docker

    def poke(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("storage check failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)

    async def tick(self) -> None:
        minutes = self.settings.storage_check_minutes
        if not self.settings.devenv_enabled:
            return
        async with SessionLocal() as db:
            rows = list((await db.execute(select(Storage).order_by(Storage.id))).scalars())
        now = utcnow()
        for st in rows:
            if (
                minutes > 0
                and st.enabled
                and (st.checked_at is None or now - st.checked_at >= timedelta(minutes=minutes))
            ):
                await self.check(st.id)
        await self.remove_unused_volumes(rows)

    async def check(self, storage_id: int) -> Storage | None:
        lock = self._locks.setdefault(storage_id, asyncio.Lock())
        async with lock:
            async with SessionLocal() as db:
                st = await db.get(Storage, storage_id)
            if st is None:
                return None
            status, usage = await check_storage(self.docker, self.settings, st)
            async with SessionLocal() as db:
                st = await db.get(Storage, storage_id)
                if st is None:
                    return None
                was_ok = load_json(st.status, {}).get("ok")
                st.status, st.checked_at = json.dumps(status), utcnow()
                if usage is not None:
                    st.usage = json.dumps({str(k): v for k, v in sorted(usage.items())})
                    await self._quota(db, st, usage)
                await db.commit()
            if was_ok and not status["ok"]:
                self._emit("storage_down", storage=st.name, error=(status["error"] or "").splitlines()[0][:300])
            return st

    async def _quota(self, db, st: Storage, usage: dict[int, int]) -> None:
        """Tells people (and admins) once when they go over the storage's soft limit."""
        if st.purpose != "personal" or not st.quota_gb:
            st.warned = None
            return
        limit = st.quota_gb * 1e9
        over = {uid for uid, used in usage.items() if used > limit}
        warned = {int(i) for i in load_json(st.warned, []) if isinstance(i, int)}
        for uid in sorted(over - warned):
            user = await db.get(User, uid)
            if user is None:
                continue
            params = {"storage": st.name, "used": gb(usage[uid]), "quota": gb(limit)}
            self._emit("storage_quota", user_id=uid, **params)
            self._emit("storage_quota_admin", user=user.username, **params)
        st.warned = json.dumps(sorted(over)) if over else None

    def _emit(self, event: str, **data) -> None:
        notifier = getattr(self.state, "notifier", None)
        if notifier is not None:
            notifier.emit(event, **data)

    async def remove_unused_volumes(self, rows: list[Storage] | None = None) -> None:
        """Removes volumes of deleted storages and of old settings; Docker keeps the ones still in use."""
        if rows is None:
            async with SessionLocal() as db:
                rows = list((await db.execute(select(Storage))).scalars())
        current = {str(st.id): volume_name(st, self.settings.secret_key) for st in rows if st.kind != "host"}
        try:
            volumes = await self.docker.volumes(LABEL)
        except Exception:  # Docker unreachable or not configured: nothing to clean up
            log.debug("listing storage volumes failed", exc_info=True)
            return
        for v in volumes:
            sid = (v.get("Labels") or {}).get(LABEL, "")
            name = v.get("Name") or ""
            keep = current.get(sid)
            if keep and (name == keep or name.startswith(keep + "-")):
                continue
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                await self.docker.remove_volume(name)
