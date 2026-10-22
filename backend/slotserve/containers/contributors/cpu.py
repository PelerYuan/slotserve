"""CPU placement and limits (devenv_cpu): a lane's containers get the lane's reserved cores; a CPU-only environment
gets the shared pool with a lower CPU weight, a core limit and its own memory limit, and no GPU at all."""

from ..context import SessionContext
from ..model import ContainerSpec, GiB


def cpu_ranges(cores) -> str:
    """``[0, 1, 2, 5, 7, 8]`` -> ``"0-2,5,7-8"`` (Docker's cpuset format)."""
    out: list[str] = []
    for c in sorted(set(cores)):
        if out and c == int(out[-1].split("-")[-1]) + 1:
            first = out[-1].split("-")[0]
            out[-1] = f"{first}-{c}"
        else:
            out.append(str(c))
    return ",".join(out)


class CpuContributor:
    name = "cpu"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        r = ctx.resources
        if ctx.kind != "cpu":
            if r.cpus > 0:
                spec.cpu["NanoCpus"] = int(r.cpus * 1e9)
            if r.memory_gb > 0:
                spec.memory = int(r.memory_gb * GiB)
            if r.lane_cores:
                spec.cpu["CpusetCpus"] = cpu_ranges(r.lane_cores)
            return
        # images that set NVIDIA_VISIBLE_DEVICES=all would get every GPU where nvidia is the default runtime
        spec.devices = None
        spec.env["NVIDIA_VISIBLE_DEVICES"] = "void"
        spec.env["SLOTSERVE_GPU"] = "0"
        if r.pool:
            spec.cpu["CpusetCpus"] = cpu_ranges(r.pool)
        if r.cpu_core_limit is not None:
            spec.cpu["NanoCpus"] = int(r.cpu_core_limit * 1e9)
        if r.cpu_memory_gb > 0:
            spec.memory = int(r.cpu_memory_gb * GiB)
        spec.cpu["CpuShares"] = r.pool_shares
