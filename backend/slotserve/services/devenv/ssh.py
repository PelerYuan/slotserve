"""SSH of dev environments: public keys, the Dropbear binary and every environment's own host key.

Each environment has its own ed25519 host key (R1-07, F10): ``data/devenv/hostkeys/<env>`` where ``<env>`` is
``p<project id>`` for a kept environment (the same key across pauses, GPU changes and snapshots), ``l<lane id>`` for
a lane's shared idle environment and ``s<session id>`` for any other one (removed when the session ends). The keys
are in Dropbear's own file format; ``host_key_fingerprint`` gives the fingerprint ``ssh`` shows.
"""

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ...containers.contributors.ssh import booking_ssh_files as booking_ssh_files  # used by CI
from .constants import KEY_TYPES

ED25519 = b"ssh-ed25519"


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
    return " ".join(parts[:3]), _fingerprint(blob)


def _fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _string(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + data


def generate_host_key() -> bytes:
    """A new ed25519 host key in Dropbear's file format: string "ssh-ed25519", then a string of the 32-byte seed
    followed by the 32-byte public key."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _string(ED25519) + _string(seed + public)


def host_public_key(key: bytes) -> str | None:
    """The OpenSSH public key line (``ssh-ed25519 AAAA...``) of a Dropbear ed25519 host key file, or None."""
    try:
        n = int.from_bytes(key[:4], "big")
        if key[4 : 4 + n] != ED25519:
            return None
        rest = key[4 + n :]
        m = int.from_bytes(rest[:4], "big")
        if m != 64 or len(rest) < 4 + m:
            return None
        public = rest[4 + 32 : 4 + 64]
    except (IndexError, ValueError):
        return None
    return "ssh-ed25519 " + base64.b64encode(_string(ED25519) + _string(public)).decode()


def host_key_fingerprint(key: bytes) -> str:
    """``SHA256:...`` as ``ssh`` shows it for the host key; "" for a key that is not ed25519."""
    line = host_public_key(key)
    return _fingerprint(base64.b64decode(line.split()[1])) if line else ""


@dataclass
class SshBundle:
    binary: bytes
    host_key: bytes = b""  # no longer shared: every environment has its own (DevEnvManager.host_key)
    fingerprint: str = ""
