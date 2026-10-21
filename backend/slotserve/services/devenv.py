"""Dev environments: a Docker container with SSH for the length of a booking.

When a dev environment booking comes up, its lane's model server is stopped (it is scheduled like a whole-GPU
booking), the image is pulled ahead of time, and a container is started with the lane's GPUs, the person's
persistent ``/workspace`` volume and the shared models read-only. SSH comes from a static Dropbear that SlotServe
copies into the container, so any image works; logins use the person's public keys only. When the booking ends
the container is removed; ``/workspace`` stays. A running environment can be saved as an image for next time.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select

from ..db import SessionLocal, utcnow
from ..orm import DevSession, DevTemplate, Lane, Reservation, SshKey, User
from ..runtime.manager import RuntimeState
from .docker_api import DockerClient, DockerError, registry_of, tar_files

log = logging.getLogger(__name__)

TICK_SECONDS = 3.0
OPEN_STATES = ("waiting", "pulling", "ready", "starting", "running", "stopping", "failed")
LABEL = "slotserve.devenv"
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

START_SCRIPT = """#!/bin/sh
# Started by SlotServe: an SSH server next to the environment's own main process.
/opt/slotserve/dropbearmulti dropbear -F -e -s -K 60 -p 22 \\
  -r /opt/slotserve/ssh_host_key -D /opt/slotserve/ssh -P /tmp/.slotserve-dropbear.pid &
