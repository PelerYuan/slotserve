"""SSH: the static Dropbear SlotServe copies in, the environment's own host key and the logins.

A person's own environment: root logs in with the person's keys, and with their environment password when admins
allow password login. Credentials are optional for templates with a web interface (F03): without any, root has no
password ("*") and no keys, so SSH simply lets nobody in; a template without a web interface still needs a way in.
A lane's shared idle environment: one account per person who joined it (never root), with keys only - no password
hashes are ever copied into it (R1-10).
"""

import hashlib

from ..context import Account, SessionContext
from ..model import ContainerSpec, SpecError
from ..names import MAX_AUTH_TRIES
from ..scripts import ACCOUNTS_SCRIPT, LIB_SCRIPT, PROFILE, ROOT_SETUP, SHARED_SETUP, SSH_SCRIPT, START_SCRIPT


def booking_ssh_files(bundle, keys: list[str], secret: str | None) -> dict[str, tuple[bytes, int]]:
    """Files that give a booking container its SSH server: root logs in with the person's keys, and with their
    password (``secret``, a SHA-512 crypt hash) when one is set and password login is allowed. ``bundle`` has the
    Dropbear ``binary`` and the environment's ``host_key``."""
    ssh = SSH_SCRIPT.format(setup=ROOT_SETUP, flags="" if secret else "-s", tries=MAX_AUTH_TRIES, key_dir="-D $S/ssh ")
    return {
        "opt/slotserve/dropbearmulti": (bundle.binary, 0o755),
        "opt/slotserve/ssh_host_key": (bundle.host_key, 0o600),
        "opt/slotserve/lib.sh": (LIB_SCRIPT.encode(), 0o644),
        "opt/slotserve/ssh/authorized_keys": ("".join(k + "\n" for k in keys).encode(), 0o600),
        "opt/slotserve/root.hash": ((secret or "*").encode(), 0o600),
        "etc/profile.d/slotserve.sh": (PROFILE.encode(), 0o644),
        "opt/slotserve/ssh.sh": (ssh.encode(), 0o755),
        "opt/slotserve/start.sh": (START_SCRIPT.format(cd="cd /workspace 2>/dev/null || true").encode(), 0o755),
    }


def account_files(accounts: tuple[Account, ...] | list[Account]) -> tuple[dict[str, tuple[bytes, int]], str]:
    """The accounts of a shared environment (name:uid:hash lines and each person's keys) and their digest."""
    files: dict[str, tuple[bytes, int]] = {}
    lines = []
    for a in accounts:
        lines.append(f"{a.name}:{a.uid}:*")  # never a password hash: keys only
        files[f"opt/slotserve/keys/{a.name}"] = ("".join(k + "\n" for k in a.keys).encode(), 0o600)
    files["opt/slotserve/accounts"] = (("\n".join(lines) + "\n").encode(), 0o600)
    files["opt/slotserve/accounts.sh"] = (ACCOUNTS_SCRIPT.encode(), 0o700)
    files["opt/slotserve/lib.sh"] = (LIB_SCRIPT.encode(), 0o644)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode() + b"\0" + files[path][0] + b"\0")
    return files, digest.hexdigest()


def needs_no_login(web_ports) -> bool:
    """A template with a web interface can start without SSH credentials (it is used in the browser)."""
    return bool(web_ports)


class SshContributor:
    name = "ssh"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        ssh = ctx.ssh
        spec.files.update(
            {
                "opt/slotserve/dropbearmulti": (ssh.binary, 0o755),
                "opt/slotserve/ssh_host_key": (ssh.host_key, 0o600),
                "opt/slotserve/lib.sh": (LIB_SCRIPT.encode(), 0o644),
            }
        )
        if ctx.kind == "shared":
            files, spec.accounts_digest = account_files(ssh.accounts)
            spec.files.update(files)
            # everyone logs in to their own account (-w: not as root), with keys only (-s)
            script = SSH_SCRIPT.format(setup=SHARED_SETUP, flags="-w -s", tries=MAX_AUTH_TRIES, key_dir="")
            spec.files["opt/slotserve/ssh.sh"] = (script.encode(), 0o755)
            spec.files["opt/slotserve/start.sh"] = (START_SCRIPT.format(cd="").encode(), 0o755)
            return
        if not ssh.keys and not ssh.secret and not needs_no_login(ctx.web.get("ports")):
            raise SpecError("ssh_login_required" if ssh.password_login else "ssh_key_required")
        spec.files.update(booking_ssh_files(ssh, list(ssh.keys), ssh.secret))
