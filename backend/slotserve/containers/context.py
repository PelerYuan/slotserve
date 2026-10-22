"""SessionContext: everything a dev environment container is built from, resolved before the build.

The dev environment manager reads the database, the settings, Docker's host info and the SSH key files, and hands
the result to ``build_spec`` as one value. Contributors only read it (no I/O, no database, no Docker); the storage
plan is the one object they add to (folders to prepare, volumes to create).
"""

from dataclasses import dataclass, field
from typing import Any

from .model import NodePolicy


@dataclass(frozen=True)
class UserInfo:
    id: int
    username: str


@dataclass(frozen=True)
class TemplateInfo:
    id: int
    command: str | None = None
    env: str | None = None  # JSON object, as stored
    web_ports: str | None = None  # JSON list, as stored
    keep_entrypoint: bool = False
    persist_paths: list[str] = field(default_factory=list)
    compat: bool = False


@dataclass(frozen=True)
class ProjectInfo:
    id: int
    persist_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LaneInfo:
    id: int
    name: str
    devices: list[int] | None = None  # None: the lane does not name its GPUs


@dataclass(frozen=True)
class Account:
    """One login of a shared idle environment."""

    name: str
    uid: int
    keys: tuple[str, ...] = ()
    hash: str = "*"  # SHA-512-crypt password hash; "*" = no password


@dataclass(frozen=True)
class SshInputs:
    binary: bytes  # the static Dropbear SlotServe copies in
    host_key: bytes  # this environment's own host key
    keys: tuple[str, ...] = ()  # the person's public keys (booking, cpu)
    secret: str | None = None  # the person's SHA-512-crypt password hash, when password login is on
    password_login: bool = True
    accounts: tuple[Account, ...] = ()  # shared idle environments: the people who joined it (keys only)


@dataclass(frozen=True)
class Resources:
    shm_bytes: int
    cpus: float = 0.0  # devenv_cpus: NanoCpus of GPU environments; 0 = none
    memory_gb: float = 0.0  # devenv_memory_gb; 0 = the node's default (harden)
    lane_cores: tuple[int, ...] | None = None  # the lane's reserved cores (cpuset); None: no layout
    pool: tuple[int, ...] = ()  # cores of CPU-only environments
    cpu_core_limit: float | None = None  # NanoCpus of a CPU-only environment, in cores
    cpu_memory_gb: float = 0.0  # devenv_cpu_memory_gb; 0 = the node's default (harden)
    pool_shares: int = 512
    pids: int | None = None  # devenv_pids_limit; None = the node's default


@dataclass(frozen=True)
class SessionContext:
    session_id: int
    kind: str  # booking | shared | cpu
    image: str
    ssh: SshInputs
    resources: Resources
    node_policy: NodePolicy
    web: dict  # {token, ports: [{name, port, kind, strip, mode?}], base_session}
    user: UserInfo | None = None
    template: TemplateInfo | None = None
    project: ProjectInfo | None = None
    lane: LaneInfo | None = None
    end_at: str = ""  # ISO time the booking ends ("" without one)
    storage: Any = None  # services.storage.StoragePlan of the container (managed storages)
    storage_dir: str = ""  # legacy devenv_storage_dir (bind mounts), without the trailing slash
    models_source: str | None = None  # host path of the models directory, mounted read-only at /models
    in_container: bool = False  # SlotServe itself runs in a container (joins the environment's network)
