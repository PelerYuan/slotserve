"""Web interfaces and the template's own start: declared ports, their token and base path, command, environment.

Every start gets a random token. JupyterLab and the desktop check it themselves and the proxy adds it to the requests
it trusts; VS Code (code-server) runs with ``--auth password`` and ``HASHED_PASSWORD={token_sha256}``, and the proxy
adds code-server's session cookie (``code_server_cookie``), so people never see its password page while nobody
reaching the port without SlotServe gets in (R1-08).
"""

import contextlib
import json

from ..context import SessionContext
from ..model import ContainerSpec
from ..names import fill_placeholders, placeholder_values

CODE_SERVER_COOKIE = "code-server-session"


def code_server_cookie(token: str) -> tuple[str, str]:
    """The session cookie code-server accepts when it runs with HASHED_PASSWORD = sha256(token) (its SHA-256
    password method compares the cookie with the hash)."""
    return CODE_SERVER_COOKIE, placeholder_values(0, [], token)["token_sha256"]


class WebContributor:
    name = "web"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        web = ctx.web
        spec.web = web
        template = ctx.template
        values = placeholder_values(ctx.session_id, web["ports"], web["token"])
        if web["ports"]:
            spec.env.update(
                SLOTSERVE_WEB_BASE=values["base_path"],
                SLOTSERVE_WEB_PORT=values["port"],
                SLOTSERVE_WEB_TOKEN=web["token"],
            )
            if any(p["kind"] == "jupyter" for p in web["ports"]):
                spec.env["JUPYTER_TOKEN"] = web["token"]
        if template is not None and template.env:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                extra = json.loads(template.env).items()
                spec.env.update({str(k): fill_placeholders(str(v), values) for k, v in extra})
        if template is not None and template.command:
            spec.cmd = ["/bin/sh", "-c", fill_placeholders(template.command, values)]
        for p in web["ports"]:
            spec.exposed.append(p["port"])
            if not ctx.in_container:
                # only SlotServe (on this host) reaches it; Docker picks a free port
                spec.port_bindings[f"{p['port']}/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": ""}]
