"""Names and small pure helpers shared by the contributors and the dev environment manager."""

import hashlib
import json
import re

LABEL = "slotserve.devenv"
UID_BASE = 20000  # Unix uid of a SlotServe user in shared environments: UID_BASE + user id
MAX_AUTH_TRIES = 3
WEB_KINDS = ("desktop", "jupyter", "vscode", "http")


def network_name(session_id: int) -> str:
    return f"slotserve-devenv-{session_id}"


def project_network_name(project_id: int) -> str:
    """A kept environment's container keeps its network between bookings (until the project is deleted)."""
    return f"slotserve-devenv-p{project_id}"


def path_slug(path: str) -> str:
    """A kept directory as one name: ``/root/.cache`` -> ``root-.cache``."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", path.strip("/").replace("/", "-"))[:60] or "root"


def web_base(session_id: int, port: int) -> str:
    """The path one of a dev environment's container ports is served under."""
    return f"/devenv/{session_id}/port/{port}/"


def load_web_ports(raw: str | None) -> list[dict]:
    """A template's declared web interfaces: [{name, port, kind, mode?}] (invalid entries are skipped)."""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        try:
            port = int(item["port"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= port <= 65535 or port in seen:
            continue
        seen.add(port)
        kind = item.get("kind") if item.get("kind") in WEB_KINDS else "http"
        entry = {"name": str(item.get("name") or port)[:32], "port": port, "kind": kind}
        if item.get("mode") == "direct":  # direct ports: published on the host (services/devenv_ports.py)
            entry["mode"] = "direct"
        out.append(entry)
    return out


def placeholder_values(session_id: int, ports: list[dict], token: str) -> dict[str, str]:
    """``{base_path}``/``{port}`` are the first declared port's, ``{base_path_<port>}`` any declared port's."""
    values = {
        "session": str(session_id),
        "token": token,
        # code-server's HASHED_PASSWORD (its legacy SHA-256 form): the proxy signs people in with it (web.py)
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "base_path": "",
        "port": "",
    }
    for i, p in enumerate(ports):
        values[f"base_path_{p['port']}"] = web_base(session_id, p["port"])
        if i == 0:
            values.update(base_path=web_base(session_id, p["port"]), port=str(p["port"]))
    return values


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Replaces ``{base_path}``, ``{token}``, ``{token_sha256}``, ``{port}`` and ``{session}``; other braces are left
    alone."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
