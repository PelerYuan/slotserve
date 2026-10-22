"""GPUs: a lane's devices; none for CPU-only environments."""

from ..context import SessionContext
from ..model import ContainerSpec


class GpuContributor:
    name = "gpu"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        if ctx.kind == "cpu":
            spec.devices = None
            return
        devices = ctx.lane.devices if ctx.lane else None
        request: dict = {"Driver": "nvidia", "Capabilities": [["gpu"]]}
        if devices:
            request["DeviceIDs"] = [str(d) for d in devices]
        else:
            request["Count"] = -1
        spec.devices = [request]
