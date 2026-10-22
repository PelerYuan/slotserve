"""Contributors: each adds one concern to a ContainerSpec (SSH, storage, web interfaces, GPUs, CPU, limits, ...).

A contributor reads the SessionContext and changes the spec; it does no I/O (no ``await``, no database, no Docker,
no HTTP: ``tests/unit/containers`` checks the imports). ``build.py`` holds the one ordered list of them.

Adding one (e.g. ``platform_env.py``, F11): a class with ``name`` and ``contribute(ctx, spec)``; read only ``ctx``
(add an input field to ``SessionContext`` and fill it in ``DevEnvManager._context`` when the contributor needs
something new); write ``spec.env`` (shown in ``docker inspect`` - never secrets), ``spec.files`` (copied in before
every start and resume: ``{"run/slotserve/llm-key": (data, 0o600)}``), ``spec.labels``, ``spec.mounts``. Branch on
``ctx.kind`` ("booking", "shared", "cpu"; ``ctx.project`` for kept environments). Then one line in
``build.CONTRIBUTORS`` (before ``LimitsContributor``) and a golden case; harden() runs after all of them.
"""

from typing import Protocol

from ..context import SessionContext
from ..model import ContainerSpec


class SpecContributor(Protocol):
    name: str

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None: ...
