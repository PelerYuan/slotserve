import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

API_KEY_PREFIX = "sk-ss-"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return hmac.compare_digest(digest.hex(), digest_hex)


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(24)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(b"slotserve api key v1:" + secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(key: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(key.encode()).decode()


# Before 0.10 an unset SLOTSERVE_SECRET_KEY meant this value; data encrypted with it stays readable after the
# key was replaced by a generated one (see secret_key.py).
LEGACY_DEFAULT_KEY = "change-me"


def decrypt_api_key(token: str, secret_key: str) -> str | None:
    """None when the copy cannot be read, e.g. after SECRET_KEY changed."""
    for key in dict.fromkeys((secret_key, LEGACY_DEFAULT_KEY)):
        try:
            return _fernet(key).decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            continue
    return None


def key_display_prefix(key: str) -> str:
    return key[: len(API_KEY_PREFIX) + 4]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
