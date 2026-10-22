"""Specs of the short-lived containers SlotServe runs for itself: storage helpers (create, check, measure and remove
folders) and direct-port forwarders. They go through harden() like every other container."""

from .build import render_for_docker
from .model import ContainerSpec, GiB, NodePolicy

HELPER_LABEL = "helper"
HELPER_MEMORY = 1 * GiB
HELPER_PIDS = 512
# Helpers set their own limits, so they need nothing from the machine.
HELPER_POLICY = NodePolicy()


def helper_spec(
    image: str,
    script: str,
    *,
    label_key: str,
    mounts: list[dict] | None = None,
    binds: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> ContainerSpec:
    """A helper that runs ``script`` with ``/bin/sh -c`` as root and exits."""
    return ContainerSpec(
        name="",
        image=image,
        kind="helper",
        env=dict(env or {}),
        mounts=list(mounts or []),
        binds=list(binds or []),
        labels={label_key: HELPER_LABEL},
        entrypoint=["/bin/sh", "-c", script],
        cmd=[],
        user="root",
        memory=HELPER_MEMORY,
        pids=HELPER_PIDS,
    )


def helper_body(spec: ContainerSpec) -> dict:
    return render_for_docker(spec, HELPER_POLICY)
