"""Dev environments: Docker containers with SSH, for a booking or for whoever wants an idle lane.

When a dev environment booking comes up, its lane's model server is stopped (it is scheduled like a whole-GPU
booking), the image is pulled ahead of time, and a container is started with the lane's GPUs, the person's
persistent ``/workspace`` and the shared models read-only. SSH comes from a static Dropbear that SlotServe
copies into the container, so any image works; logins use the person's public keys and, when admins allow
it, their environment password. When the booking ends the container is removed (``/workspace`` stays) - or,
for a kept environment ("project"), stopped and started again at the next booking.

A lane whose idle mode is ``devenv`` runs one shared container while nobody has it booked. Every SlotServe
user gets an account there with their own keys and password; it is stopped ahead of the next booking.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import posixpath
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select

from ..db import SessionLocal, utcnow
from ..orm import HOLDING_STATUSES, DevProject, DevSession, DevTemplate, Lane, Reservation, SshKey, User
from ..runtime.manager import RuntimeState
from .docker_api import DockerClient, DockerError, registry_of, tar_files

log = logging.getLogger(__name__)

TICK_SECONDS = 3.0
ACCOUNTS_REFRESH_SECONDS = 60.0
HOUSEKEEPING_SECONDS = 3600.0
OPEN_STATES = ("waiting", "pulling", "ready", "starting", "running", "stopping", "failed")
GPU_STATES = ("starting", "running", "stopping")
LABEL = "slotserve.devenv"
UID_BASE = 20000  # Unix uid of a SlotServe user in shared environments: UID_BASE + user id
MAX_AUTH_TRIES = 3
KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:@]{0,250}$", re.I)
PATH_RE = re.compile(r"^/[A-Za-z0-9._+@/-]{1,200}$")
MAX_PERSIST_PATHS = 8
# Paths that cannot be kept: system directories, SlotServe's own and the ones that are mounted anyway.
FORBIDDEN_PATHS = (
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
    "/opt/slotserve",
    "/workspace",
    "/models",
)
# Names a SlotServe user cannot get as a Unix account (system accounts and the usual image users).
RESERVED_NAMES = {
    "root",
    "daemon",
    "bin",
    "sys",
    "sync",
    "games",
    "man",
    "lp",
    "mail",
    "news",
    "uucp",
    "proxy",
    "www-data",
    "backup",
    "list",
    "irc",
    "gnats",
    "nobody",
    "systemd-network",
    "systemd-resolve",
    "messagebus",
    "sshd",
    "ubuntu",
    "jovyan",
    "admin",
    "operator",
    "halt",
    "shutdown",
    "ftp",
    "postgres",
    "mysql",
    "redis",
    "_apt",
}

LIB_SCRIPT = r"""# SlotServe helpers
set_hash() {
  # $1 = account, $2 = SHA-512-crypt hash, "*" (no password) or "!" (locked)
  if command -v chpasswd >/dev/null 2>&1 && printf '%s:%s\n' "$1" "$2" | chpasswd -e >/dev/null 2>&1; then
    return 0
  fi
  if command -v usermod >/dev/null 2>&1 && usermod -p "$2" "$1" >/dev/null 2>&1; then
    return 0
  fi
  for f in /etc/shadow /etc/passwd; do
    if [ -f "$f" ] && grep -q "^$1:" "$f"; then
      awk -F: -v OFS=: -v u="$1" -v h="$2" '$1 == u { $2 = h } { print }' "$f" > "$f.slotserve" \
        && cat "$f.slotserve" > "$f"
      rm -f "$f.slotserve"
      return 0
    fi
  done
  echo "slotserve: could not set the password of $1" >&2
  return 1
}
"""

START_SCRIPT = """#!/bin/sh
# Started by SlotServe: an SSH server next to the environment's own main process.
S=/opt/slotserve
. $S/lib.sh
if [ -f $S/root.hash ]; then
  set_hash root "$(cat $S/root.hash)" || true
  rm -f $S/root.hash
fi
$S/dropbearmulti dropbear -F -e {flags} -K 60 -T {tries} -p 22 \\
  -r $S/ssh_host_key -D $S/ssh -P /tmp/.slotserve-dropbear.pid &
cd /workspace 2>/dev/null || true
if [ "$#" -gt 0 ]; then exec "$@"; fi
while :; do sleep 3600; done
"""

SHARED_START_SCRIPT = """#!/bin/sh
# Started by SlotServe: the shared environment of an idle lane. Everyone logs in with their own account.
S=/opt/slotserve
if ! /bin/sh $S/accounts.sh; then
  echo "slotserve: could not create the user accounts, see above" >&2
  exit 1
fi
$S/dropbearmulti dropbear -F -e -w {flags} -K 60 -T {tries} -p 22 \\
  -r $S/ssh_host_key -P /tmp/.slotserve-dropbear.pid &
if [ "$#" -gt 0 ]; then exec "$@"; fi
while :; do sleep 3600; done
"""

ACCOUNTS_SCRIPT = r"""#!/bin/sh
# Written by SlotServe: one account per SlotServe user (name:uid:hash lines in /opt/slotserve/accounts).
S=/opt/slotserve
. $S/lib.sh
if command -v useradd >/dev/null 2>&1; then
  ADD=useradd
elif command -v adduser >/dev/null 2>&1; then
  ADD=adduser
else
  echo "slotserve: this image has neither useradd nor adduser, so it cannot host a shared environment" >&2
  exit 2
fi
SH=/bin/sh
[ -x /bin/bash ] && SH=/bin/bash
mkdir -p /home
status=0
while IFS=: read -r name uid hash; do
  [ -n "$name" ] || continue
  if id "$name" >/dev/null 2>&1; then
    if [ "$(id -u "$name")" != "$uid" ]; then
      echo "slotserve: the image already has an account $name; skipped" >&2
      continue
    fi
  elif [ "$ADD" = useradd ]; then
    useradd -M -d "/home/$name" -s "$SH" -u "$uid" "$name" || { status=1; continue; }
  else
    adduser -D -H -h "/home/$name" -s "$SH" -u "$uid" "$name" || { status=1; continue; }
  fi
  h="/home/$name"
  mkdir -p "$h/.ssh"
  cat "$S/keys/$name" > "$h/.ssh/authorized_keys" 2>/dev/null || : > "$h/.ssh/authorized_keys"
  chown "$uid:$(id -g "$name")" "$h" "$h/.ssh" "$h/.ssh/authorized_keys"
  chmod 700 "$h" "$h/.ssh"
  chmod 600 "$h/.ssh/authorized_keys"
  set_hash "$name" "$hash" || status=1
