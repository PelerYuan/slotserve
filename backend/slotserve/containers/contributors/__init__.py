"""Contributors: each adds one concern to a ContainerSpec (SSH, storage, web interfaces, GPUs, CPU, limits, ...).

A contributor reads the SessionContext and changes the spec; it does no I/O (no ``await``, no database, no Docker,
no HTTP: ``tests/unit/containers`` checks the imports). ``build.py`` holds the one ordered list of them.
"""

from typing import Protocol

from ..context import SessionContext
from ..model import ContainerSpec


class SpecContributor(Protocol):
    name: str

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None: ...
