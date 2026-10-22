"""SSH of dev environments: public keys and the files that give a booking container its SSH server."""

import base64
import hashlib
from dataclasses import dataclass

from .constants import KEY_TYPES, MAX_AUTH_TRIES
from .scripts import LIB_SCRIPT, PROFILE, ROOT_SETUP, SSH_SCRIPT, START_SCRIPT


def parse_public_key(text: str) -> tuple[str, str]:
    """Validates an OpenSSH public key line; returns (normalized key, SHA256 fingerprint)."""
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise ValueError("not an OpenSSH public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError("not an OpenSSH public key") from exc
    if len(blob) < 4 or blob[4 : 4 + int.from_bytes(blob[:4], "big")].decode(errors="replace") != parts[0]:
        raise ValueError("not an OpenSSH public key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return " ".join(parts[:3]), fingerprint


def booking_ssh_files(bundle: "SshBundle", keys: list[str], secret: str | None) -> dict[str, tuple[bytes, int]]:
    """Files that give a booking container its SSH server: root logs in with the person's keys, and with their
    password (``secret``, a SHA-512 crypt hash) when one is set and password login is allowed."""
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


@dataclass
class SshBundle:
    binary: bytes
    host_key: bytes
    fingerprint: str
