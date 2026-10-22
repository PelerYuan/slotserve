"""The audit log: who changed what, with before/after for settings (secrets only as fingerprints).

Actions are ``<domain>.<verb>``; ``AUDIT_ACTIONS`` lists them (the admin UI filters by them and labels each one in
both languages). The client address is kept in the entry as ``ip`` (``detail["_ip"]`` in the table).
"""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from ..orm import AuditLog, User

IP_FIELD = "_ip"

AUDIT_ACTIONS = frozenset(
    {
        "auth.first_admin",
        "auth.login",
        "auth.login_failed",
        "backup.create",
        "backup.download",
        "catalog.delete",
        "catalog.deploy",
        "catalog.hide",
        "catalog.save",
        "catalog.show",
        "channels.announce",
        "devenv.cpu_stop",
        "devenv.port_direct",
        "devenv.port_reclaim",
        "devenv.project_delete",
        "devenv.restart",
        "devenv.template_create",
        "devenv.template_delete",
        "devenv.template_update",
        "key.create",
        "key.revoke",
        "lane.create",
        "lane.delete",
        "lane.update",
        "model.args_request",
        "model.args_update",
        "model.clear_default",
        "model.delete",
        "model.set_default",
        "reservation.approve",
        "reservation.cancel",
        "reservation.create_for",
        "reservation.reject",
        "reservation.release",
        "reservation.update",
        "runtime.auto",
        "runtime.load",
        "runtime.restart",
        "runtime.unload",
        "settings.sensitive_update",
        "settings.undo",
        "settings.update",
        "ssh_key.add",
        "ssh_key.remove",
        "storage.create",
        "storage.delete",
        "storage.update",
        "user.create",
        "user.delete",
        "user.disable",
        "user.enable",
        "user.grant_admin",
        "user.quota_grant",
        "user.quota_revoke",
        "user.reset_password",
        "user.revoke_admin",
    }
)


def client_ip(request: Request | None) -> str:
    return request.client.host if request is not None and request.client is not None else ""


def record(
    db: AsyncSession,
    actor: User | None,
    action: str,
    target: str = "",
    *,
    ip: str | Request | None = None,
    **detail: Any,
) -> AuditLog:
    """Adds an audit entry to the session; it is written with the caller's commit. ``ip`` takes the address or
    the request it came with."""
    address = client_ip(ip) if isinstance(ip, Request) else (ip or "")
    if address:
        detail[IP_FIELD] = address
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_name=actor.username if actor else "",
        action=action,
        target=target[:160],
        detail=json.dumps(detail, default=str, ensure_ascii=False),
    )
    db.add(entry)
    return entry


def split_detail(raw: str | None) -> tuple[dict, str]:
    """``(detail without the address, address)`` of a stored entry."""
    try:
        detail = json.loads(raw or "{}")
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {"value": detail}
    return detail, str(detail.pop(IP_FIELD, "") or "")
