"""Removes secrets from text that leaves the server: "copy diagnostics", ``slotserve doctor``, issue reports.

``redact(text, settings)`` replaces the values of every secret setting (chat platform credentials, webhook URLs,
tokens, the secret key, the invite code, ...) and anything that looks like a credential whatever its source:
``Authorization`` and cookie headers, SlotServe API keys, tokens in URLs and query strings, passwords in mount
options and URLs, ``KEY=value`` lines whose name says secret, private key blocks and password hashes.
``redact_mapping`` does the same for structured data (JSON before it is printed).

Pure functions, no I/O: everything that shows diagnostics goes through here (architecture A05).
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "[redacted]"

# Settings whose value is a secret even though the settings registry may not flag them (it flags the rest).
SECRET_SETTINGS = (
    "secret_key",
    "invite_code",
    "metrics_token",
    "hf_token",
    "telegram_bot_token",
    "feishu_app_secret",
    "dingtalk_client_secret",
    "wecom_secret",
    "wecom_token",
    "wecom_aes_key",
    "qq_app_secret",
    "feishu_webhook_url",
    "feishu_webhook_secret",
    "dingtalk_webhook_url",
    "dingtalk_webhook_secret",
    "wecom_webhook_url",
)
# Names of keys (settings, JSON fields, environment variables) whose values are secrets.
SECRET_NAME = re.compile(
    r"(secret|token|passw(or)?d|passwd|api[_-]?key|access[_-]?key|private[_-]?key|credential|cookie|"
    r"authorization|webhook|aes[_-]?key|invite[_-]?code|hash)",
    re.I,
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys (SSH host keys, TLS keys): the whole block
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
        MASK,
    ),
    # HTTP headers that carry credentials
    (re.compile(r"(?im)^(\s*(?:proxy-)?authorization\s*[:=]\s*).+$"), r"\1" + MASK),
    (re.compile(r"(?im)^(\s*(?:set-)?cookie\s*[:=]\s*).+$"), r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # SlotServe API keys and common provider key shapes
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}"), MASK),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), MASK),  # Telegram bot token
    # user:password@ in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),
    # webhook URLs whose path is the credential (Feishu, Slack-style hooks)
    (re.compile(r"(?i)(/hook/|/webhook/send\?key=|/hooks/)[A-Za-z0-9_-]{8,}"), r"\1" + MASK),
    # tokens in query strings and mount options: access_token=..., key=..., password=...
    (
        re.compile(
            r"(?i)([?&,;\s]|^)((?:access_)?token|key|secret|sign|signature|password|passwd|pass|pwd|"
            r"client_secret|app_secret|auth)=([^&,;\s\"']+)"
        ),
        r"\1\2=" + MASK,
    ),
    # KEY=value / "key": "value" whose name says secret (environment, .env, JSON, YAML)
    (
        re.compile(
            r"(?im)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|AES_KEY|INVITE_CODE)"
            r"[A-Z0-9_]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1=" + MASK,
    ),
    (
        re.compile(
            r"(?i)(\"[a-z0-9_]*(?:secret|token|password|passwd|api_key|private_key|aes_key|invite_code|cookie|"
            r"authorization)[a-z0-9_]*\"\s*:\s*)\"(?:[^\"\\]|\\.)*\""
        ),
        r'\1"' + MASK + '"',
    ),
    # crypt(3) password hashes ($6$salt$hash, $y$..., $2b$...) as in /etc/shadow or root.hash
    (re.compile(r"\$(?:1|2[aby]?|5|6|y|gy|7)\$[./A-Za-z0-9$]{8,}"), MASK),
]


def secret_values(settings: Any = None, extra: Iterable[str] = ()) -> list[str]:
    """The secret values in effect: every secret setting that is set, plus ``extra``; longest first."""
    values: set[str] = {v for v in extra if v}
    if settings is not None:
        fields = getattr(type(settings), "model_fields", None) or {}
        names = set(SECRET_SETTINGS) | {name for name in fields if SECRET_NAME.search(name)}
        for name in names:
            value = getattr(settings, name, None)
            if isinstance(value, str) and len(value.strip()) >= 4:
                values.add(value.strip())
    return sorted(values, key=len, reverse=True)


def redact(text: str, settings: Any = None, *, extra: Iterable[str] = ()) -> str:
    """``text`` with every known secret value and every credential-looking string replaced by ``[redacted]``."""
    if not text:
        return text
    for value in secret_values(settings, extra):
        text = text.replace(value, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_mapping(data: Any, settings: Any = None, *, extra: Iterable[str] = ()) -> Any:
    """A copy of JSON-like ``data``: values under secret-sounding keys masked, every string run through
    ``redact``."""
    values = secret_values(settings, extra)
    return _walk(data, values)


def _walk(data: Any, values: list[str]) -> Any:
    if isinstance(data, Mapping):
        out = {}
        for key, value in data.items():
            if isinstance(key, str) and SECRET_NAME.search(key) and value not in (None, "", False, 0):
                out[key] = MASK if not isinstance(value, bool | int | float) else value
            else:
                out[key] = _walk(value, values)
        return out
    if isinstance(data, list | tuple):
        return [_walk(v, values) for v in data]
    if isinstance(data, str):
        return redact(data, extra=values)
    return data
