"""Process and memory limits of an environment (R1-05): the configured ones, or the machine's defaults.

``devenv_memory_gb`` / ``devenv_cpu_memory_gb`` of 0 no longer mean "no limit" but the host's memory less a reserve
(``NodePolicy.default_memory``: max(4 GB, 10 %)), so an out-of-memory training run is killed inside its container
instead of taking SlotServe and everyone else with it. harden() enforces the same values once more at the end.
"""

from ..context import SessionContext
from ..model import ContainerSpec


class LimitsContributor:
    name = "limits"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        policy = ctx.node_policy
        if not spec.memory:
            spec.memory = policy.default_memory()
        elif policy.host_memory:
            spec.memory = min(spec.memory, policy.host_memory)
        pids = ctx.resources.pids or policy.pids_limit
        spec.pids = min(pids, policy.pids_limit)
