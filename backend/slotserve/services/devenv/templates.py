"""Built-in templates, who sees a template, and the web interfaces a template declares."""

import json

from sqlalchemy import select

from ...containers.names import fill_placeholders as fill_placeholders
from ...containers.names import load_web_ports as load_web_ports
from ...containers.names import placeholder_values as placeholder_values
from ...containers.names import web_base as web_base
from ...orm import DevSession, DevTemplate, User

# The built-in templates (admins can change the image, disable them or add their own).
BUILTINS = (
    {
        "builtin_key": "pytorch",
        "name": "PyTorch",
        "image": "pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel",
        "description": "PyTorch 2.14, CUDA 12.6, cuDNN 9 and compilers. conda and pip work.",
    },
    {
        "builtin_key": "jupyter-pytorch",
        "name": "JupyterLab + PyTorch",
        "image": "quay.io/jupyter/pytorch-notebook:cuda12-pytorch-2.11.0",
        "description": "JupyterLab in the browser: Jupyter Docker Stacks with conda and PyTorch 2.11 (CUDA 12).",
        "command": (
            "jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --ServerApp.root_dir=/workspace"
            " --ServerApp.base_url={base_path} --IdentityProvider.token={token} --ServerApp.allow_remote_access=True"
        ),
        "web_ports": json.dumps([{"name": "JupyterLab", "port": 8888, "kind": "jupyter"}]),
    },
    {
        "builtin_key": "tensorflow",
        "name": "TensorFlow",
        "image": "tensorflow/tensorflow:2.21.0-gpu-jupyter",
        "description": "TensorFlow 2.21 with GPU support and Jupyter.",
    },
    {
        "builtin_key": "cuda",
        "name": "CUDA",
        "image": "nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04",
        "description": "Ubuntu 24.04 with the CUDA 12.9 toolkit and cuDNN. Install what you need with apt and pip.",
    },
    {
        "builtin_key": "desktop",
        "name": "Ubuntu desktop",
        "image": "linuxserver/webtop:ubuntu-xfce-388f14a2-ls7",
        "description": "An Ubuntu XFCE desktop in the browser (linuxserver webtop) with the lane's GPUs.",
        "env": json.dumps(
            {"SUBFOLDER": "{base_path}", "CUSTOM_USER": "slotserve", "PASSWORD": "{token}", "TITLE": "SlotServe"}
        ),
        "web_ports": json.dumps([{"name": "Desktop", "port": 3000, "kind": "desktop"}]),
        "keep_entrypoint": True,
    },
    {
        "builtin_key": "vscode",
        "name": "VS Code",
        "image": "codercom/code-server:4.141.0",
        "description": "VS Code in the browser (code-server), opened on /workspace.",
        "command": "code-server --bind-addr 0.0.0.0:8080 --auth none --disable-telemetry --disable-update-check"
        " /workspace",
        "web_ports": json.dumps([{"name": "VS Code", "port": 8080, "kind": "vscode"}]),
    },
)
WEB_FIELDS = ("command", "env", "web_ports", "keep_entrypoint")


async def seed_templates(db) -> None:
    existing = {
        t.builtin_key: t
        for t in (await db.execute(select(DevTemplate).where(DevTemplate.builtin_key.is_not(None)))).scalars()
    }
    for spec in BUILTINS:
        row = existing.get(spec["builtin_key"])
        if row is None:
            db.add(DevTemplate(kind="builtin", **spec))
        elif (
            spec.get("web_ports")
            and row.image == spec["image"]
            and row.web_ports is None
            and row.command is None
            and row.env is None
        ):
            # seeded before templates had web interfaces and not changed since
            for name in WEB_FIELDS:
                setattr(row, name, spec.get(name))
            row.name, row.description = spec["name"], spec["description"]
    await db.commit()


def port_entry(web: dict | None, port: int) -> dict:
    """How the proxy treats a port: the declared entry, or a plain HTTP app that gets the prefix stripped."""
    for p in (web or {}).get("ports", []):
        if p["port"] == port:
            return p
    return {"name": str(port), "port": port, "kind": "http", "strip": True}


def session_web(session: DevSession) -> dict | None:
    """How SlotServe reaches a running session's ports: {token, ports: [{name, port, kind, strip}], container,
    ip, published: {"<port>": "127.0.0.1:<host port>"}}."""
    if not session.web:
        return None
    try:
        return json.loads(session.web)
    except ValueError:
        return None


def visible(template: DevTemplate | None, user: User) -> bool:
    if template is None or template.deleted_at is not None:
        return False
    if template.kind == "builtin":
        return template.enabled
    return template.owner_id == user.id or user.is_admin
