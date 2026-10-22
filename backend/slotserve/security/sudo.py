"""Sudo mode: dangerous actions need the password to have been entered within the last few minutes.

Signing in or ``POST /api/auth/sudo`` (password again) records the time in the session; ``Depends(sudo_user)``
/ ``Depends(sudo_admin)`` (``deps.py``) refuse with 403 ``sudo_required`` once ``WINDOW_SECONDS`` have passed.
The frontend answers that error by asking for the password and repeating the request.
"""

import time

from starlette.requests import Request

WINDOW_SECONDS = 5 * 60
SESSION_KEY = "sudo"


def _now() -> float:
    return time.time()


def grant(request: Request, user_id: int) -> float:
    """Record a fresh password check for ``user_id``; returns when sudo mode ends."""
    at = _now()
    request.session[SESSION_KEY] = {"uid": user_id, "at": at}
    return at + WINDOW_SECONDS


def expires_at(request: Request, user_id: int) -> float | None:
    """When sudo mode of ``user_id`` ends, or None when it is not active."""
    entry = request.session.get(SESSION_KEY)
    if not isinstance(entry, dict) or entry.get("uid") != user_id:
        return None
    at = entry.get("at")
    if not isinstance(at, (int, float)):
        return None
    now = _now()
    if at > now + 5 or now - at > WINDOW_SECONDS:  # a clock that jumped back counts as expired as well
        return None
    return at + WINDOW_SECONDS


def is_active(request: Request, user_id: int) -> bool:
    return expires_at(request, user_id) is not None


def revoke(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)
