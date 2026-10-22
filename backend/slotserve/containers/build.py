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
from .model import ContainerSpec

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