cd /workspace 2>/dev/null || true
if [ "$#" -gt 0 ]; then exec "$@"; fi
while :; do sleep 3600; done
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

    # ---- plumbing ---------------------------------------------------------------

    @property
    def docker(self) -> DockerClient:
        if self._docker is None or (self._docker_host and self._docker_host != self.settings.docker_host):
            self._docker = DockerClient(self.settings.docker_host)
            self._docker_host = self.settings.docker_host
        return self._docker

    def poke(self) -> None:
        self._wake.set()

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
                if not active_or_soon and not self.settings.devenv_enabled:
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
                for s in sessions:
                    try:
                        await self._advance(db, s)
                    except DevEnvError as exc:
                        await self._fail(db, s, exc.code, notify=True)
                    except DockerError as exc:
                        await self._fail(db, s, exc.message, notify=True)
                    except (httpx.HTTPError, OSError) as exc:
                        log.warning("dev environment %s: docker unreachable: %s", s.id, exc)
                    await db.commit()

    async def _sync_sessions(self, db) -> bool:
        """Creates a session for every dev environment booking that is running or due to be prepared."""
        now = utcnow()
        soon = now + timedelta(minutes=max(self.settings.devenv_pull_minutes, 1))
        rows = (
            (
                await db.execute(
                    select(Reservation).where(
                        Reservation.devenv_template_id.is_not(None),
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
            template = await db.get(DevTemplate, r.devenv_template_id)
            if template is None:
                continue
            db.add(
                DevSession(
                    reservation_id=r.id,
                    user_id=r.user_id,
                    template_id=template.id,
                    lane_id=r.lane_id,
                    image=template.image,
                    state="waiting",
                )
            )
        await db.commit()
        open_count = (
            await db.execute(select(func.count(DevSession.id)).where(DevSession.state.in_(OPEN_STATES)))
        ).scalar_one()
        return bool(rows) or open_count > 0

    async def _advance(self, db, s: DevSession) -> None:
        now = utcnow()
        r = await db.get(Reservation, s.reservation_id)
        over = r is None or r.status != "booked" or r.end_at <= now
        if over:
            await self._stop(db, s, "stopped")
            return
        if s.state == "failed":
            return  # waits for a restart from the person or an admin
        if s.state in ("waiting", "pulling"):
            if await self.docker.image_exists(s.image):
                s.state, s.detail, s.progress = "ready", None, None
            else:
                self._ensure_pull(s.image)
                s.state = "pulling"
                return
        if s.state == "ready" and r.start_at <= now and self._gpu_free(r.lane_id):
            await self._start(db, s, r)
            return
        if s.state == "running" and s.container_id and s.id not in self._saves:
            info = await self.docker.inspect(s.container_id)
            if info is None or not info.get("State", {}).get("Running"):
                code = (info or {}).get("State", {}).get("ExitCode")
                tail = await self.docker.logs(s.container_id, 40) if info else ""
                reason = f"container stopped (exit code {code})" if code is not None else "container is gone"
                await self._fail(db, s, f"{reason}\n{tail}".strip(), notify=True)

    def _gpu_free(self, lane_id: int | None) -> bool:
        """The lane's model server has stopped, so the container gets the GPU memory to itself."""
        lanes = getattr(self.state, "lanes", None)
        if lanes is None:
            return True
        return lanes.get(lane_id).runtime.state == RuntimeState.STOPPED

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

    async def _start(self, db, s: DevSession, r: Reservation) -> None:
        user = await db.get(User, s.user_id)
        template = await db.get(DevTemplate, s.template_id) if s.template_id else None
        keys = (await db.execute(select(SshKey.public_key).where(SshKey.user_id == s.user_id))).scalars().all()
        if not keys:
            raise DevEnvError("ssh_key_required")
        bundle = self.ssh_bundle()
        s.state, s.detail, s.error = "starting", None, None
        await db.commit()

        lane = await db.get(Lane, r.lane_id) if r.lane_id else None
        devices = lane.devices if lane else None
        used = set(
            (
                await db.execute(
                    select(DevSession.ssh_port).where(
                        DevSession.state.in_(("starting", "running")), DevSession.id != s.id
                    )
                )
            ).scalars()
        )
        lo, hi = parse_port_range(self.settings.devenv_ssh_ports)
        candidates = [p for p in range(lo, hi + 1) if p not in used]
        if not candidates:
            raise DevEnvError("devenv_no_ports")
        files = {
            "opt/slotserve/dropbearmulti": (bundle.binary, 0o755),
            "opt/slotserve/ssh_host_key": (bundle.host_key, 0o600),
            "opt/slotserve/ssh/authorized_keys": (("\n".join(keys) + "\n").encode(), 0o600),
            "opt/slotserve/start.sh": (START_SCRIPT.encode(), 0o755),
            "etc/profile.d/slotserve.sh": (PROFILE.encode(), 0o644),
        }
        archive = tar_files(files)
        config = await self._container_config(s, r, user, template, devices)
        name = f"slotserve-dev-{s.id}"
        last_error = ""
        for port in candidates[:5]:
            config["HostConfig"]["PortBindings"] = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]}
            await self.docker.remove(name)
            cid = await self.docker.create(name, config)
            try:
                await self.docker.put_archive(cid, "/", archive)
                await self.docker.start(cid)
            except DockerError as exc:
                await self.docker.remove(cid)
                last_error = exc.message
                if "port" in exc.message.lower() and ("allocated" in exc.message or "in use" in exc.message):
                    continue  # something else on the host holds this port
                raise
            s.container_id, s.container_name, s.ssh_port = cid, name, port
            s.state, s.started_at = "running", utcnow()
            await db.commit()
            self.state.notifier.emit("devenv_ready", user_id=s.user_id, reservation_id=r.id, port=port)
            return
        raise DevEnvError("devenv_no_ports", detail=last_error)

    async def _container_config(self, s: DevSession, r: Reservation, user, template, devices) -> dict:
        env = {"SLOTSERVE_USER": user.username if user else "", "SLOTSERVE_END": r.end_at.isoformat()}
        if template is not None and template.env:
            with contextlib.suppress(ValueError, TypeError):
                env.update({str(k): str(v) for k, v in json.loads(template.env).items()})
        command = ["/bin/sh", "-c", template.command] if template is not None and template.command else []
        device_request = {"Driver": "nvidia", "Capabilities": [["gpu"]]}
        if devices:
            device_request["DeviceIDs"] = [str(d) for d in devices]
        else:
            device_request["Count"] = -1
        mounts = [{"Type": "volume", "Source": f"slotserve-workspace-u{s.user_id}", "Target": "/workspace"}]
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
        return {
            "Image": s.image,
            "Entrypoint": ["/bin/sh", "/opt/slotserve/start.sh"],
            "Cmd": command,
            "User": "root",
            "WorkingDir": "/workspace",
            "Hostname": f"{(user.username if user else 'dev')[:40]}-dev",
            "Env": [f"{k}={v}" for k, v in env.items()],
            "ExposedPorts": {"22/tcp": {}},
            "Labels": {LABEL: "1", "slotserve.session": str(s.id), "slotserve.user": user.username if user else ""},
            "HostConfig": host_config,
        }

    async def _stop(self, db, s: DevSession, state: str) -> None:
        if s.container_id:
            s.state = "stopping"
            await db.commit()
            with contextlib.suppress(DockerError):
                await self.docker.stop(s.container_id, timeout=10)
            await self.docker.remove(s.container_id)
        s.state, s.stopped_at = state, utcnow()
        s.detail = s.progress = None

    async def _fail(self, db, s: DevSession, message: str, notify: bool) -> None:
        log.warning("dev environment %s failed: %s", s.id, message)
        if s.container_id:
            with contextlib.suppress(DockerError):
                await self.docker.remove(s.container_id)
            s.container_id = None
        s.state, s.error, s.detail, s.progress = "failed", message[:4000], None, None
        if notify:
            self.state.notifier.emit(
                "devenv_failed", user_id=s.user_id, reservation_id=s.reservation_id, error=message.splitlines()[0][:300]
            )

    async def _cleanup(self, db) -> None:
        """After a restart: keep containers whose session is still running, remove the rest."""
        running = {
            str(s.id): s for s in (await db.execute(select(DevSession).where(DevSession.state == "running"))).scalars()
        }
        for c in await self.docker.list(f"{LABEL}=1"):
            sid = (c.get("Labels") or {}).get("slotserve.session")
            if sid in running and c.get("State") == "running":
                continue
            await self.docker.remove(c["Id"])

    # ---- actions ----------------------------------------------------------------

    async def restart(self, session_id: int) -> None:
        async with self._lock, SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            if s is None or s.state not in OPEN_STATES:
                raise DevEnvError("not_found")
            if s.container_id:
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
            limit = self.settings.devenv_saved_images
            count = (
                await db.execute(
                    select(func.count(DevTemplate.id)).where(
                        DevTemplate.owner_id == s.user_id, DevTemplate.kind == "saved", DevTemplate.deleted_at.is_(None)
                    )
                )
            ).scalar_one()
            if count >= limit:
                raise DevEnvError("devenv_saved_limit", limit=limit)
            s.detail = "saving"
            await db.commit()
        self._saves[session_id] = asyncio.create_task(self._commit(session_id, name.strip()[:64], user.id))

    async def _commit(self, session_id: int, name: str, actor_id: int) -> None:
        async with SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            source = await db.get(DevTemplate, s.template_id) if s.template_id else None
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "env"
            ref = f"slotserve/u{s.user_id}-{slug}:{utcnow().strftime('%Y%m%d%H%M%S')}"
            try:
                await self.docker.commit(s.container_id, ref, comment=f"saved in SlotServe from {s.image}")
                db.add(
                    DevTemplate(
                        name=name,
                        image=ref,
                        description=f"Saved from {source.name if source else s.image}",
                        command=source.command if source else None,
                        env=source.env if source else None,
                        kind="saved",
                        owner_id=s.user_id,
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

    async def logs(self, s: DevSession, tail: int = 300) -> str:
        if not s.container_id:
            return s.error or ""
        return await self.docker.logs(s.container_id, tail)
