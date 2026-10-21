"""Model libraries (Admin -> Storage, purpose "models") on a node.

* A host-directory library is used at its path when an admin marked it as present on this node (same path).
* An NFS / SMB library is mounted the way the control plane mounts it: a Docker named volume of the ``local``
  driver. The agent creates the volume on this machine's Docker and keeps a tiny holder container running with it,
  so Docker keeps the share mounted at the volume's mount point (``/var/lib/docker/volumes/<name>/_data``). Model
  servers and downloads use that directory, which the agent must be able to reach (when the agent runs in a
  container, mount ``/var/lib/docker/volumes`` at the same path with ``rslave`` propagation).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from ..config import Settings
from ..services.docker_api import DockerClient, DockerError

log = logging.getLogger(__name__)

HOLDER_LABEL = "slotserve.storage"
HOLDER_TARGET = "/library"


class LibraryError(Exception):
    pass


class AgentLibraries:
    def __init__(self, settings: Settings, docker: Callable[[], DockerClient] | None = None):
        self.settings = settings
        self._docker_factory = docker
        self.dirs: dict[int, str] = {}  # storage id -> directory on this machine
        self.errors: dict[int, str] = {}

    def docker(self) -> DockerClient:
        if self._docker_factory is not None:
            return self._docker_factory()
        return DockerClient(self.settings.docker_host)

    def describe(self) -> list[dict]:
        ids = sorted(set(self.dirs) | set(self.errors))
        return [{"storage_id": i, "path": self.dirs.get(i), "error": self.errors.get(i)} for i in ids]

    async def prepare(self, spec: dict) -> dict:
        """``{storage_id, path}`` (host directory) or ``{storage_id, volume: {name, driver_opts, labels}}``."""
        storage_id = int(spec["storage_id"])
        try:
            if spec.get("volume"):
                path = await self._volume(spec["volume"])
            else:
                path = str(spec.get("path") or "")
                if not path or not Path(path).is_absolute():
                    raise LibraryError("the library has no absolute path")
                if not Path(path).is_dir():
                    raise LibraryError(f"{path} is not a directory on this machine")
        except (LibraryError, DockerError, OSError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            self.errors[storage_id] = message[:500]
            self.dirs.pop(storage_id, None)
            raise LibraryError(message) from exc
        self.errors.pop(storage_id, None)
        self.dirs[storage_id] = path
        return {"storage_id": storage_id, "path": path, "writable": os.access(path, os.W_OK)}

    async def sync(self, specs: list[dict]) -> None:
        """The libraries the control plane wants on this node (sent in ``welcome``); others are forgotten."""
        wanted = {int(s["storage_id"]) for s in specs}
        for sid in list(self.dirs):
            if sid not in wanted:
                self.dirs.pop(sid, None)
        for sid in list(self.errors):
            if sid not in wanted:
                self.errors.pop(sid, None)
        for spec in specs:
            try:
                await self.prepare(spec)
            except LibraryError as exc:
                log.warning("model library %s is not available on this node: %s", spec.get("storage_id"), exc)

    async def _volume(self, volume: dict) -> str:
        name = str(volume["name"])
        docker = self.docker()
        info = await docker.volume_inspect(name)
        if info is None:
            await docker.volume_create(name, volume.get("driver_opts") or {}, volume.get("labels") or {})
        holder = f"slotserve-lib-{name}"[:120]
        found = await docker.inspect(holder)
        if found is None or not (found.get("State") or {}).get("Running"):
            image = self.settings.storage_helper_image
            if not await docker.image_exists(image):
                await docker.pull(image)
            if found is not None:
                await docker.remove(holder)
            config = {
                "Image": image,
                "Entrypoint": ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 3600 & wait; done"],
                "Cmd": [],
                "Labels": {HOLDER_LABEL: "holder"},
                "HostConfig": {
                    "Mounts": [{"Type": "volume", "Source": name, "Target": HOLDER_TARGET}],
                    "RestartPolicy": {"Name": "unless-stopped"},
                },
            }
            cid = await docker.create(holder, config)
            await docker.start(cid)
        info = await docker.volume_inspect(name) or {}
        mountpoint = str(info.get("Mountpoint") or "")
        if not mountpoint:
            raise LibraryError(f"Docker reports no mount point for volume {name}")
        if not Path(mountpoint).is_dir():
            raise LibraryError(
                f"the volume is mounted at {mountpoint}, which the agent cannot see "
                "(mount /var/lib/docker/volumes into the agent container at the same path)"
            )
        return mountpoint
