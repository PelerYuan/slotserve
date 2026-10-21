"""Dev environments on other machines (nodes): their SSH, direct ports and web interfaces through the relay.

Nothing connects to a node: the control plane itself listens and relays every connection through the node's agent
(``cluster/relay.py``) to the container's port on that machine.

* SSH: the environment's ``ssh_port`` (from ``devenv_ssh_ports``) is a listener on the control plane
  (``devenv_relay_host``) relayed to the container's port 22, so the ssh command always names the control plane.
* Direct ports: a listener on the port's host port (``devenv_public_ports``) relayed to the container port; no
  forwarder container is needed on the node.
* Web interfaces: a listener on a free loopback port of the control plane, used by the web proxy like any other
  upstream (``DevEnvManager.port_upstream``).

The containers themselves publish nothing beyond the node's loopback interface: the agent connects to the
container's port there (or to the container's address where container networks are routable).
"""

from __future__ import annotations

import logging

from ..cluster.relay import RelayListener
from ..orm import DevPort, DevSession

log = logging.getLogger(__name__)


class RelayPorts:
    def __init__(self, manager):
        self.m = manager
        self.ssh: dict[int, RelayListener] = {}  # session id -> its SSH listener
        self.direct: dict[int, RelayListener] = {}  # DevPort id -> its listener
        self.direct_session: dict[int, int] = {}  # DevPort id -> the session it serves
        self.web: dict[tuple[int, int], RelayListener] = {}  # (session id, container port) -> loopback listener

    @property
    def settings(self):
        return self.m.settings

    def _relay(self):
        cluster = getattr(self.m.state, "cluster", None)
        return cluster.relay if cluster is not None else None

    def _executor(self, node_id: int | None):
        def get():
            return self.m.executor(node_id)

        return get

    @staticmethod
    def target(s: DevSession, port: int, container: str | None = None, network: str | None = None) -> dict:
        from .devenv import session_web

        web = session_web(s) or {}
        return {
            "kind": "container",
            "container": container or s.container_name,
            "network": network or web.get("network"),
            "port": port,
        }

    def ssh_target(self, s: DevSession, container: str | None = None, network: str | None = None) -> dict:
        """Where an SSH connection to an environment on another machine goes: the one place this is decided.

        Today every environment has its own port on the control plane (``open_ssh``). A single SSH front that routes
        by name (``ssh bob+thesis@server``) would terminate SSH on the control plane, find the session from the
        user name, and open ``Relay.open(self.m.executor(s.node_id), self.ssh_target(s))`` for the session's
        channel; the relay and the agent need nothing new."""
        return self.target(s, 22, container, network)

    async def _listen(self, s: DevSession, target: dict, host: str, listen_port: int, label: str) -> RelayListener:
        relay = self._relay()
        if relay is None:
            raise OSError("the relay is not available")
        listener = RelayListener.to(relay, self._executor(s.node_id), target, host, listen_port, label)
        await listener.start()
        return listener

    # ---- SSH ----------------------------------------------------------------------------------------

    async def open_ssh(self, s: DevSession, port: int, container: str | None = None, network: str | None = None):
        """Listens on ``port`` for the session's SSH (raises ``OSError`` when the port is taken here)."""
        target = self.ssh_target(s, container, network)
        current = self.ssh.get(s.id)
        if current is not None and current.port == port and current.key == target:
            return
        await self.close_ssh(s.id)
        host = self.settings.devenv_relay_host
        self.ssh[s.id] = await self._listen(s, target, host, port, f"ssh of environment {s.id}")

    async def close_ssh(self, session_id: int) -> None:
        listener = self.ssh.pop(session_id, None)
        if listener is not None:
            await listener.close()

    # ---- web interfaces ----------------------------------------------------------------------------

    async def web_upstream(self, s: DevSession, port: int) -> str | None:
        """``http://127.0.0.1:<port>`` relaying to one of a running remote session's container ports."""
        if s.state != "running" or not s.container_name or not 1 <= port <= 65535:
            return None
        key = (s.id, port)
        listener = self.web.get(key)
        if listener is not None and listener.key != self.target(s, port):
            await listener.close()
            listener = None
        if listener is None:
            try:
                listener = await self._listen(
                    s, self.target(s, port), "127.0.0.1", 0, f"port {port} of environment {s.id}"
                )
            except OSError as exc:
                log.warning("dev environment %s: no relay for port %s: %s", s.id, port, exc)
                return None
            self.web[key] = listener
        return f"http://127.0.0.1:{listener.port}"

    # ---- keeping listeners in line with running environments ----------------------------------------

    async def reconcile(self, db, running: list[DevSession], rows: dict[int, list[DevPort]]) -> set[int]:
        """Listeners for the running remote sessions (SSH, direct ports); everything else is closed.

        ``rows``: each session's port rows. Returns the ids of the direct port rows that are served."""
        by_id = {s.id: s for s in running}
        for sid in [k for k in self.ssh if k not in by_id]:
            await self.close_ssh(sid)
        for key in [k for k in self.web if k[0] not in by_id]:
            await self.web.pop(key).close()
        for s in running:
            current = self.ssh.get(s.id)
            if s.ssh_port and (current is None or current.key != self.ssh_target(s) or current.port != s.ssh_port):
                try:
                    await self.open_ssh(s, s.ssh_port)
                except OSError as exc:
                    log.warning("dev environment %s: cannot listen on SSH port %s: %s", s.id, s.ssh_port, exc)
                    s.error = f"SSH port {s.ssh_port} is taken on the server: {exc}"[:500]
        active: set[int] = set()
        wanted: dict[int, tuple[DevSession, DevPort]] = {}
        if self.settings.devenv_public_ports_enabled:
            for s in running:
                for row in rows.get(s.id, []):
                    if row.mode == "direct" and row.host_port:
                        wanted[row.id] = (s, row)
        for row_id in [k for k in self.direct if k not in wanted]:
            await self.close_port(row_id)
        for row_id, (s, row) in wanted.items():
            current = self.direct.get(row_id)
            moved = current is not None and (
                current.port != row.host_port or current.key != self.target(s, row.container_port)
            )
            if moved:
                await self.close_port(row_id)
                current = None
            if current is None:
                try:
                    self.direct[row_id] = await self._listen(
                        s,
                        self.target(s, row.container_port),
                        self.settings.devenv_relay_host,
                        row.host_port,
                        f"direct port {row.host_port} of environment {s.id}",
                    )
                except OSError as exc:
                    row.error = f"host port {row.host_port}: {exc}"[:500]
                    continue
                self.direct_session[row_id] = s.id
            active.add(row_id)
            row.error = None
            if row.active_at is None:
                from ..db import utcnow

                row.active_at = utcnow()
        return active

    async def drop(self, s: DevSession) -> None:
        """An environment stops, pauses or restarts: its listeners go."""
        await self.close_ssh(s.id)
        for key in [k for k in self.web if k[0] == s.id]:
            await self.web.pop(key).close()
        for row_id in [k for k, sid in self.direct_session.items() if sid == s.id]:
            await self.close_port(row_id)

    async def close_port(self, row_id: int) -> None:
        self.direct_session.pop(row_id, None)
        listener = self.direct.pop(row_id, None)
        if listener is not None:
            await listener.close()

    async def close(self) -> None:
        for listener in [*self.ssh.values(), *self.direct.values(), *self.web.values()]:
            await listener.close()
        self.ssh.clear()
        self.direct.clear()
        self.direct_session.clear()
        self.web.clear()