done < $S/accounts
# People who are no longer listed (disabled) lose access.
if [ -f $S/accounts.done ]; then
  for name in $(cat $S/accounts.done); do
    if ! grep -q "^$name:" $S/accounts; then
      rm -f "/home/$name/.ssh/authorized_keys"
      set_hash "$name" '!' || true
    fi
  done
fi
cut -d: -f1 $S/accounts > $S/accounts.done
rm -rf $S/accounts $S/keys
exit $status
"""

PROFILE = """# Added by SlotServe
[ -d /workspace ] && [ "$PWD" = "$HOME" ] && cd /workspace
"""

# The built-in templates (admins can change the image, disable them or add their own).
BUILTINS = (
    {
        "builtin_key": "pytorch",
        "name": "PyTorch",
        "image": "pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel",
        "description": "PyTorch 2.14, CUDA 12.6, cuDNN 9 and compilers. conda and pip work.",
    },
    {
        "builtin_key": "jupyter-pytorch",
        "name": "Jupyter + PyTorch",
        "image": "quay.io/jupyter/pytorch-notebook:cuda12-pytorch-2.11.0",
        "description": "Jupyter Docker Stacks with conda, JupyterLab and PyTorch 2.11 (CUDA 12).",
    },
    {
        "builtin_key": "tensorflow",
        "name": "TensorFlow",
        "image": "tensorflow/tensorflow:2.21.0-gpu-jupyter",
        "description": "TensorFlow 2.21 with GPU support and Jupyter.",
    },
    {
        "builtin_key": "cuda",
        "name": "CUDA",
        "image": "nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04",
        "description": "Ubuntu 24.04 with the CUDA 12.9 toolkit and cuDNN. Install what you need with apt and pip.",
    },
)


class DevEnvError(Exception):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def parse_port_range(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
    if not m:
        raise ValueError("expected a range like 2200-2299")
    lo, hi = int(m.group(1)), int(m.group(2))
    if not (1 <= lo <= hi <= 65535):
        raise ValueError("ports must be between 1 and 65535, low to high")
    return lo, hi


def parse_public_key(text: str) -> tuple[str, str]:
    """Validates an OpenSSH public key line; returns (normalized key, SHA256 fingerprint)."""
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise ValueError("not an OpenSSH public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError("not an OpenSSH public key") from exc
    if len(blob) < 4 or blob[4 : 4 + int.from_bytes(blob[:4], "big")].decode(errors="replace") != parts[0]:
        raise ValueError("not an OpenSSH public key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return " ".join(parts[:3]), fingerprint


def check_image(settings, image: str) -> str:
    image = image.strip()
    if not IMAGE_RE.match(image):
        raise DevEnvError("devenv_image_invalid")
    allowed = [r.strip().lower() for r in settings.devenv_allowed_registries.split(",") if r.strip()]
    if allowed and registry_of(image) not in allowed:
        raise DevEnvError("devenv_registry_not_allowed", registries=", ".join(allowed))
    return image


def check_persist_paths(paths: list[str]) -> list[str]:
    """Extra directories kept between bookings: absolute, normalized, not a system directory."""
    out: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        norm = posixpath.normpath(path)
        bad = (
            not PATH_RE.match(path)
            or norm != path.rstrip("/")
            or norm == "/"
            or any(norm == f or norm.startswith(f + "/") or f.startswith(norm + "/") for f in FORBIDDEN_PATHS)
        )
        if bad:
            raise DevEnvError("devenv_path_invalid", path=path[:200])
        if norm not in out:
            out.append(norm)
    if len(out) > MAX_PERSIST_PATHS:
        raise DevEnvError("devenv_path_limit", limit=MAX_PERSIST_PATHS)
    return out


def load_paths(value: str | None) -> list[str]:
    try:
        data = json.loads(value) if value else []
    except ValueError:
        return []
    return [str(p) for p in data] if isinstance(data, list) else []


def dump_paths(paths: list[str]) -> str | None:
    return json.dumps(paths) if paths else None


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", path.strip("/").replace("/", "-"))[:60] or "root"


def unix_names(users: list[User]) -> dict[int, str]:
    """A stable, valid Unix account name for every user (lower case; clashes get the user id appended)."""
    out: dict[int, str] = {}
    taken: set[str] = set()
    for u in sorted(users, key=lambda x: x.id):
        name = re.sub(r"[^a-z0-9_-]", "_", u.username.lower())[:28]
        if not re.match(r"^[a-z_]", name):
            name = "u" + name
        if name in RESERVED_NAMES or name in taken:
            name = f"{name[:24]}_{u.id}"
        taken.add(name)
        out[u.id] = name
    return out


async def unix_name(db, user: User) -> str:
    users = (await db.execute(select(User).where(User.disabled_at.is_(None)))).scalars().all()
    return unix_names([*users, user]).get(user.id, user.username.lower())


def can_access(session: DevSession, user: User | None) -> bool:
    """Who may see and use a dev environment: its owner, admins, and anyone for a lane's shared environment."""
    if user is None or user.disabled_at is not None:
        return False
    return user.is_admin or session.kind == "shared" or (session.user_id is not None and session.user_id == user.id)


async def seed_templates(db) -> None:
    existing = set(
        (await db.execute(select(DevTemplate.builtin_key).where(DevTemplate.builtin_key.is_not(None)))).scalars()
    )
    for spec in BUILTINS:
        if spec["builtin_key"] not in existing:
            db.add(DevTemplate(kind="builtin", **spec))
    await db.commit()


