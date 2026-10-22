"""GPUs: exactly the lane's devices (a lane without any cannot start a GPU environment); none for CPU-only
environments."""

from ..context import SessionContext
from ..model import ContainerSpec, SpecError


class GpuContributor:
    name = "gpu"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        if ctx.kind == "cpu":
            spec.devices = None
            return
        devices = ctx.lane.devices if ctx.lane else None
        request: dict = {"Driver": "nvidia", "Capabilities": [["gpu"]]}
        if not devices:
            # never "all GPUs" (Count -1): a lane that names none would hand out every GPU of the machine
            raise SpecError("devenv_lane_no_gpus", lane=ctx.lane.name if ctx.lane else "")
        request["DeviceIDs"] = [str(d) for d in devices]
        spec.devices = [request]
