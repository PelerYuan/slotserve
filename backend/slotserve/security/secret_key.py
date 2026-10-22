"""The key that signs session cookies and encrypts stored secrets.

An explicitly configured ``SLOTSERVE_SECRET_KEY`` always wins. When it is unset (still the old public default
``change-me``) or empty, SlotServe uses a random key kept in ``<data dir>/secret_key`` (created on first start,
readable by the owner only), so no installation runs with a key everyone knows.
"""

import logging
import os
import secrets
from pathlib import Path

from .primitives import LEGACY_DEFAULT_KEY

log = logging.getLogger(__name__)

FILE_NAME = "secret_key"
INSECURE_KEYS = frozenset({"", LEGACY_DEFAULT_KEY})
MIN_RECOMMENDED_LENGTH = 16


def is_insecure(key: str | None) -> bool:
    return key is None or key.strip() in INSECURE_KEYS


def key_file(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def write_private(path: Path, text: str) -> None:
    """Create ``path`` readable and writable by the owner only (0600 on POSIX); fails if it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)  # in case the umask or the file system widened it


def load_or_create(data_dir: Path) -> str:
    path = key_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = ""
    if key:
        return key
    key = secrets.token_urlsafe(48)
    try:
        path.unlink(missing_ok=True)  # an empty file left by an interrupted start
        write_private(path, key + "\n")
    except FileExistsError:  # another process created it at the same moment
        return path.read_text(encoding="utf-8").strip()
    log.info("generated a new secret key in %s", path)
    return key


def resolve(settings) -> str:
    """Make ``settings.secret_key`` safe to use and return it (call before anything signs or encrypts)."""
    if not is_insecure(settings.secret_key):
        if len(settings.secret_key.strip()) < MIN_RECOMMENDED_LENGTH:
            log.warning(
                "SLOTSERVE_SECRET_KEY is shorter than %s characters; use a long random value "
                "(or remove it to let SlotServe generate one)",
                MIN_RECOMMENDED_LENGTH,
            )
        return settings.secret_key
    settings.secret_key = load_or_create(settings.data_dir)
    return settings.secret_key
