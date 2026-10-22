"""Who and what the container is: its name, host name, labels, network and how it starts."""

import re

from ..context import SessionContext
from ..model import ContainerSpec
from ..names import LABEL, network_name, project_network_name


class SessionContributor:
    name = "session"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        spec.image = ctx.image
        spec.kind = ctx.kind  # type: ignore[assignment]
        project = ctx.project
        if ctx.kind == "shared":
            lane_id = ctx.lane.id if ctx.lane else None
            spec.name = f"slotserve-idle-{lane_id}"
            lane_name = ctx.lane.name if ctx.lane else "lane"
            spec.hostname = f"{re.sub(r'[^a-z0-9-]', '-', lane_name.lower())[:40]}-idle"
            spec.env["SLOTSERVE_SHARED"] = "1"
        else:
            spec.name = f"slotserve-proj-{project.id}" if project is not None else f"slotserve-dev-{ctx.session_id}"
            spec.hostname = f"{(ctx.user.username if ctx.user else 'dev')[:40]}-dev"
            spec.env["SLOTSERVE_USER"] = ctx.user.username if ctx.user else ""
            spec.env["SLOTSERVE_END"] = ctx.end_at
        if project is not None:
            spec.network = project_network_name(project.id)
            spec.network_labels = {LABEL: "1", "slotserve.project": str(project.id)}
        else:
            spec.network = network_name(ctx.session_id)
            spec.network_labels = {LABEL: "1", "slotserve.session": str(ctx.session_id)}
        spec.labels = {LABEL: "1", "slotserve.session": str(ctx.session_id), "slotserve.kind": ctx.kind or "booking"}
        if ctx.user is not None:
            spec.labels["slotserve.user"] = ctx.user.username
        if project is not None:
            spec.labels["slotserve.project"] = str(project.id)
        keep = bool(ctx.template is not None and ctx.template.keep_entrypoint)
        spec.keep_entrypoint = keep
        spec.compat = bool(ctx.template is not None and ctx.template.compat)
        spec.shm = ctx.resources.shm_bytes
        # an image that keeps its entrypoint may run an init system that has to be PID 1
        spec.init = not keep
        spec.restart = "no"
        spec.exposed = [22]
        if not keep:
            spec.entrypoint = ["/bin/sh", "/opt/slotserve/start.sh"]
            spec.cmd = []
            spec.user = "root"
            spec.workdir = "/" if ctx.kind == "shared" else "/workspace"
        # (an image that keeps its entrypoint keeps its own user; SSH is added with docker exec once it runs)
