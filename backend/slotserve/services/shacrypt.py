"""SHA-512-crypt (``$6$...``), the password hash format of /etc/shadow.

Python 3.13 dropped the ``crypt`` module, so this follows Ulrich Drepper's published specification
("Unix crypt using SHA-256 and SHA-512"). Only hashing is needed: SlotServe writes the hash into a dev
environment and the SSH server there checks passwords.
"""

import hashlib
import secrets

ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ROUNDS_DEFAULT = 5000
ROUNDS_MIN = 1000
ROUNDS_MAX = 999_999_999
SALT_MAX = 16

# The order in which the 64 digest bytes are encoded, three at a time (the last byte on its own).
_ORDER = (
    (0, 21, 42),
    (22, 43, 1),
    (44, 2, 23),
    (3, 24, 45),
    (25, 46, 4),
    (47, 5, 26),
    (6, 27, 48),
    (28, 49, 7),
    (50, 8, 29),
    (9, 30, 51),
    (31, 52, 10),
    (53, 11, 32),
    (12, 33, 54),
    (34, 55, 13),
    (56, 14, 35),
    (15, 36, 57),
    (37, 58, 16),
    (59, 17, 38),
    (18, 39, 60),
    (40, 61, 19),
    (62, 20, 41),
)


def _b64(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(out)


def _repeat(digest: bytes, length: int) -> bytes:
    return digest * (length // len(digest)) + digest[: length % len(digest)]


def sha512_crypt(password: str | bytes, salt: str | None = None, rounds: int | None = None) -> str:
    """Hashes ``password``; ``salt`` may be a bare salt or a whole ``$6$[rounds=N$]salt[$...]`` setting."""
    key = password.encode() if isinstance(password, str) else password
    if salt is None:
        salt = "".join(secrets.choice(ALPHABET) for _ in range(SALT_MAX))
    if salt.startswith("$6$"):
        salt = salt[3:]
        if salt.startswith("rounds="):
            spec, _, salt = salt.partition("$")
            rounds = int(spec[len("rounds=") :])
        salt = salt.split("$", 1)[0]
    custom_rounds = rounds is not None
    rounds = max(ROUNDS_MIN, min(ROUNDS_MAX, rounds)) if rounds is not None else ROUNDS_DEFAULT
    salt_bytes = salt.encode()[:SALT_MAX]

    b = hashlib.sha512(key + salt_bytes + key).digest()
    a = hashlib.sha512(key + salt_bytes)
    a.update(_repeat(b, len(key)))
    n = len(key)
    while n:
        a.update(b if n & 1 else key)
        n >>= 1
    a_digest = a.digest()

    dp = _repeat(hashlib.sha512(key * len(key)).digest(), len(key))
    ds = _repeat(hashlib.sha512(salt_bytes * (16 + a_digest[0])).digest(), len(salt_bytes))

    c = a_digest
    for i in range(rounds):
        h = hashlib.sha512(dp if i & 1 else c)
        if i % 3:
            h.update(ds)
        if i % 7:
            h.update(dp)
        h.update(c if i & 1 else dp)
        c = h.digest()

    encoded = "".join(_b64((c[x] << 16) | (c[y] << 8) | c[z], 4) for x, y, z in _ORDER) + _b64(c[63], 2)
    prefix = f"$6$rounds={rounds}$" if custom_rounds else "$6$"
    return f"{prefix}{salt_bytes.decode()}${encoded}"


def verify(password: str, hashed: str) -> bool:
    return secrets.compare_digest(sha512_crypt(password, hashed), hashed)


def is_sha512_crypt(value: str) -> bool:
    digest = value.rsplit("$", 1)[-1]
    return value.startswith("$6$") and len(digest) == 86 and all(ch in ALPHABET for ch in digest)
