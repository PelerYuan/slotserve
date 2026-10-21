"""Ports of dev environments: which container ports are listed, how they are opened, and direct host ports.

Every environment lists its template's declared ports and the ones its owner added while it runs. A port is opened
either through SlotServe's web proxy behind the SlotServe login (``login``, see ``gateway/devenv_web.py``) or
``direct``: published on a host port from ``devenv_public_ports`` as ``http://server:<host port>``, for any app and
protocol, without SlotServe in between.

A direct port is served by a small forwarder container (socat, ``devenv_forwarder_image``) that joins the
environment's Docker network and publishes the host port, so ports can be added and removed while the environment
runs, without restarting it. Forwarders only run while their environment runs; ``PortManager.reconcile`` (every
tick of the dev environment loop) starts the missing ones and removes the rest.

Host ports are fixed: a kept environment's direct port belongs to its project (the number stays while it is paused
and comes back when it resumes) until the project is deleted; any other environment's belongs to its session and is
released when the booking ends. Admins can reclaim any of them.
"""

import asyncio
import contextlib
import json
import logging
import re

import httpx
from sqlalchemy import func, select

from ..db import utcnow
from ..orm import DevPort, DevProject, DevSession, DevTemplate, User
from .docker_api import DockerError

log = logging.getLogger(__name__)

FORWARDER = "forwarder"  # value of the slotserve.devenv label on forwarder containers
FORWARD_LISTEN = 18080  # socat listens here inside the forwarder (unprivileged)
MAX_PORTS = 20  # ports one environment can list (declared ones included)
RESERVED = {22}  # SSH has its own host port
RETRY_SECONDS = 30.0
PROBE_SECONDS = 3.0
MODES = ("login", "direct")


def env_filter(s: DevSession):
    """The rows of a session's environment: its project's, or its own."""
    return DevPort.project_id == s.project_id if s.project_id else DevPort.session_id == s.id


def env_key(s: DevSession) -> str:
    return f"p{s.project_id}" if s.project_id else f"s{s.id}"


def forwarder_name(row: DevPort) -> str:
    return f"slotserve-fwd-{row.id}"


