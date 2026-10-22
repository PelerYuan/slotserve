"""Redaction for previews and diagnostics: nothing secret leaves SlotServe in a spec summary (A01 acceptance 5).

Secrets are removed two ways: by name (environment variables and fields that look like tokens, passwords or keys) and
by value (every known secret string - the web token and its hash, password hashes, SMB passwords, host keys - is
replaced wherever it appears, e.g. inside a command line).
"""

import re
from collections.abc import Iterable

MASK = "***"
SECRET_NAME = re.compile(r"(TOKEN|PASSWORD|PASSWD|SECRET|KEY|HASH|CREDENTIAL)", re.I)


def _env_item(item: str, secrets: list[str]) -> str:
    name, sep, _value = item.partition("=")
    if sep and SECRET_NAME.search(name):
        return f"{name}={MASK}"
    return _text(item, secrets)


def _text(value: str, secrets: list[str]) -> str:
    for s in secrets:
        value = value.replace(s, MASK)
    return value


def redact(obj, secrets: Iterable[str | bytes | None] = (), *, _key: str = ""):
    """A copy of ``obj`` (dicts, lists, strings) with secrets masked. ``secrets``: known secret values."""
    known = sorted(
        {s.decode(errors="replace") if isinstance(s, bytes) else s for s in secrets if s and len(s) >= 4},
        key=len,
        reverse=True,
    )
    return _redact(obj, known, _key)


def _redact(obj, secrets: list[str], key: str):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and SECRET_NAME.search(str(k)):
                out[k] = MASK
            else:
                out[k] = _redact(v, secrets, str(k))
        return out
    if isinstance(obj, list | tuple):
        if key == "Env":
            return [_env_item(x, secrets) if isinstance(x, str) else _redact(x, secrets, "") for x in obj]
        return [_redact(x, secrets, key) for x in obj]
    if isinstance(obj, str):
        return _text(obj, secrets)
    if isinstance(obj, bytes):
        return MASK
    return obj
