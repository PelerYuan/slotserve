"""The models directory, read-only at /models."""

from ..context import SessionContext
from ..model import ContainerSpec


class ModelsContributor:
    name = "models"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        if ctx.models_source:
            spec.mounts.append({"Type": "bind", "Source": ctx.models_source, "Target": "/models", "ReadOnly": True})
