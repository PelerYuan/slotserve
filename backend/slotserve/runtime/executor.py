"""Where lanes run: the execution abstraction between scheduling decisions and the machine that carries them out.

A *node* is one machine with GPUs (or only CPUs). The control plane decides what each lane should run; a
``NodeExecutor`` carries that out on the lane's node: it creates the lane's model server runtime (start / stop,
health, loading progress, logs), reports GPU metrics, finds model files and gives access to the node's Docker API.

``LocalExecutor`` is the built-in node: everything happens in this process, exactly as on a single machine.
Remote nodes (``slotserve agent`` connected over a WebSocket) are implemented in ``slotserve.cluster``; the rest
of the code only talks to this interface, so single-machine deployments do not depend on any cluster code.

A lane runtime (``LaneRuntime``) is what the scheduler, the gateway and the admin views use: ``LlamaRuntime`` for
the local node, ``cluster.remote.RemoteRuntime`` for a node agent.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..config import Settings
from ..services.scanner import ModelFile
from . import gpu as gpu_module

if TYPE_CHECKING:
    from ..orm import LlmModel, ModelPreset
    from ..schemas import GpuOut
    from ..services.docker_api import DockerClient
    from .manager import RuntimeSnapshot, RuntimeState
    from .progress import LogProgress

LOCAL_NODE_NAME = "local"
__all__ = ["LaneRuntime", "LocalExecutor", "ModelFile", "NodeExecutor", "NodeRegistry"]


@runtime_checkable
class LaneRuntime(Protocol):
    """The model server of one lane, wherever it runs. ``LlamaRuntime`` is the reference implementation."""

    lane_id: int
    devices: list[int] | None
    state: RuntimeState
    model_id: int | None
    model_name: str | None
    preset_id: int | None
    preset_name: str | None
    engine: str | None
    command: str | None
    pid: int | None
    exit_code: int | None
    failure_log: list[str]
    inflight: int
    draining: bool
    _failed_at: datetime | None

    def snapshot(self) -> RuntimeSnapshot: ...
    def begin_request(self) -> None: ...
    def end_request(self) -> None: ...
    @property
    def port(self) -> int: ...
    @property
    def base_url(self) -> str: ...
    @property
    def spec(self) -> tuple[int, int | None] | None: ...
    @property
    def fail_count(self) -> int: ...
    @property
    def next_retry_at(self) -> datetime | None: ...
    @property
    def log_name(self) -> str: ...
    async def ensure(
        self,
        model: LlmModel | None,
        est_load_seconds: float = 0.0,
        force: bool = False,
        preset: ModelPreset | None = None,
    ) -> None: ...
    async def shutdown(self) -> None: ...
    def log_progress(self) -> LogProgress: ...
    async def log_tail(self, lines: int = 200) -> list[str]: ...
    async def log_size(self) -> int: ...
    async def log_read(self, start: int, end: int) -> bytes: ...


class NodeExecutor(ABC):
    """Carries out the control plane's decisions on one node."""

    node_id: int | None = None
    name: str = LOCAL_NODE_NAME
    is_local: bool = False

    @property
    @abstractmethod
    def online(self) -> bool:
        """Whether commands can reach the node right now."""

    @property
    @abstractmethod
    def address(self) -> str:
        """Host name or IP the control plane uses for the data plane (model servers, SSH, container ports)."""

    @abstractmethod
    def create_runtime(self, lane_id: int, devices: list[int] | None) -> LaneRuntime:
        """The model server runtime of a lane on this node."""

    @abstractmethod
    async def gpu_stats(self) -> list[GpuOut]:
        """Live GPU metrics of the node (empty without GPUs or metrics)."""

    @abstractmethod
    async def scan_models(self) -> list[ModelFile]:
        """GGUF models in the node's models directory."""

    def docker(self) -> DockerClient:
        """The node's Docker API (dev environments)."""
        raise NotImplementedError(f"node {self.name!r} has no Docker access")

    def describe(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "name": self.name, "local": self.is_local, "online": self.online}


class LocalExecutor(NodeExecutor):
    """The machine the control plane runs on: subprocesses, local nvidia-smi, local files and Docker socket."""

    is_local = True

    def __init__(self, settings: Settings, node_id: int | None = None, name: str = LOCAL_NODE_NAME):
        self.settings = settings
        self.node_id = node_id
        self.name = name
        # Set by the app: the dev environment manager owns (and may replace) the Docker client.
        self.docker_provider: Callable[[], DockerClient] | None = None

    @property
    def online(self) -> bool:
        return True

    @property
    def address(self) -> str:
        return self.settings.llama_host

    def create_runtime(self, lane_id: int, devices: list[int] | None) -> LaneRuntime:
        from .manager import LlamaRuntime

        return LlamaRuntime(self.settings, lane_id=lane_id, devices=devices)

    async def gpu_stats(self) -> list[GpuOut]:
        return await gpu_module.gpu_stats()  # looked up at call time so tests can replace it

    async def scan_models(self) -> list[ModelFile]:
        from ..services import scanner

        return await asyncio.to_thread(scanner.find_files, Path(self.settings.models_dir))

    def docker(self) -> DockerClient:
        if self.docker_provider is None:
            from ..services.docker_api import DockerClient

            return DockerClient(self.settings.docker_host)
        return self.docker_provider()


class NodeRegistry:
    """Every node executor by node id; ``None`` (and the local node's id) mean the built-in local node."""

    def __init__(self, local: NodeExecutor):
        self.local = local
        self._remote: dict[int, NodeExecutor] = {}

    def get(self, node_id: int | None) -> NodeExecutor:
        if node_id is None or node_id == self.local.node_id:
            return self.local
        found = self._remote.get(node_id)
        if found is None:
            raise KeyError(node_id)
        return found

    def find(self, node_id: int | None) -> NodeExecutor | None:
        try:
            return self.get(node_id)
        except KeyError:
            return None

    def add(self, executor: NodeExecutor) -> None:
        assert executor.node_id is not None
        self._remote[executor.node_id] = executor

    def remove(self, node_id: int) -> NodeExecutor | None:
        return self._remote.pop(node_id, None)

    def all(self) -> list[NodeExecutor]:
        return [self.local, *self._remote.values()]

    def is_local(self, node_id: int | None) -> bool:
        return node_id is None or node_id == self.local.node_id
