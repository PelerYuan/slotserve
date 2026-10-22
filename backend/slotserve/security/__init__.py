"""Authentication and request-safety building blocks (A06).

The functions of the former ``slotserve/security.py`` live in ``primitives.py`` and are re-exported here, so
``from slotserve.security import hash_password`` keeps working.
"""

from .primitives import (
    API_KEY_PREFIX,
    constant_time_equals,
    decrypt_api_key,
    encrypt_api_key,
    generate_api_key,
    hash_api_key,
    hash_password,
    key_display_prefix,
    verify_password,
)

__all__ = [
    "API_KEY_PREFIX",
    "constant_time_equals",
    "decrypt_api_key",
    "encrypt_api_key",
    "generate_api_key",
    "hash_api_key",
    "hash_password",
    "key_display_prefix",
    "verify_password",
]
