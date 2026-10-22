"""What a container is made of, before it becomes a Docker create body (``render.py``).

``ContainerSpec`` names the parts SlotServe decides (image, environment, mounts, GPUs, CPU and memory, network, files
copied in, ports, how it starts) without the shape of Docker's JSON. Fields are only ever added, never renamed: a
rename has to update the golden files (``tests/unit/containers``) in the same change.
"""

from dataclasses import dataclass, field
from typing import Literal

# What a container is for; harden() applies its rules per kind.
Kind = Literal["booking", "shared", "cpu", "project", "helper", "forwarder", "engine"]


class SpecError(Exception):
    """A container cannot be built as asked (an error code for the person, with parameters)."""

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


@dataclass
class ContainerSpec:
    name: str
    image: str
    kind: Kind = "booking"
    hostname: str = ""
    env: dict[str, str] = field(default_factory=dict)  # in order
    mounts: list[dict] = field(default_factory=list)  # Docker mount objects, in order
    binds: list[str] = field(default_factory=list)  # legacy "source:target[:ro]" binds (helpers only)
    devices: list[dict] | None = None  # Docker DeviceRequests; None: no GPU
    cpu: dict = field(default_factory=dict)  # CpusetCpus / NanoCpus / CpuShares
    memory: int | None = None  # bytes; None: harden() picks the node's default
    pids: int | None = None  # None: harden() picks the node's default
    shm: int | None = None  # bytes
    network: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    files: dict[str, tuple[bytes, int]] = field(default_factory=dict)  # copied into the container: path -> (data, mode)
    exposed: list[int] = field(default_factory=list)  # TCP ports
    port_bindings: dict[str, list[dict]] = field(default_factory=dict)  # "<port>/tcp" -> [{HostIp, HostPort}]
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    user: str | None = None
    workdir: str | None = None
    init: bool | None = None
    restart: str | None = None
    log: dict | None = None  # Docker LogConfig; None: harden() sets the bounded default
    host_extra: dict = field(default_factory=dict)  # other HostConfig fields (e.g. ReadonlyRootfs, CapDrop)
    compat: bool = False  # the template asked for compatibility mode (D14): no no-new-privileges
    # Not part of the Docker body: what the dev environment manager does around the container.
    keep_entrypoint: bool = False  # the image keeps its own entrypoint and user; SSH comes with docker exec
    prepare: list[tuple[str, str]] = field(default_factory=list)  # (dir under the storage dir, path) to create
    network_labels: dict[str, str] = field(default_factory=dict)
    accounts_digest: str | None = None
    web: dict = field(default_factory=dict)  # declared ports and token (see DevEnvManager._web_spec)


GiB = 1024**3


@dataclass(frozen=True)
class NodePolicy:
    """What the machine a container runs on allows. harden() reads it; it can only make a body stricter."""

    host_memory: int | None = None  # bytes (Docker's MemTotal); None: unknown
    pids_limit: int = 4096
    memory_reserve: int = 4 * GiB  # kept free for the host and SlotServe: at least this ...
    memory_reserve_fraction: float = 0.10  # ... or this share of the host's memory, whichever is more

    def default_memory(self) -> int | None:
        """A container's memory limit when nothing else sets one: the host's memory less the reserve."""
        if not self.host_memory:
            return None
        reserve = max(self.memory_reserve, int(self.host_memory * self.memory_reserve_fraction))
        return max(self.host_memory - reserve, min(self.host_memory, GiB) // 2)
