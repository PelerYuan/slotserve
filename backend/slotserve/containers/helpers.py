"""Specs of the short-lived containers SlotServe runs for itself: storage helpers (create, check, measure and remove
folders) and direct-port forwarders. They go through harden() like every other container (arch3-2 K2-1, R1-06):

* a helper always runs SlotServe's own helper image (``storage_helper_image``, busybox by default), never the image
  of a person's environment - a person's image could replace ``/bin/sh`` and reach everyone's folders;
* a helper mounts as little as the storage allows: for a host directory (and the legacy storage directory) only the
  parent folders of what it prepares; an NFS/SMB storage can only be mounted from its root (a sub-folder that does
  not exist yet cannot be mounted), which is safe because the helper runs only SlotServe's image and script;
* a new folder is seeded from the environment's image without running that image: a container of it is created (never
  started) and the directory is copied out with Docker's archive API into the helper, which copies it into place.
"""

import posixpath
import re
from dataclasses import dataclass

from .build import render_for_docker
from .model import ContainerSpec, GiB, NodePolicy

HELPER_LABEL = "helper"
SEED_LABEL = "seed"
HELPER_MEMORY = 1 * GiB
HELPER_PIDS = 512
NEW_MARK = "==NEW=="
SEED_DIR = "/tmp/slotserve-seed"
# Helpers set their own limits, so they need nothing from the machine.
HELPER_POLICY = NodePolicy()


@dataclass(frozen=True)
class Folder:
    """A folder a helper creates when it is missing (``path`` as the helper sees it)."""

    path: str
    seed: str | None = None  # copy this directory of the environment's image into a new folder
    owner: int | None = None  # chown a new folder to this uid and make it private (shared environments)


def q(text: str) -> str:
    """Single-quoted for /bin/sh."""
    return "'" + text.replace("'", "'''") + "'"


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


def seed_source_spec(image: str, *, label_key: str) -> ContainerSpec:
    """A container of the environment's image that is only created, never started: Docker's archive API reads the
    directories to seed from it. No mounts, no network, an unprivileged user."""
    return ContainerSpec(
        name="",
        image=image,
        kind="helper",
        labels={label_key: SEED_LABEL},
        entrypoint=["/bin/true"],
        cmd=[],
        user="65534:65534",
        network="none",
        memory=64 * 1024**2,
        pids=16,
    )


def helper_body(spec: ContainerSpec) -> dict:
    return render_for_docker(spec, HELPER_POLICY)


def parent_binds(root: str, rels: list[str], target: str) -> list[str]:
    """Binds of the parent folders of ``rels`` under a host directory (Docker creates missing ones): the helper
    sees ``<target>/<rel>`` without the rest of the storage."""
    root = root.rstrip("/")
    out: list[str] = []
    for rel in rels:
        parent = posixpath.dirname(rel.strip("/"))
        source = f"{root}/{parent}" if parent else root
        bind = f"{source}:{target}/{parent}" if parent else f"{source}:{target}"
        if bind not in out:
            out.append(bind)
    return out


def prepare_script(folders: list[Folder]) -> str:
    """Creates the missing folders and prints which ones are new (``==NEW== <index>``)."""
    lines = ["set -e"]
    for i, f in enumerate(folders):
        d = q(f.path)
        lines.append(f'if [ ! -d {d} ]; then mkdir -p {d}; echo "{NEW_MARK} {i}"; fi')
        if f.owner is not None:
            # the folder (not its content) belongs to the person's account in shared environments
            lines.append(f"chown {f.owner}:{f.owner} {d} && chmod 700 {d} || true")
    return "\n".join(lines)


def new_folders(output: str) -> set[int]:
    return {int(m) for m in re.findall(rf"^{NEW_MARK} (\d+)\s*$", output, re.M)}


def seed_path(i: int) -> str:
    return f"{SEED_DIR}/{i}"


def seed_script(folders: list[tuple[int, Folder]]) -> str:
    """Copies what was taken out of the image (``<SEED_DIR>/<i>/<name>``) into the new folders."""
    lines = []
    for i, f in folders:
        name = posixpath.basename((f.seed or "").rstrip("/")) or "."
        src = q(f"{seed_path(i)}/{name}")
        lines.append(f"if [ -d {src} ]; then cp -a {q(f'{seed_path(i)}/{name}/.')} {q(f.path)}/ || true; fi")
    return "\n".join(lines) or "true"
