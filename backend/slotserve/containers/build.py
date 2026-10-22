"""build_spec(ctx): runs the contributors in their fixed order. The list below is the one shared line-per-feature
point: a new concern is a new contributor file plus one line here."""

from .context import SessionContext
from .contributors.cpu import CpuContributor
from .contributors.gpu import GpuContributor
from .contributors.models import ModelsContributor
from .contributors.session import SessionContributor
from .contributors.ssh import SshContributor
from .contributors.web import WebContributor
from .contributors.workspace import WorkspaceContributor
from .harden import harden
from .model import ContainerSpec, NodePolicy
from .render import render

CONTRIBUTORS = [
    SessionContributor(),
    SshContributor(),
    WorkspaceContributor(),
    WebContributor(),
    GpuContributor(),
    ModelsContributor(),
    CpuContributor(),
]


def build_spec(ctx: SessionContext) -> ContainerSpec:
    spec = ContainerSpec(name="", image=ctx.image)
    for contributor in CONTRIBUTORS:
        contributor.contribute(ctx, spec)
    return spec


def render_for_docker(spec: ContainerSpec, node_policy: NodePolicy) -> dict:
    """The only way from a spec to a Docker body: render, then harden as the very last step."""
    return harden(render(spec), kind=spec.kind, node_policy=node_policy, compat=spec.compat)