def _session_web(s: DevSession) -> dict:
    try:
        data = json.loads(s.web) if s.web else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_listening(text: str) -> dict[int, str]:
    """Listening TCP ports from /proc/net/tcp and tcp6: {port: "all" | "local"} (local = loopback only)."""
    out: dict[int, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A" or ":" not in parts[1]:
            continue
        addr, _, port_hex = parts[1].rpartition(":")
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        local = addr in ("0100007F", "00000000000000000000000001000000", "0000000000000000FFFF00000100007F")
        if out.get(port) != "all":
            out[port] = "local" if local else "all"
    return out


class PortManager:
    def __init__(self, manager):
        self.m = manager
        self.active: set[int] = set()  # rows whose forwarder runs
        self._retry: dict[int, float] = {}  # row id -> loop time before which a failed forwarder is not retried
        self._pull: asyncio.Task | None = None
        self._pull_error: str | None = None
        self._probes: dict[int, tuple[float, dict[int, str] | None]] = {}

    @property
    def settings(self):
        return self.m.settings

    @property
    def docker(self):
        return self.m.docker

    # ---- what an environment lists -----------------------------------------------------

    @staticmethod
    async def rows(db, s: DevSession) -> list[DevPort]:
        return list((await db.execute(select(DevPort).where(env_filter(s)).order_by(DevPort.container_port))).scalars())

    @staticmethod
    async def declared(db, s: DevSession) -> list[dict]:
        """The template's ports: those the running container was created with, otherwise the template's now."""
        from .devenv import load_web_ports

        if s.state == "running" and s.web:
            return [dict(p) for p in _session_web(s).get("ports", [])]
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        if project is not None and project.container_web:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                return [dict(p) for p in json.loads(project.container_web).get("ports", [])]
        template = await db.get(DevTemplate, s.template_id) if s.template_id else None
        return load_web_ports(template.web_ports) if template is not None else []

    @staticmethod
    def merge(declared: list[dict], rows: list[DevPort]) -> list[dict]:
        """One entry per container port: {name, port, kind, mode, declared, row}."""
        by_port = {r.container_port: r for r in rows}
        out = []
        for d in declared:
            r = by_port.pop(d["port"], None)
            out.append(
                {
                    "name": (r.name if r is not None and r.name else d["name"]),
                    "port": d["port"],
                    "kind": d.get("kind", "http"),
                    "mode": r.mode if r is not None else d.get("mode", "login"),
                    "declared": True,
                    "row": r,
                }
            )
        for r in sorted(by_port.values(), key=lambda x: x.container_port):
            out.append(
                {
                    "name": r.name or str(r.container_port),
                    "port": r.container_port,
                    "kind": "http",
                    "mode": r.mode,
                    "declared": False,
                    "row": r,
                }
            )
        return out

    async def owner_of(self, db, s: DevSession) -> int | None:
        if s.project_id:
            project = await db.get(DevProject, s.project_id)
            return project.owner_id if project is not None else s.user_id
        return s.user_id

    async def direct_count(self, db, owner_id: int) -> int:
        return (
            await db.execute(
                select(func.count(DevPort.id)).where(DevPort.owner_id == owner_id, DevPort.host_port.is_not(None))
            )
        ).scalar_one()

    async def free_host_port(self, db, avoid: set[int] | None = None) -> int | None:
        from .devenv import parse_port_range

        lo, hi = parse_port_range(self.settings.devenv_public_ports)
        taken = set((await db.execute(select(DevPort.host_port).where(DevPort.host_port.is_not(None)))).scalars())
        taken |= {self.settings.port, self.settings.devenv_web_port, *(avoid or set())}
        return next((p for p in range(lo, hi + 1) if p not in taken), None)

    # ---- running forwarders ------------------------------------------------------------------

    async def _forwarders(self) -> list[dict]:
        from .devenv import LABEL

        return await self.docker.list(f"{LABEL}={FORWARDER}")

    async def drop_forwarders(self, s: DevSession) -> None:
        """An environment stops, pauses or restarts: its forwarders go (its host ports stay assigned)."""
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
            for c in await self._forwarders():
                if (c.get("Labels") or {}).get("slotserve.env") == env_key(s):
                    await self.docker.remove(c["Id"])
                    with contextlib.suppress(ValueError):
                        self.active.discard(int((c.get("Labels") or {}).get("slotserve.port", "")))

    async def remove_all(self) -> None:
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
            for c in await self._forwarders():
                await self.docker.remove(c["Id"])
        self.active = set()

    async def remove_row(self, db, row: DevPort) -> None:
        """Releases a port now (its forwarder too)."""
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
            await self.docker.remove(forwarder_name(row))
        self.active.discard(row.id)
        await db.delete(row)

    async def release_host_port(self, row: DevPort) -> None:
        """The port stays listed but loses its host port (and forwarder)."""
        with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
            await self.docker.remove(forwarder_name(row))
        self.active.discard(row.id)
        row.host_port, row.active_at, row.error = None, None, None

    async def release_project(self, db, project: DevProject) -> None:
        for row in (await db.execute(select(DevPort).where(DevPort.project_id == project.id))).scalars().all():
            await self.remove_row(db, row)

    async def _declare(self, db, s: DevSession, rows: list[DevPort]) -> list[DevPort]:
        """A running environment's declared direct ports get their row (and host port) once."""
        have = {r.container_port for r in rows}
        wanted = [p for p in _session_web(s).get("ports", []) if p.get("mode") == "direct" and p["port"] not in have]
        if not wanted or not self.settings.devenv_public_ports_enabled:
            return rows
        owner_id = await self.owner_of(db, s)
        owner = await db.get(User, owner_id) if owner_id else None
        for p in wanted:
            row = DevPort(
                owner_id=owner_id,
                project_id=s.project_id,
                session_id=None if s.project_id else s.id,
                container_port=p["port"],
                name=str(p.get("name") or p["port"])[:32],
                mode="direct",
                declared=True,
            )
            problem = await self.direct_problem(db, owner, owner_id)
            if problem is None:
                row.host_port = await self.free_host_port(db)
                problem = None if row.host_port else "devenv_public_ports_full"
            if problem is not None:
                row.mode, row.error = "login", problem
            db.add(row)
            await db.flush()
            rows.append(row)
        return rows

    async def direct_problem(self, db, owner, owner_id: int | None, acting_admin: bool = False) -> str | None:
        """Why the owner of an environment cannot have one more direct port (None: they can)."""
        if not self.settings.devenv_public_ports_enabled:
            return "devenv_public_ports_disabled"
        if owner_id is None or acting_admin or (owner is not None and owner.is_admin):
            return None  # a lane's shared environment, or an admin's
        limit = self.settings.devenv_public_port_limit
        if limit <= 0:
            return "devenv_public_ports_admin_only"
        if await self.direct_count(db, owner_id) >= limit:
            return "devenv_public_port_limit"
        return None

    async def reconcile(self, db) -> None:
        """Runs the forwarders of running environments' direct ports, removes the others, and releases the ports
        of environments that are over (not kept)."""
        from .devenv import OPEN_STATES

        open_ids = set((await db.execute(select(DevSession.id).where(DevSession.state.in_(OPEN_STATES)))).scalars())
        for row in (await db.execute(select(DevPort).where(DevPort.session_id.is_not(None)))).scalars().all():
            if row.session_id not in open_ids:
                await db.delete(row)  # the booking is over; nothing runs on it any more
        running = (await db.execute(select(DevSession).where(DevSession.state == "running"))).scalars().all()
        wanted: dict[str, tuple[DevPort, DevSession, str]] = {}
        for s in running:
            if not s.container_name:
                continue
            rows = await self._declare(db, s, await self.rows(db, s))
            network = _session_web(s).get("network") or ""
            for row in rows:
                if row.mode == "direct" and row.host_port and network and self.settings.devenv_public_ports_enabled:
                    spec = f"{s.container_name}:{row.container_port}>{row.host_port}@{network}"
                    wanted[forwarder_name(row)] = (row, s, spec)
        await db.commit()
        try:
            existing = await self._forwarders()
        except (DockerError, httpx.HTTPError, OSError) as exc:
            log.warning("listing port forwarders failed: %s", exc)
            return
        have: set[str] = set()
        active: set[int] = set()
        for c in existing:
            labels = c.get("Labels") or {}
            name = labels.get("slotserve.name", "")
            item = wanted.get(name)
            if item is not None and labels.get("slotserve.spec") == item[2] and c.get("State") == "running":
                have.add(name)
                active.add(item[0].id)
                continue
            with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                await self.docker.remove(c["Id"])
        self.active = active
        missing = [v for k, v in wanted.items() if k not in have]
        if not missing:
            return
        image = self.settings.devenv_forwarder_image
        if not await self.docker.image_exists(image):
            self._ensure_pull(image)
            for row, _s, _spec in missing:
                row.error = self._pull_error
            await db.commit()
            return
        loop = asyncio.get_running_loop()
        for row, s, spec in missing:
            if self._retry.get(row.id, 0.0) > loop.time():
                continue
            await self._start(db, row, s, spec, image)
        await db.commit()

    def _ensure_pull(self, image: str) -> None:
        if self._pull is not None and not self._pull.done():
            return

        async def pull():
            try:
                await self.docker.pull(image)
                self._pull_error = None
            except (DockerError, httpx.HTTPError, OSError) as exc:
                message = exc.message if isinstance(exc, DockerError) else (str(exc) or type(exc).__name__)
                self._pull_error = f"pulling {image} failed: {message}"[:500]
                log.warning("%s", self._pull_error)
            finally:
                self.m.poke()

        self._pull = asyncio.create_task(pull(), name=f"pull {image}")

    def _config(self, row: DevPort, s: DevSession, spec: str, image: str, network: str) -> dict:
        from .devenv import LABEL

        listen = f"{FORWARD_LISTEN}/tcp"
        return {
            "Image": image,
            "Entrypoint": ["socat"],
            "Cmd": [
                f"TCP-LISTEN:{FORWARD_LISTEN},fork,reuseaddr",
                f"TCP:{s.container_name}:{row.container_port}",
            ],
            "User": "65534:65534",
            "ExposedPorts": {listen: {}},
            "Labels": {
                LABEL: FORWARDER,
                "slotserve.name": forwarder_name(row),
                "slotserve.port": str(row.id),
                "slotserve.env": env_key(s),
                "slotserve.session": str(s.id),
                "slotserve.spec": spec,
            },
            "HostConfig": {
                "NetworkMode": network,
                "PortBindings": {listen: [{"HostIp": "0.0.0.0", "HostPort": str(row.host_port)}]},
                "RestartPolicy": {"Name": "no"},
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": 64 * 1024**2,
                "PidsLimit": 256,
            },
        }

    async def _start(self, db, row: DevPort, s: DevSession, spec: str, image: str) -> None:
        network = _session_web(s).get("network") or ""
        name = forwarder_name(row)
        loop = asyncio.get_running_loop()
        try:
            await self.docker.remove(name)
            cid = await self.docker.create(name, self._config(row, s, spec, image, network))
            try:
                await self.docker.start(cid)
            except DockerError:
                with contextlib.suppress(DockerError, httpx.HTTPError, OSError):
                    await self.docker.remove(cid)
                raise
        except DockerError as exc:
            from .devenv import _port_taken

            self._retry[row.id] = loop.time() + RETRY_SECONDS
            if _port_taken(exc) and row.active_at is None:
                # never used yet: take another number instead of waiting for this one
                row.host_port = await self.free_host_port(db, avoid={row.host_port or 0})
                row.error = None if row.host_port else "devenv_public_ports_full"
                if row.host_port is None:
                    row.mode = "login"
                self._retry.pop(row.id, None)
            else:
                row.error = f"host port {row.host_port}: {exc.message}"[:500]
                log.warning("dev environment %s: forwarder for port %s failed: %s", s.id, row.container_port, exc)
            return
        except (httpx.HTTPError, OSError) as exc:
            log.warning("dev environment %s: docker unreachable while starting a forwarder: %s", s.id, exc)
            return
        self._retry.pop(row.id, None)
        self.active.add(row.id)
        row.error = None
        if row.active_at is None:
            row.active_at = utcnow()

    # ---- probes and saving -------------------------------------------------------------------------

    async def listening(self, s: DevSession) -> dict[int, str] | None:
        """The TCP ports something listens on inside a running environment (None: unknown)."""
        if s.state != "running" or not s.container_id:
            return None
        loop = asyncio.get_running_loop()
        cached = self._probes.get(s.id)
        if cached is not None and cached[0] > loop.time():
            return cached[1]
        result: dict[int, str] | None
        try:
            _code, out = await self.docker.exec(
                s.container_id, ["/bin/sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"], timeout=10
            )
            result = parse_listening(out) if out.strip() else None
        except (DockerError, httpx.HTTPError, OSError, ValueError):
            result = None
        self._probes[s.id] = (loop.time() + PROBE_SECONDS, result)
        if len(self._probes) > 200:
            self._probes = {k: v for k, v in self._probes.items() if v[0] > loop.time()}
        return result

    async def saved_web_ports(self, db, s: DevSession, source: DevTemplate | None) -> str | None:
        """The declarations of an image saved from an environment: the declared ports and the ones added at
        runtime, each with how it is opened."""
        from .devenv import load_web_ports

        declared = load_web_ports(source.web_ports) if source is not None else []
        items = []
        for e in self.merge(declared, await self.rows(db, s))[:10]:
            item = {"name": e["name"], "port": e["port"], "kind": e["kind"]}
            if e["mode"] == "direct":
                item["mode"] = "direct"
            items.append(item)
        return json.dumps(items) if items else None


PORT_NAME_RE = re.compile(r"[\x00-\x1f]")


def clean_name(name: str, port: int) -> str:
    return PORT_NAME_RE.sub("", name).strip()[:32] or str(port)