def visible(template: DevTemplate | None, user: User) -> bool:
    if template is None or template.deleted_at is not None:
        return False
    if template.kind == "builtin":
        return template.enabled
    return template.owner_id == user.id or user.is_admin


@dataclass
class SshBundle:
    binary: bytes
    host_key: bytes
    fingerprint: str


@dataclass
class Plan:
    """Everything needed to (re)create and start the container of a session."""

    name: str
    files: dict[str, tuple[bytes, int]]
    config: dict
    binds: list[tuple[str, str]]  # (directory under the storage dir, path in the container) to prepare
    accounts_digest: str | None = None


class DevEnvManager:
    def __init__(self, app_state, docker: DockerClient | None = None):
        self.state = app_state
        self.settings = app_state.settings
        self._docker = docker
        self._docker_host: str | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._pulls: dict[str, asyncio.Task] = {}
        self._saves: dict[int, asyncio.Task] = {}
        self._bundle: SshBundle | None = None
        self._models_source: str | bool | None = False  # False = not looked up yet
        self.status: dict = {"ok": False, "error": None, "version": None}
        self._status_at: datetime | None = None
        self._cleaned = False
        self._shared_wanted: dict[int, int | None] = {}  # lane id -> template of its shared environment
        self._holding: set[int] = set()  # lanes whose shared environment holds the GPUs
        self._accounts_dirty = False
        self._accounts_digest: dict[int, str] = {}
        self._accounts_checked: dict[int, float] = {}
        self._housekept_at: datetime | None = None

    # ---- plumbing ---------------------------------------------------------------

    @property
    def docker(self) -> DockerClient:
        if self._docker is None or (self._docker_host and self._docker_host != self.settings.docker_host):
            self._docker = DockerClient(self.settings.docker_host)
            self._docker_host = self.settings.docker_host
        return self._docker

    def poke(self) -> None:
        self._wake.set()

    def accounts_changed(self) -> None:
        """Someone's keys or password changed: refresh the accounts in shared environments soon."""
        self._accounts_dirty = True
        self.poke()

    def holds_gpu(self, lane_id: int | None) -> bool:
        """Whether a lane's shared environment is (still) using its GPUs; the scheduler waits for it."""
        return lane_id in self._holding

    async def close(self) -> None:
        for task in [*self._pulls.values(), *self._saves.values()]:
            task.cancel()
        if self._docker is not None:
            await self._docker.close()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dev environment tick failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)

    async def refresh_status(self, force: bool = False) -> dict:
        now = utcnow()
        if not force and self._status_at and now - self._status_at < timedelta(seconds=30):
            return self.status
        self._status_at = now
        try:
            version = await self.docker.version()
            self.status = {"ok": True, "error": None, "version": f"{version.get('Version', '?')}"}
        except (DockerError, OSError, ValueError, httpx.HTTPError) as exc:
            self.status = {"ok": False, "error": str(exc) or type(exc).__name__, "version": None}
        return self.status

    def ssh_bundle(self) -> SshBundle:
        """The Dropbear binary and the host key every environment shares (so clients see one stable key)."""
        if self._bundle is not None:
            return self._bundle
        binary_path = Path(self.settings.dropbear_path)
        if not binary_path.is_file():
            raise DevEnvError("devenv_ssh_missing", path=str(binary_path))
        key_dir = self.settings.data_dir / "devenv"
        key_dir.mkdir(parents=True, exist_ok=True)
        key = key_dir / "ssh_host_ed25519"
        if not key.is_file():
            subprocess.run(
                [str(binary_path), "dropbearkey", "-t", "ed25519", "-f", str(key)],
                check=True,
                capture_output=True,
                timeout=30,
            )
        try:
            out = subprocess.run(
                [str(binary_path), "dropbearkey", "-y", "-f", str(key)], check=True, capture_output=True, timeout=30
            ).stdout.decode(errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not read the SSH host key fingerprint: %s", exc)
            out = ""
        m = re.search(r"Fingerprint: (\S+)", out)
        self._bundle = SshBundle(binary_path.read_bytes(), key.read_bytes(), m.group(1) if m else "")
        return self._bundle

    async def _models_mount(self) -> str | None:
        """Host path of the models directory: SlotServe's own bind mount when it runs in a container."""
        if self._models_source is not False:
            return self._models_source or None
        source: str | None = None
        models = str(Path(self.settings.models_dir).resolve())
        if Path("/.dockerenv").exists():
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                me = await self.docker.inspect(socket.gethostname())
                for m in (me or {}).get("Mounts", []):
                    if m.get("Destination") == models and m.get("Type") == "bind":
                        source = m.get("Source")
        elif os.name != "nt":
            source = models
        self._models_source = source or ""
        return source

    # ---- the loop ---------------------------------------------------------------

    async def tick(self) -> None:
        async with self._lock:
            async with SessionLocal() as db:
                active_or_soon = await self._sync_sessions(db)
                shared = await self._sync_shared(db)
                if not active_or_soon and not shared and not self.settings.devenv_enabled:
                    self._holding = set()
                    return
                await self.refresh_status()
                if not self.status["ok"]:
                    return
                if not self._cleaned:
                    await self._cleanup(db)
                    self._cleaned = True
                sessions = (
                    (await db.execute(select(DevSession).where(DevSession.state.in_(OPEN_STATES)))).scalars().all()
                )
                # Shared environments first, so they leave the GPUs before a booking's container starts.
                for s in sorted(sessions, key=lambda x: (x.kind != "shared", x.id)):
                    try:
                        await self._advance(db, s)
                    except DevEnvError as exc:
                        await self._fail(db, s, exc.code, notify=True)
                    except DockerError as exc:
                        await self._fail(db, s, exc.message, notify=True)
                    except (httpx.HTTPError, OSError) as exc:
                        log.warning("dev environment %s: docker unreachable: %s", s.id, exc)
                    await db.commit()
                self._accounts_dirty = False
                self._holding = set(
                    (
                        await db.execute(
                            select(DevSession.lane_id).where(
                                DevSession.kind == "shared", DevSession.state.in_(GPU_STATES)
                            )
                        )
                    ).scalars()
                )
                await self._housekeeping(db)

    async def _sync_sessions(self, db) -> bool:
        """Creates a session for every dev environment booking that is running or due to be prepared."""
        now = utcnow()
        soon = now + timedelta(minutes=max(self.settings.devenv_pull_minutes, 1))
        rows = (
            (
                await db.execute(
                    select(Reservation).where(
                        or_(Reservation.devenv_template_id.is_not(None), Reservation.devenv_project_id.is_not(None)),
                        Reservation.status == "booked",
                        Reservation.end_at > now,
                        Reservation.start_at <= soon,
                    )
                )
            )
            .scalars()
            .all()
        )
        known = set(
            (
                await db.execute(
                    select(DevSession.reservation_id).where(DevSession.reservation_id.in_([r.id for r in rows]))
                )
            ).scalars()
        )
        for r in rows:
            if r.id in known:
                continue
            project = await db.get(DevProject, r.devenv_project_id) if r.devenv_project_id else None
            template = await db.get(DevTemplate, r.devenv_template_id) if r.devenv_template_id else None
            if project is None and template is None:
                continue
            db.add(
                DevSession(
                    kind="booking",
                    reservation_id=r.id,
                    user_id=r.user_id,
                    template_id=project.template_id if project else template.id,
                    project_id=project.id if project else None,
                    lane_id=r.lane_id,
                    image=project.image if project else template.image,
                    state="waiting",
                )
            )
        await db.commit()
        open_count = (
            await db.execute(
                select(func.count(DevSession.id)).where(
                    DevSession.state.in_(OPEN_STATES), DevSession.kind.is_not("shared")
                )
            )
        ).scalar_one()
        return bool(rows) or open_count > 0

    def _lane_idle(self, lane_id: int) -> bool:
        """The lane's scheduler has nothing to run and its idle mode asks for the shared environment."""
        lanes = getattr(self.state, "lanes", None)
        if lanes is None or lane_id not in lanes.contexts:
            return False
        target = lanes.contexts[lane_id].scheduler.target
        return target is not None and target.reason == "idle-devenv"

    async def _sync_shared(self, db) -> bool:
        """Decides which lanes want their shared environment now; creates the sessions for them."""
        wanted: dict[int, int | None] = {}
        for lane in (await db.execute(select(Lane))).scalars():
            template = await db.get(DevTemplate, lane.idle_template_id) if lane.idle_template_id else None
            ok = (
                self.settings.devenv_enabled
                and lane.mode == "devenv"
                and template is not None
                and template.deleted_at is None
                and self._lane_idle(lane.id)
            )
            wanted[lane.id] = template.id if ok else None
        self._shared_wanted = wanted
        open_shared = (
            (await db.execute(select(DevSession).where(DevSession.kind == "shared", DevSession.state.in_(OPEN_STATES))))
            .scalars()
            .all()
        )
        have = {(s.lane_id, s.template_id) for s in open_shared}
        for lane_id, template_id in wanted.items():
            if template_id is None or (lane_id, template_id) in have:
                continue
            template = await db.get(DevTemplate, template_id)
            db.add(
                DevSession(
                    kind="shared", lane_id=lane_id, template_id=template_id, image=template.image, state="waiting"
                )
            )
        await db.commit()
        return bool(open_shared) or any(wanted.values())

    async def _advance(self, db, s: DevSession) -> None:
        now = utcnow()
        r: Reservation | None = None
        if s.kind == "shared":
            over = self._shared_wanted.get(s.lane_id) != s.template_id
        else:
            r = await db.get(Reservation, s.reservation_id) if s.reservation_id else None
            over = r is None or r.status != "booked" or r.end_at <= now
        if over:
            await self._stop(db, s, "stopped")
            return
        if s.state == "failed":
            return  # waits for a restart from the person or an admin
        if s.state in ("waiting", "pulling"):
            if await self.docker.image_exists(s.image):
                s.state, s.detail, s.progress = "ready", None, None
            elif await self._project_image_lost(db, s):
                return
            else:
                self._ensure_pull(s.image)
                s.state = "pulling"
                return
        if s.state == "ready" and (r is None or r.start_at <= now) and await self._gpu_free(db, s):
            await self._start(db, s)
            return
        if s.state == "running" and s.container_id and s.id not in self._saves:
            info = await self.docker.inspect(s.container_id)
            if info is None or not info.get("State", {}).get("Running"):
                code = (info or {}).get("State", {}).get("ExitCode")
                tail = await self.docker.logs(s.container_id, 40) if info else ""
                reason = f"container stopped (exit code {code})" if code is not None else "container is gone"
                await self._fail(db, s, f"{reason}\n{tail}".strip(), notify=True)
            elif s.kind == "shared":
                await self._refresh_accounts(db, s)

    async def _project_image_lost(self, db, s: DevSession) -> bool:
        """A kept environment's snapshot image is gone (pruned): start over from its template's image."""
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        if project is None or not s.image.startswith("slotserve/"):
            return False
        template = await db.get(DevTemplate, project.template_id) if project.template_id else None
        if template is None:
            raise DevEnvError("devenv_project_image_missing")
        project.image = s.image = template.image
        project.container_id, project.state = None, "missing"
        return False

    async def _gpu_free(self, db, s: DevSession) -> bool:
        """The lane's model server has stopped and no other container of the lane still holds the GPUs."""
        lanes = getattr(self.state, "lanes", None)
        if lanes is not None and lanes.get(s.lane_id).runtime.state != RuntimeState.STOPPED:
            return False
        other = await db.execute(
            select(DevSession.id).where(
                DevSession.lane_id == s.lane_id, DevSession.id != s.id, DevSession.state.in_(GPU_STATES)
            )
        )
        return other.first() is None

    def _ensure_pull(self, image: str) -> None:
        task = self._pulls.get(image)
        if task is None or task.done():
            self._pulls[image] = asyncio.create_task(self._pull(image), name=f"pull {image}")

    async def _pull(self, image: str) -> None:
        last = 0.0

        async def progress(pct: float | None, status: str) -> None:
            nonlocal last
            loop = asyncio.get_running_loop()
            if loop.time() - last < 1.0:
                return
            last = loop.time()
            await self._update_pulling(image, pct, status)

        try:
            await self.docker.pull(image, progress)
            await self._update_pulling(image, 100.0, "done")
        except (DockerError, httpx.HTTPError, OSError) as exc:
            message = exc.message if isinstance(exc, DockerError) else (str(exc) or type(exc).__name__)
            async with SessionLocal() as db:
                rows = (
                    (
                        await db.execute(
                            select(DevSession).where(DevSession.image == image, DevSession.state == "pulling")
                        )
                    )
                    .scalars()
                    .all()
                )
                for s in rows:
                    await self._fail(db, s, f"pulling {image} failed: {message}", notify=True)
                await db.commit()
        finally:
            self.poke()

    async def _update_pulling(self, image: str, pct: float | None, status: str) -> None:
        async with SessionLocal() as db:
            rows = (
                (await db.execute(select(DevSession).where(DevSession.image == image, DevSession.state == "pulling")))
                .scalars()
                .all()
            )
            for s in rows:
                s.progress, s.detail = pct, status[:200]
            await db.commit()

    # ---- starting -----------------------------------------------------------------

    async def _account_files(self, db) -> tuple[dict[str, tuple[bytes, int]], str]:
        """The accounts of a shared environment: every active user, their keys and (maybe) password hash."""
        users = list((await db.execute(select(User).where(User.disabled_at.is_(None)))).scalars())
        names = unix_names(users)
        keys: dict[int, list[str]] = {}
        for user_id, key in (await db.execute(select(SshKey.user_id, SshKey.public_key).order_by(SshKey.id))).all():
            keys.setdefault(user_id, []).append(key)
        files: dict[str, tuple[bytes, int]] = {}
        lines = []
        for u in sorted(users, key=lambda x: x.id):
            secret = u.devenv_password_hash if self.settings.devenv_password_login and u.devenv_password_hash else "*"
            lines.append(f"{names[u.id]}:{UID_BASE + u.id}:{secret}")
            text = "".join(k + "\n" for k in keys.get(u.id, []))
            files[f"opt/slotserve/keys/{names[u.id]}"] = (text.encode(), 0o600)
        files["opt/slotserve/accounts"] = (("\n".join(lines) + "\n").encode(), 0o600)
        files["opt/slotserve/accounts.sh"] = (ACCOUNTS_SCRIPT.encode(), 0o700)
        files["opt/slotserve/lib.sh"] = (LIB_SCRIPT.encode(), 0o644)
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.encode() + b"\0" + files[path][0] + b"\0")
        return files, digest.hexdigest()

    async def _refresh_accounts(self, db, s: DevSession) -> None:
        loop = asyncio.get_running_loop()
        due = self._accounts_dirty or loop.time() - self._accounts_checked.get(s.id, 0.0) > ACCOUNTS_REFRESH_SECONDS
        if not due:
            return
        self._accounts_checked[s.id] = loop.time()
        files, digest = await self._account_files(db)
        if self._accounts_digest.get(s.id) == digest:
            return
        await self.docker.put_archive(s.container_id, "/", tar_files(files))
        code, output = await self.docker.exec(s.container_id, ["/bin/sh", "/opt/slotserve/accounts.sh"])
        if code != 0:
            log.warning("dev environment %s: refreshing accounts failed (%s): %s", s.id, code, output.strip()[-500:])
            s.error = f"refreshing accounts failed:\n{output.strip()[-2000:]}"
        else:
            s.error = None
        self._accounts_digest[s.id] = digest

    def _volume(
        self, plan_binds: list, mounts: list, volume: str, rel: str, target: str, read_only: bool = False
    ) -> None:
        """A persistent directory: a bind mount under the storage directory, or a Docker named volume."""
        storage = self.settings.devenv_storage_dir.rstrip("/")
        if storage:
            plan_binds.append((rel, target))
            mounts.append({"Type": "bind", "Source": f"{storage}/{rel}", "Target": target, "ReadOnly": read_only})
        else:
            mounts.append({"Type": "volume", "Source": volume, "Target": target})

    def _project_paths(self, s: DevSession, project: DevProject | None, template: DevTemplate | None) -> list[str]:
        if project is not None:
            return load_paths(project.persist_paths)
        return load_paths(template.persist_paths) if template is not None else []

    async def _plan(self, db, s: DevSession) -> Plan:
        user = await db.get(User, s.user_id) if s.user_id else None
        template = await db.get(DevTemplate, s.template_id) if s.template_id else None
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        lane = await db.get(Lane, s.lane_id) if s.lane_id else None
        r = await db.get(Reservation, s.reservation_id) if s.reservation_id else None
        bundle = self.ssh_bundle()
        password_login = self.settings.devenv_password_login
        flags = "" if password_login else "-s"
        files: dict[str, tuple[bytes, int]] = {
            "opt/slotserve/dropbearmulti": (bundle.binary, 0o755),
            "opt/slotserve/ssh_host_key": (bundle.host_key, 0o600),
            "opt/slotserve/lib.sh": (LIB_SCRIPT.encode(), 0o644),
        }
        binds: list[tuple[str, str]] = []
        mounts: list[dict] = []
        digest = None
        if s.kind == "shared":
            account_files, digest = await self._account_files(db)
            files.update(account_files)
            script = SHARED_START_SCRIPT.format(flags=flags, tries=MAX_AUTH_TRIES)
            self._volume(binds, mounts, f"slotserve-idle-home-l{s.lane_id}", f"lanes/l{s.lane_id}/home", "/home")
            name = f"slotserve-idle-{s.lane_id}"
            hostname = f"{re.sub(r'[^a-z0-9-]', '-', (lane.name if lane else 'lane').lower())[:40]}-idle"
            env = {"SLOTSERVE_SHARED": "1"}
        else:
            keys = (await db.execute(select(SshKey.public_key).where(SshKey.user_id == s.user_id))).scalars().all()
            secret = user.devenv_password_hash if user is not None and password_login else None
            if not keys and not secret:
                raise DevEnvError("ssh_login_required" if password_login else "ssh_key_required")
            files.update(
                {
                    "opt/slotserve/ssh/authorized_keys": ("".join(k + "\n" for k in keys).encode(), 0o600),
                    "opt/slotserve/root.hash": ((secret or "*").encode(), 0o600),
                    "etc/profile.d/slotserve.sh": (PROFILE.encode(), 0o644),
                }
            )
            script = START_SCRIPT.format(flags="" if secret else "-s", tries=MAX_AUTH_TRIES)
            uid = s.user_id
            self._volume(binds, mounts, f"slotserve-workspace-u{uid}", f"users/u{uid}/workspace", "/workspace")
            for path in self._project_paths(s, project, template):
                slug = _slug(path)
                if project is not None:
                    volume, rel = f"slotserve-project{project.id}-{slug}", f"projects/p{project.id}/{slug}"
                    self._volume(binds, mounts, volume, rel, path)
                else:
                    tid = template.id if template else 0
                    self._volume(binds, mounts, f"slotserve-u{uid}-t{tid}-{slug}", f"users/u{uid}/t{tid}/{slug}", path)
            name = f"slotserve-proj-{project.id}" if project is not None else f"slotserve-dev-{s.id}"
            hostname = f"{(user.username if user else 'dev')[:40]}-dev"
            env = {"SLOTSERVE_USER": user.username if user else "", "SLOTSERVE_END": r.end_at.isoformat() if r else ""}
        files["opt/slotserve/start.sh"] = (script.encode(), 0o755)

        if template is not None and template.env:
            with contextlib.suppress(ValueError, TypeError):
                env.update({str(k): str(v) for k, v in json.loads(template.env).items()})
        command = ["/bin/sh", "-c", template.command] if template is not None and template.command else []
        devices = lane.devices if lane else None
        device_request: dict = {"Driver": "nvidia", "Capabilities": [["gpu"]]}
        if devices:
            device_request["DeviceIDs"] = [str(d) for d in devices]
        else:
            device_request["Count"] = -1
        if models := await self._models_mount():
            mounts.append({"Type": "bind", "Source": models, "Target": "/models", "ReadOnly": True})
        host_config: dict = {
            "DeviceRequests": [device_request],
            "Mounts": mounts,
            "ShmSize": int(self.settings.devenv_shm_gb * 1024**3),
            "Init": True,
            "RestartPolicy": {"Name": "no"},
        }
        if self.settings.devenv_cpus > 0:
            host_config["NanoCpus"] = int(self.settings.devenv_cpus * 1e9)
        if self.settings.devenv_memory_gb > 0:
            host_config["Memory"] = int(self.settings.devenv_memory_gb * 1024**3)
        labels = {LABEL: "1", "slotserve.session": str(s.id), "slotserve.kind": s.kind or "booking"}
        if user is not None:
            labels["slotserve.user"] = user.username
        if project is not None:
            labels["slotserve.project"] = str(project.id)
        config = {
            "Image": s.image,
            "Entrypoint": ["/bin/sh", "/opt/slotserve/start.sh"],
            "Cmd": command,
            "User": "root",
            "WorkingDir": "/" if s.kind == "shared" else "/workspace",
            "Hostname": hostname,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "ExposedPorts": {"22/tcp": {}},
            "Labels": labels,
            "HostConfig": host_config,
        }
        return Plan(name, files, config, binds, digest)

    async def _used_ports(self, db, s: DevSession) -> tuple[set[int], set[int]]:
        """Ports of running containers, and ports that paused kept environments would like back."""
        used = set(
            (
                await db.execute(
                    select(DevSession.ssh_port).where(DevSession.state.in_(GPU_STATES), DevSession.id != s.id)
                )
            ).scalars()
        )
        kept = set(
            (
                await db.execute(
                    select(DevProject.container_port).where(
                        DevProject.container_port.is_not(None),
                        DevProject.state == "paused",
                        DevProject.id != (s.project_id or 0),
                    )
                )
            ).scalars()
        )
        return {p for p in used if p}, kept

    async def _prepare_storage(self, s: DevSession, plan: Plan) -> None:
        """Creates missing directories under the storage dir (seeded from the image, like a new volume would be).

        SlotServe may itself run in a container and only reach host paths through the Docker daemon, so a
        short-lived helper container from the environment's image does the work.
        """
        storage = self.settings.devenv_storage_dir.rstrip("/")
        if not storage or not plan.binds:
            return
        lines = ["set -e"]
        for rel, target in plan.binds:
            d = f"/slotserve-storage/{rel}"
            lines.append(
                f'if [ ! -d "{d}" ]; then mkdir -p "{d}"; '
                f'if [ -d "{target}" ]; then cp -a "{target}/." "{d}/" || true; fi; fi'
            )
        await self._helper(f"slotserve-prep-{s.id}", s.image, storage, "\n".join(lines))

    async def _helper(self, name: str, image: str, storage: str, script: str) -> None:
        config = {
            "Image": image,
            "Entrypoint": ["/bin/sh", "-c", script],
            "Cmd": [],
            "User": "root",
            "Labels": {LABEL: "helper"},
            "HostConfig": {"Binds": [f"{storage}:/slotserve-storage"]},
        }
        await self.docker.remove(name)
        cid = await self.docker.create(name, config)
        try:
            await self.docker.start(cid)
            code = await self.docker.wait(cid)
            if code != 0:
                tail = await self.docker.logs(cid, 20)
                raise DevEnvError("devenv_storage_failed", detail=f"exit code {code}: {tail.strip()[-500:]}")
        finally:
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                await self.docker.remove(cid)

    async def _start(self, db, s: DevSession) -> None:
        used, kept = await self._used_ports(db, s)
        plan = await self._plan(db, s)
        s.state, s.detail, s.error = "starting", None, None
        await db.commit()
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        if project is not None and await self._resume(db, s, project, plan, used):
            return
        await self._prepare_storage(s, plan)
        lo, hi = parse_port_range(self.settings.devenv_ssh_ports)
        free = [p for p in range(lo, hi + 1) if p not in used]
        candidates = [p for p in free if p not in kept] + [p for p in free if p in kept]
        if not candidates:
            raise DevEnvError("devenv_no_ports")
        archive = tar_files(plan.files)
        last_error = ""
        for port in candidates[:5]:
            plan.config["HostConfig"]["PortBindings"] = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]}
            await self.docker.remove(plan.name)
            cid = await self.docker.create(plan.name, plan.config)
            try:
                await self.docker.put_archive(cid, "/", archive)
                await self.docker.start(cid)
            except DockerError as exc:
                await self.docker.remove(cid)
                last_error = exc.message
                if _port_taken(exc):
                    continue  # something else on the host holds this port
                raise
            if project is not None:
                lane = await db.get(Lane, s.lane_id) if s.lane_id else None
                project.container_id, project.container_port = cid, port
                project.container_devices = lane.gpu_indices if lane else ""
                project.container_paths = project.persist_paths or ""
                project.state, project.last_used_at = "running", utcnow()
            self._running(s, cid, plan.name, port, plan.accounts_digest)
            await db.commit()
            if s.kind != "shared":
                self.state.notifier.emit("devenv_ready", user_id=s.user_id, reservation_id=s.reservation_id, port=port)
            return
        raise DevEnvError("devenv_no_ports", detail=last_error)

    def _running(self, s: DevSession, cid: str, name: str, port: int, digest: str | None) -> None:
        s.container_id, s.container_name, s.ssh_port = cid, name, port
        s.state, s.started_at = "running", utcnow()
        if digest is not None:
            self._accounts_digest[s.id] = digest
            self._accounts_checked[s.id] = asyncio.get_running_loop().time()

    async def _resume(self, db, s: DevSession, project: DevProject, plan: Plan, used: set[int]) -> bool:
        """Starts a kept environment's container again; when it cannot be reused, snapshots it for recreating.

        Returns True when the old container runs again; False when a new one has to be created.
        """
        info = await self.docker.inspect(project.container_id) if project.container_id else None
        if info is None:
            if project.container_id:
                project.state, project.container_id = "missing", None
            return False
        lane = await db.get(Lane, s.lane_id) if s.lane_id else None
        devices = lane.gpu_indices if lane else ""
        same = (
            (project.container_devices or "") == devices
            and (project.container_paths or "") == (project.persist_paths or "")
            and project.container_port is not None
            and project.container_port not in used
        )
        if same:
            if info.get("State", {}).get("Running"):
                with contextlib.suppress(DockerError):
                    await self.docker.stop(project.container_id, timeout=10)
            try:
                await self.docker.put_archive(project.container_id, "/", tar_files(plan.files))
                await self.docker.start(project.container_id)
            except DockerError as exc:
                if not _port_taken(exc):
                    raise
            else:
                project.state, project.last_used_at = "running", utcnow()
                self._running(s, project.container_id, plan.name, project.container_port, None)
                await db.commit()
                self.state.notifier.emit(
                    "devenv_ready", user_id=s.user_id, reservation_id=s.reservation_id, port=project.container_port
                )
                return True
        # Different GPUs, paths or port: keep what was installed by snapshotting the container into an image.
        s.detail = "snapshot"
        await db.commit()
        ref = f"slotserve/project-{project.id}:{utcnow().strftime('%Y%m%d%H%M%S')}"
        await self.docker.commit(project.container_id, ref, comment=f"SlotServe project {project.name}")
        await self.docker.remove(project.container_id)
        old = project.image
        project.image, project.container_id, project.container_port = ref, None, None
        s.image, s.detail = ref, None
        plan.config["Image"] = ref
        if old.startswith(f"slotserve/project-{project.id}:"):
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                await self.docker.remove_image(old)
        await db.commit()
        return False

    # ---- stopping -----------------------------------------------------------------

    async def _pause(self, db, project: DevProject, container_id: str) -> None:
        with contextlib.suppress(DockerError):
            await self.docker.stop(container_id, timeout=10)
        project.state, project.last_used_at = "paused", utcnow()
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError, ValueError, TypeError):
            info = await self.docker.inspect(container_id, size=True)
            if info and info.get("SizeRw") is not None:
                project.size_bytes = int(info["SizeRw"])

    async def _stop(self, db, s: DevSession, state: str) -> None:
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        if s.container_id:
            s.state = "stopping"
            await db.commit()
            if project is not None and project.container_id == s.container_id:
                await self._pause(db, project, s.container_id)
            else:
                with contextlib.suppress(DockerError):
                    await self.docker.stop(s.container_id, timeout=10)
                await self.docker.remove(s.container_id)
        elif project is not None and project.state == "running":
            project.state = "paused"
        s.state, s.stopped_at = state, utcnow()
        s.detail = s.progress = None
        self._accounts_digest.pop(s.id, None)
        self._accounts_checked.pop(s.id, None)

    async def _fail(self, db, s: DevSession, message: str, notify: bool) -> None:
        log.warning("dev environment %s failed: %s", s.id, message)
        if s.container_id:
            project = await db.get(DevProject, s.project_id) if s.project_id else None
            if project is not None and project.container_id == s.container_id:
                await self._pause(db, project, s.container_id)  # keep what is installed in it
            else:
                with contextlib.suppress(DockerError):
                    await self.docker.remove(s.container_id)
            s.container_id = None
        s.state, s.error, s.detail, s.progress = "failed", message[:4000], None, None
        if notify and s.kind != "shared":
            self.state.notifier.emit(
                "devenv_failed", user_id=s.user_id, reservation_id=s.reservation_id, error=message.splitlines()[0][:300]
            )

    async def _cleanup(self, db) -> None:
        """After a restart: keep containers whose session is still running, pause kept ones, remove the rest."""
        running = set(
            (await db.execute(select(DevSession.container_id).where(DevSession.state == "running"))).scalars()
        )
        for c in await self.docker.list(f"{LABEL}=1"):
            labels = c.get("Labels") or {}
            if c["Id"] in running and c.get("State") == "running":
                continue
            project_id = labels.get("slotserve.project")
            project = await db.get(DevProject, int(project_id)) if project_id and project_id.isdigit() else None
            if project is not None and project.container_id == c["Id"]:
                if c.get("State") == "running":
                    await self._pause(db, project, c["Id"])
                continue
            await self.docker.remove(c["Id"])
        await db.commit()

    async def _housekeeping(self, db) -> None:
        """Hourly: notice kept containers that disappeared; remove kept environments unused for too long."""
        now = utcnow()
        if self._housekept_at and now - self._housekept_at < timedelta(seconds=HOUSEKEEPING_SECONDS):
            return
        self._housekept_at = now
        days = self.settings.devenv_project_days
        projects = (await db.execute(select(DevProject).where(DevProject.state != "running"))).scalars().all()
        for p in projects:
            if p.container_id and await self.docker.inspect(p.container_id) is None:
                p.state, p.container_id = "missing", None
            last = p.last_used_at or p.created_at
            if days and last < now - timedelta(days=days):
                booked = await db.execute(
                    select(Reservation.id).where(
                        Reservation.devenv_project_id == p.id,
                        Reservation.status.in_(HOLDING_STATUSES),
                        Reservation.end_at > now,
                    )
                )
                if booked.first() is None:
                    log.info("removing dev environment project %s (%s): unused for %s days", p.id, p.name, days)
                    with contextlib.suppress(DevEnvError):
                        await self.delete_project(db, p, data=False)
        await db.commit()

    # ---- actions ----------------------------------------------------------------

    async def restart(self, session_id: int) -> None:
        async with self._lock, SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            if s is None or s.state not in OPEN_STATES:
                raise DevEnvError("not_found")
            if s.container_id:
                project = await db.get(DevProject, s.project_id) if s.project_id else None
                if project is not None and project.container_id == s.container_id:
                    await self._pause(db, project, s.container_id)
                else:
                    with contextlib.suppress(DockerError):
                        await self.docker.remove(s.container_id)
            s.container_id, s.state, s.error, s.detail, s.progress = None, "waiting", None, None, None
            await db.commit()
        self.poke()

    async def save(self, session_id: int, name: str, user: User) -> None:
        async with SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            if s is None or s.state != "running" or not s.container_id:
                raise DevEnvError("devenv_not_running")
            if s.id in self._saves and not self._saves[s.id].done():
                raise DevEnvError("devenv_saving")
            owner_id = s.user_id or user.id
            limit = self.settings.devenv_saved_images
            count = (
                await db.execute(
                    select(func.count(DevTemplate.id)).where(
                        DevTemplate.owner_id == owner_id, DevTemplate.kind == "saved", DevTemplate.deleted_at.is_(None)
                    )
                )
            ).scalar_one()
            if count >= limit:
                raise DevEnvError("devenv_saved_limit", limit=limit)
            s.detail = "saving"
            await db.commit()
        self._saves[session_id] = asyncio.create_task(self._commit(session_id, name.strip()[:64], owner_id))

    async def _commit(self, session_id: int, name: str, owner_id: int) -> None:
        async with SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            source = await db.get(DevTemplate, s.template_id) if s.template_id else None
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "env"
            ref = f"slotserve/u{owner_id}-{slug}:{utcnow().strftime('%Y%m%d%H%M%S')}"
            try:
                await self.docker.commit(s.container_id, ref, comment=f"saved in SlotServe from {s.image}")
                db.add(
                    DevTemplate(
                        name=name,
                        image=ref,
                        description=f"Saved from {source.name if source else s.image}",
                        command=source.command if source else None,
                        env=source.env if source else None,
                        persist_paths=source.persist_paths if source else None,
                        kind="saved",
                        owner_id=owner_id,
                    )
                )
                s.detail = None
            except (DockerError, httpx.HTTPError, OSError) as exc:
                s.detail = None
                s.error = f"saving failed: {exc.message if isinstance(exc, DockerError) else exc}"
                log.warning("saving dev environment %s failed: %s", session_id, exc)
            await db.commit()
        self._saves.pop(session_id, None)

    async def delete_template(self, db, template: DevTemplate) -> None:
        if template.kind == "saved":
            in_use = (
                await db.execute(
                    select(func.count(DevSession.id)).where(
                        DevSession.image == template.image, DevSession.state.in_(("starting", "running"))
                    )
                )
            ).scalar_one()
            if in_use:
                raise DevEnvError("devenv_template_in_use")
            with contextlib.suppress(DockerError, OSError):
                await self.docker.remove_image(template.image)
        template.deleted_at = utcnow()

    async def delete_project(self, db, project: DevProject, data: bool) -> None:
        """Removes a kept environment's container and snapshot; with ``data`` also its kept directories."""
        busy = await db.execute(
            select(DevSession.id).where(
                DevSession.project_id == project.id, DevSession.state.in_(("starting", "running", "stopping"))
            )
        )
        if busy.first() is not None:
            raise DevEnvError("devenv_project_in_use")
        if project.container_id:
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                await self.docker.remove(project.container_id)
        if data:
            storage = self.settings.devenv_storage_dir.rstrip("/")
            if storage:
                image = project.image
                if not await self.docker.image_exists(image):
                    template = await db.get(DevTemplate, project.template_id) if project.template_id else None
                    image = template.image if template else image
                script = f"rm -rf /slotserve-storage/projects/p{project.id}"
                await self._helper(f"slotserve-clean-p{project.id}", image, storage, script)
            else:
                for path in {*load_paths(project.persist_paths), *load_paths(project.container_paths)}:
                    with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                        await self.docker.remove_volume(f"slotserve-project{project.id}-{_slug(path)}")
        if project.image.startswith(f"slotserve/project-{project.id}:"):
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                await self.docker.remove_image(project.image)
        await db.delete(project)

    async def logs(self, s: DevSession, tail: int = 300) -> str:
        if not s.container_id:
            return s.error or ""
        return await self.docker.logs(s.container_id, tail)


def _port_taken(exc: DockerError) -> bool:
    text = exc.message.lower()
    return "port" in text and ("allocated" in text or "in use" in text)
