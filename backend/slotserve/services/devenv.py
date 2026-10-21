"""Dev environments: a Docker container with SSH for the length of a booking.

When a dev environment booking comes up, its lane's model server is stopped (it is scheduled like a whole-GPU
booking), the image is pulled ahead of time, and a container is started with the lane's GPUs, the person's
persistent ``/workspace`` volume and the shared models read-only. SSH comes from a static Dropbear that SlotServe
copies into the container, so any image works; logins use the person's public keys only. When the booking ends
the container is removed; ``/workspace`` stays. A running environment can be saved as an image for next time.

Every environment gets its own Docker network, and SlotServe reverse-proxies any of the container's ports under
``/devenv/<session>/port/<port>/`` to the people allowed to use the environment (see ``gateway/devenv_web.py``).
A template declares the ports of its web interfaces (a desktop in the browser, JupyterLab, VS Code or any HTTP
app) with a name and kind. SlotServe reaches a port over the environment's network when it runs in a container
itself (it joins that network), otherwise through a random port bound to 127.0.0.1 (declared ports) or the
container's address on its network. Web ports are never published publicly.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.cookiejar import CookieJar, CookiePolicy
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

WEB_KINDS = ("desktop", "jupyter", "vscode", "http")
SSH_SCRIPT = """#!/bin/sh
# Started by SlotServe: the SSH server (public keys only).
exec /opt/slotserve/dropbearmulti dropbear -F -e -s -K 60 -p 22 \\
  -r /opt/slotserve/ssh_host_key -D /opt/slotserve/ssh -P /tmp/.slotserve-dropbear.pid
"""
START_SCRIPT = """#!/bin/sh
# Started by SlotServe: an SSH server (dropbear) next to the environment's own main process.
/bin/sh /opt/slotserve/ssh.sh &
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
        "name": "JupyterLab + PyTorch",
        "image": "quay.io/jupyter/pytorch-notebook:cuda12-pytorch-2.11.0",
        "description": "JupyterLab in the browser: Jupyter Docker Stacks with conda and PyTorch 2.11 (CUDA 12).",
        "command": (
            "jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --ServerApp.root_dir=/workspace"
            " --ServerApp.base_url={base_path} --IdentityProvider.token={token} --ServerApp.allow_remote_access=True"
        ),
        "web_ports": json.dumps([{"name": "JupyterLab", "port": 8888, "kind": "jupyter"}]),
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
    {
        "builtin_key": "desktop",
        "name": "Ubuntu desktop",
        "image": "linuxserver/webtop:ubuntu-xfce-388f14a2-ls7",
        "description": "An Ubuntu XFCE desktop in the browser (linuxserver webtop) with the lane's GPUs.",
        "env": json.dumps(
            {"SUBFOLDER": "{base_path}", "CUSTOM_USER": "slotserve", "PASSWORD": "{token}", "TITLE": "SlotServe"}
        ),
        "web_ports": json.dumps([{"name": "Desktop", "port": 3000, "kind": "desktop"}]),
        "keep_entrypoint": True,
    },
    {
        "builtin_key": "vscode",
        "name": "VS Code",
        "image": "codercom/code-server:4.141.0",
        "description": "VS Code in the browser (code-server), opened on /workspace.",
        "command": "code-server --bind-addr 0.0.0.0:8080 --auth none --disable-telemetry --disable-update-check"
        " /workspace",
        "web_ports": json.dumps([{"name": "VS Code", "port": 8080, "kind": "vscode"}]),
    },
)
WEB_FIELDS = ("command", "env", "web_ports", "keep_entrypoint")


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
    existing = {
        t.builtin_key: t
        for t in (await db.execute(select(DevTemplate).where(DevTemplate.builtin_key.is_not(None)))).scalars()
    }
    for spec in BUILTINS:
        row = existing.get(spec["builtin_key"])
        if row is None:
            db.add(DevTemplate(kind="builtin", **spec))
        elif (
            spec.get("web_ports")
            and row.image == spec["image"]
            and row.web_ports is None
            and row.command is None
            and row.env is None
        ):
            # seeded before templates had web interfaces and not changed since
            for field in WEB_FIELDS:
                setattr(row, field, spec.get(field))
            row.name, row.description = spec["name"], spec["description"]
    await db.commit()


