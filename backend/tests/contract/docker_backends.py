"""The two Docker backends of the contract tests (see conftest.py)."""

import os
import secrets
import socket

from docker_mock import FakeDocker

from slotserve.containers.harden import harden
from slotserve.containers.model import NodePolicy
from slotserve.services.docker_api import DockerClient, DockerError

REAL = os.environ.get("SLOTSERVE_TEST_DOCKER") == "1"
GPU = os.environ.get("SLOTSERVE_TEST_GPU") == "1"
IMAGE = os.environ.get("SLOTSERVE_TEST_IMAGE", "busybox:1.36")
LABEL = "slotserve.contract"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend:
    """A Docker client plus what a case needs to say to the fake (no-ops against real Docker)."""

    def __init__(self, kind: str, docker: DockerClient, fake: FakeDocker | None):
        self.kind = kind
        self.docker = docker
        self.fake = fake
        self.run = secrets.token_hex(4)
        self.images: list[str] = []  # images a case created (removed afterwards)
        self.image = IMAGE

    @property
    def real(self) -> bool:
        return self.fake is None

    def name(self, what: str) -> str:
        return f"slotserve-contract-{self.run}-{what}"

    def labels(self, **extra: str) -> dict[str, str]:
        return {LABEL: self.run, **extra}

    def container(self, entrypoint: list[str] | None = None, host: dict | None = None, **config) -> dict:
        """A create body for a small container that keeps running (busybox ``sleep``) unless told otherwise."""
        body = {
            "Image": self.image,
            "Entrypoint": entrypoint or ["sleep", "3600"],
            "Cmd": [],
            "Labels": self.labels(),
            "HostConfig": {"Init": True, "RestartPolicy": {"Name": "no"}, **(host or {})},
        }
        body.update(config)
        # every container goes through harden() (DockerClient.create refuses anything else): the contract covers
        # what Docker does with hardened bodies
        body["HostConfig"].setdefault("Memory", 256 * 1024**2)
        return harden(body, kind="helper", node_policy=NodePolicy())

    # ---- what the fake is told (real Docker answers for itself) ------------------------------------------------

    def exec_returns(self, code: int, output: str) -> None:
        if self.fake is not None:
            self.fake.exec_result = (code, output.encode())

    def helper_prints(self, needle: str, output: str, code: int) -> None:
        if self.fake is not None:
            self.fake.helper_output[needle] = output
            self.fake.wait_code = code

    async def occupy_port(self, port: int) -> None:
        """Something else holds a host port."""
        if self.fake is not None:
            self.fake.busy_ports.add(str(port))
            return
        body = self.container(
            ExposedPorts={"9/tcp": {}},
            host={"PortBindings": {"9/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]}},
        )
        cid = await self.docker.create(self.name("holder"), body)
        await self.docker.start(cid)

    async def cleanup(self) -> None:
        if self.fake is not None:
            return
        for c in await self.docker.list(f"{LABEL}={self.run}"):
            await self.docker.remove(c["Id"])
        for n in await self.docker.networks(f"{LABEL}={self.run}"):
            try:
                await self.docker.network_remove(n["Id"])
            except DockerError:
                pass
        for v in await self.docker.volumes(f"{LABEL}={self.run}"):
            try:
                await self.docker.remove_volume(v["Name"])
            except DockerError:
                pass
        for ref in self.images:
            await self.docker.remove_image(ref)
