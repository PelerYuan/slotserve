"""What has to leave a container before it becomes an image (saved as a template, or a kept environment's snapshot):
the secrets SlotServe copied in and the values it put into the container's environment (R1-11).

Docker commits everything in the container's layer and its ``Config.Env``. Before a commit the dev environment
manager overwrites the files below with harmless contents (Docker's archive API cannot delete, but it can replace;
it works on stopped containers too), locks root's password in ``/etc/shadow`` (set from the person's environment
password at every start), and commits with the image's own environment: every variable SlotServe added is emptied
(Docker's commit merges the container's variables back in by name, so a name cannot be dropped, only its value).
A running container gets its files back right after the commit.

Pure functions, no I/O.
"""

import io
import re
import tarfile

from .contributors.platform_env import KEY_FILE

# path in the container -> (contents in the image, mode)
SECRET_FILES: dict[str, tuple[bytes, int]] = {
    KEY_FILE: (b"", 0o600),  # the environment key (F11)
    "opt/slotserve/ssh_host_key": (b"", 0o600),  # the environment's own SSH host key (R1-07)
    "opt/slotserve/root.hash": (b"*", 0o600),  # the person's password hash (normally removed at start)
}
SHADOW = "/etc/shadow"
# Variables SlotServe sets (session, platform, web, CPU-only); emptied when the image itself has no such variable
# and its environment is unknown.
OWN_ENV = re.compile(r"^(SLOTSERVE_\w*|OPENAI_API_KEY|OPENAI_BASE_URL|JUPYTER_TOKEN|HASHED_PASSWORD)$")


def committed_env(container_env: list[str], image_env: list[str] | None) -> list[str]:
    """The ``Env`` to commit with: the image's own variables with their own values, every other variable of the
    container emptied. Without the image's environment (``None``: the image is gone) only SlotServe's own
    variables are emptied and the rest is kept."""
    out: dict[str, str] = {}
    if image_env is not None:
        for entry in image_env:
            name = entry.split("=", 1)[0]
            out.setdefault(name, entry)
    for entry in container_env:
        name = entry.split("=", 1)[0]
        if name in out:
            continue
        if image_env is not None or OWN_ENV.match(name):
            out[name] = f"{name}="
        else:
            out[name] = entry
    return list(out.values())


def lock_root(archive: bytes) -> tuple[bytes, bool]:
    """A tar archive of ``/etc/shadow`` (as Docker's archive API returns it) with root's password field set to
    ``*``, ready to put back at ``/`` (owner and mode kept). Returns (archive, changed)."""
    found = _first_file(archive)
    if found is None:
        return archive, False
    member, data = found
    lines = data.split(b"\n")
    changed = False
    for i, line in enumerate(lines):
        fields = line.split(b":")
        if fields[0] == b"root" and len(fields) > 1 and fields[1] not in (b"*", b"!", b"!!", b"x", b"!*"):
            fields[1] = b"*"
            lines[i] = b":".join(fields)
            changed = True
    return shadow_archive(member, b"\n".join(lines)), changed


def shadow_archive(member: tarfile.TarInfo, data: bytes) -> bytes:
    """``data`` as ``etc/shadow`` in a tar archive, with the owner and mode of ``member``."""
    info = tarfile.TarInfo(SHADOW.lstrip("/"))
    info.size = len(data)
    info.mode, info.uid, info.gid, info.mtime = member.mode, member.uid, member.gid, member.mtime
    info.uname, info.gname = member.uname, member.gname
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def original_shadow(archive: bytes) -> bytes | None:
    """The unchanged ``/etc/shadow`` of an archive, to put back after the commit (None: none in it)."""
    found = _first_file(archive)
    return shadow_archive(*found) if found is not None else None


def _first_file(archive: bytes) -> tuple[tarfile.TarInfo, bytes] | None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as src:
        member = next((m for m in src.getmembers() if m.isfile()), None)
        if member is None:
            return None
        return member, src.extractfile(member).read()