def web_base(session_id: int, port: int) -> str:
    """The path one of a dev environment's container ports is served under."""
    return f"/devenv/{session_id}/port/{port}/"


def load_web_ports(raw: str | None) -> list[dict]:
    """A template's declared web interfaces: [{name, port, kind}] (invalid entries are skipped)."""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        try:
            port = int(item["port"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= port <= 65535 or port in seen:
            continue
        seen.add(port)
        kind = item.get("kind") if item.get("kind") in WEB_KINDS else "http"
        out.append({"name": str(item.get("name") or port)[:32], "port": port, "kind": kind})
    return out


def placeholder_values(session_id: int, ports: list[dict], token: str) -> dict[str, str]:
    """``{base_path}``/``{port}`` are the first declared port's, ``{base_path_<port>}`` any declared port's."""
    values = {"session": str(session_id), "token": token, "base_path": "", "port": ""}
    for i, p in enumerate(ports):
        values[f"base_path_{p['port']}"] = web_base(session_id, p["port"])
        if i == 0:
            values.update(base_path=web_base(session_id, p["port"]), port=str(p["port"]))
    return values


def port_entry(web: dict | None, port: int) -> dict:
    """How the proxy treats a port: the declared entry, or a plain HTTP app that gets the prefix stripped."""
    for p in (web or {}).get("ports", []):
        if p["port"] == port:
            return p
    return {"name": str(port), "port": port, "kind": "http", "strip": True}


def network_name(session_id: int) -> str:
    return f"slotserve-devenv-{session_id}"


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Replaces ``{base_path}``, ``{token}``, ``{port}`` and ``{session}``; other braces are left alone."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def can_access(session: DevSession, user: User) -> bool:
    """Who may use an environment: its owner, admins, or anyone when it belongs to no booking."""
    return session.reservation_id is None or session.user_id == user.id or bool(user.is_admin)


def session_web(session: DevSession) -> dict | None:
    """How SlotServe reaches a running session's ports: {token, ports: [{name, port, kind, strip}], container,
    ip, published: {"<port>": "127.0.0.1:<host port>"}}."""
    if not session.web:
        return None
    try:
        return json.loads(session.web)
    except ValueError:
        return None


def visible(template: DevTemplate | None, user: User) -> bool:
    if template is None or template.deleted_at is not None:
        return False
    if template.kind == "builtin":
        return template.enabled
    return template.owner_id == user.id or user.is_admin


class _NoCookies(CookiePolicy):
    netscape = True
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False

    def domain_return_ok(self, domain, request) -> bool:
        return False

    def path_return_ok(self, path, request) -> bool:
        return False


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
        # In a container SlotServe joins each environment's network to reach its web interface;
        # on the host it uses a port Docker binds to 127.0.0.1.
        self.in_container = Path("/.dockerenv").exists()
        self._web_http: httpx.AsyncClient | None = None
        self.self_container = socket.gethostname()
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

    def web_client(self) -> httpx.AsyncClient:
        """The HTTP client the web proxy uses. It never keeps cookies, so nothing leaks between people."""
        if self._web_http is None:
            self._web_http = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=10.0),
                cookies=httpx.Cookies(CookieJar(policy=_NoCookies())),
                trust_env=False,
            )
        return self._web_http

    async def close(self) -> None:
        if self._web_http is not None:
            await self._web_http.aclose()
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
            "opt/slotserve/ssh.sh": (SSH_SCRIPT.encode(), 0o755),
            "opt/slotserve/start.sh": (START_SCRIPT.encode(), 0o755),
            "etc/profile.d/slotserve.sh": (PROFILE.encode(), 0o644),
        }
        archive = tar_files(files)
        web = self._web_spec(s, template)
        config = await self._container_config(s, r, user, template, devices, web)
        name = f"slotserve-dev-{s.id}"
        network = network_name(s.id)
        await self._drop_network(s.id)
        await self.docker.network_create(network, {LABEL: "1", "slotserve.session": str(s.id)})
        last_error = ""
        for port in candidates[:5]:
            config["HostConfig"]["PortBindings"]["22/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": str(port)}]
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
            if template is not None and template.keep_entrypoint:
                # the image runs its own init system; add the SSH server next to it
                await self.docker.exec_detached(cid, ["/bin/sh", "/opt/slotserve/ssh.sh"])
            await self._reach(s.id, cid, name, web)
            s.web = json.dumps(web)
            s.state, s.started_at = "running", utcnow()
            await db.commit()
            self.state.notifier.emit("devenv_ready", user_id=s.user_id, reservation_id=r.id, port=port)
            return
        raise DevEnvError("devenv_no_ports", detail=last_error)

    @staticmethod
    def _web_spec(s: DevSession, template: DevTemplate | None) -> dict:
        """What a new container serves: its declared ports and a random token for this start. A port whose app is
        told its base path ({base_path}/{base_path_<port>} in the command or environment) gets the full path; for
        the others the proxy strips the prefix."""
        ports = load_web_ports(template.web_ports) if template is not None else []
        text = ((template.command or "") + "\n" + (template.env or "")) if template is not None else ""
        for i, p in enumerate(ports):
            knows_base = f"{{base_path_{p['port']}}}" in text or (i == 0 and "{base_path}" in text)
            p["strip"] = not knows_base
        return {"token": secrets.token_urlsafe(24), "ports": ports, "container": None, "ip": None, "published": {}}

    async def _reach(self, session_id: int, cid: str, name: str, web: dict) -> None:
        """Records how SlotServe reaches the container's ports: in a container itself it joins the environment's
        network (any port is then reachable by name); on the host, declared ports were bound to 127.0.0.1 and
        the others are tried on the container's address in its network."""
        web["container"] = name
        network = network_name(session_id)
        if self.in_container:
            await self.docker.network_connect(network, self.self_container)
            return
        info = await self.docker.inspect(cid) or {}
        settings = info.get("NetworkSettings") or {}
        web["ip"] = ((settings.get("Networks") or {}).get(network) or {}).get("IPAddress") or None
        for p in web["ports"]:
            bindings = (settings.get("Ports") or {}).get(f"{p['port']}/tcp") or []
            host_port = next((b.get("HostPort") for b in bindings if b.get("HostPort")), None)
            if not host_port:
                raise DevEnvError("devenv_web_port_missing")
            web["published"][str(p["port"])] = f"127.0.0.1:{host_port}"

    def port_target(self, s: DevSession, port: int) -> str | None:
        """The upstream base URL (``http://host:port``) for one of a running session's container ports, or None.
        The single place the proxy resolves addresses (tests replace it)."""
        web = session_web(s)
        if s.state != "running" or not web or not 1 <= port <= 65535:
            return None
        if published := (web.get("published") or {}).get(str(port)):
            return f"http://{published}"
        if self.in_container and web.get("container"):
            return f"http://{web['container']}:{port}"
        if web.get("ip"):
            return f"http://{web['ip']}:{port}"
        return None

    async def _drop_network(self, session_id: int) -> None:
        network = network_name(session_id)
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
            if self.in_container:
                await self.docker.network_disconnect(network, self.self_container)
            await self.docker.network_remove(network)

    async def _container_config(self, s: DevSession, r: Reservation, user, template, devices, web=None) -> dict:
        keep = bool(template is not None and template.keep_entrypoint)
        web = web or {"token": "", "ports": []}
        values = placeholder_values(s.id, web["ports"], web["token"])
        env = {"SLOTSERVE_USER": user.username if user else "", "SLOTSERVE_END": r.end_at.isoformat()}
        if web["ports"]:
            env.update(
                SLOTSERVE_WEB_BASE=values["base_path"],
                SLOTSERVE_WEB_PORT=values["port"],
                SLOTSERVE_WEB_TOKEN=web["token"],
            )
            if any(p["kind"] == "jupyter" for p in web["ports"]):
                env["JUPYTER_TOKEN"] = web["token"]
        if template is not None and template.env:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                env.update({str(k): fill_placeholders(str(v), values) for k, v in json.loads(template.env).items()})
        command = (
            ["/bin/sh", "-c", fill_placeholders(template.command, values)]
            if template is not None and template.command
            else []
        )
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
            # an image that keeps its entrypoint may run an init system that has to be PID 1
            "Init": not keep,
            "RestartPolicy": {"Name": "no"},
            "NetworkMode": network_name(s.id),
            "PortBindings": {},
        }
        exposed: dict = {"22/tcp": {}}
        for p in web["ports"]:
            exposed[f"{p['port']}/tcp"] = {}
            if not self.in_container:
                # only SlotServe (on this host) reaches it; Docker picks a free port
                host_config["PortBindings"][f"{p['port']}/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": ""}]
        if self.settings.devenv_cpus > 0:
            host_config["NanoCpus"] = int(self.settings.devenv_cpus * 1e9)
        if self.settings.devenv_memory_gb > 0:
            host_config["Memory"] = int(self.settings.devenv_memory_gb * 1024**3)
        config = {
            "Image": s.image,
            "Hostname": f"{(user.username if user else 'dev')[:40]}-dev",
            "Env": [f"{k}={v}" for k, v in env.items()],
            "ExposedPorts": exposed,
            "Labels": {LABEL: "1", "slotserve.session": str(s.id), "slotserve.user": user.username if user else ""},
            "HostConfig": host_config,
        }
        if keep:
            # the image's own entrypoint and user; SSH is added with docker exec once it runs
            if command:
                config["Cmd"] = command
        else:
            config.update(
                Entrypoint=["/bin/sh", "/opt/slotserve/start.sh"], Cmd=command, User="root", WorkingDir="/workspace"
            )
        return config

    async def _stop(self, db, s: DevSession, state: str) -> None:
        if s.container_id:
            s.state = "stopping"
            await db.commit()
            with contextlib.suppress(DockerError):
                await self.docker.stop(s.container_id, timeout=10)
            await self.docker.remove(s.container_id)
        await self._drop_network(s.id)
        s.state, s.stopped_at = state, utcnow()
        s.detail = s.progress = s.web = None

    async def _fail(self, db, s: DevSession, message: str, notify: bool) -> None:
        log.warning("dev environment %s failed: %s", s.id, message)
        if s.container_id:
            with contextlib.suppress(DockerError):
                await self.docker.remove(s.container_id)
            s.container_id = None
        await self._drop_network(s.id)
        s.state, s.error, s.detail, s.progress, s.web = "failed", message[:4000], None, None, None
        if notify:
            self.state.notifier.emit(
                "devenv_failed", user_id=s.user_id, reservation_id=s.reservation_id, error=message.splitlines()[0][:300]
            )

    async def _cleanup(self, db) -> None:
        """After a restart: keep containers whose session is still running, remove the rest."""
        running = {
            str(s.id): s for s in (await db.execute(select(DevSession).where(DevSession.state == "running"))).scalars()
        }
        kept = set()
        for c in await self.docker.list(f"{LABEL}=1"):
            sid = (c.get("Labels") or {}).get("slotserve.session")
            if sid in running and c.get("State") == "running":
                kept.add(sid)
                continue
            await self.docker.remove(c["Id"])
        for n in await self.docker.networks(f"{LABEL}=1"):
            sid = (n.get("Labels") or {}).get("slotserve.session")
            if sid in kept:
                if self.in_container:
                    # SlotServe may have been recreated: join the network again to reach the web interface
                    with contextlib.suppress(DockerError):
                        await self.docker.network_connect(n.get("Name") or n["Id"], self.self_container)
                continue
            with contextlib.suppress(DockerError):
                if self.in_container:
                    await self.docker.network_disconnect(n.get("Name") or n["Id"], self.self_container)
                await self.docker.network_remove(n.get("Name") or n["Id"])

    # ---- actions ----------------------------------------------------------------

    async def restart(self, session_id: int) -> None:
        async with self._lock, SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            if s is None or s.state not in OPEN_STATES:
                raise DevEnvError("not_found")
            if s.container_id:
                with contextlib.suppress(DockerError):
                    await self.docker.remove(s.container_id)
            await self._drop_network(s.id)
            s.container_id, s.state, s.error, s.detail, s.progress = None, "waiting", None, None, None
            s.web = None
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
                        web_ports=source.web_ports if source else None,
                        keep_entrypoint=source.keep_entrypoint if source else False,
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
