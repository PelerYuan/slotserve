"""Storage: ``/workspace``, the kept directories of templates and kept environments, shared storages.

Managed storages (Admin -> Storage, ``ctx.storage``) come first; without them a directory is a bind mount under the
legacy storage directory (``ctx.storage_dir``, created by a helper before the start) or a Docker named volume.
"""

from ..context import SessionContext
from ..model import ContainerSpec
from ..names import path_slug


def _volume(ctx: SessionContext, spec: ContainerSpec, volume: str, rel: str, target: str) -> None:
    """A persistent directory: a bind mount under the storage directory, or a Docker named volume."""
    if ctx.storage_dir:
        spec.prepare.append((rel, target))
        spec.mounts.append({"Type": "bind", "Source": f"{ctx.storage_dir}/{rel}", "Target": target, "ReadOnly": False})
    else:
        spec.mounts.append({"Type": "volume", "Source": volume, "Target": target})


class WorkspaceContributor:
    name = "workspace"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        sp = ctx.storage
        if ctx.kind == "shared":
            lane_id = ctx.lane.id if ctx.lane else None
            _volume(ctx, spec, f"slotserve-idle-home-l{lane_id}", f"lanes/l{lane_id}/home", "/home")
        else:
            uid = ctx.user.id if ctx.user else None
            if sp is not None and sp.personal is not None:
                spec.mounts.append(sp.sub(sp.personal, f"users/u{uid}", "/workspace", seed="/workspace"))
            else:
                _volume(ctx, spec, f"slotserve-workspace-u{uid}", f"users/u{uid}/workspace", "/workspace")
            project, template = ctx.project, ctx.template
            paths = project.persist_paths if project is not None else (template.persist_paths if template else [])
            for path in paths:
                slug = path_slug(path)
                if project is not None:
                    if sp is not None and sp.projects is not None:
                        spec.mounts.append(sp.sub(sp.projects, f"projects/p{project.id}/{slug}", path, seed=path))
                        continue
                    rel = f"projects/p{project.id}/{slug}"
                    _volume(ctx, spec, f"slotserve-project{project.id}-{slug}", rel, path)
                else:
                    tid = template.id if template else 0
                    if sp is not None and sp.projects is not None:
                        rel = f"templates/u{uid}-t{tid}/{slug}"
                        spec.mounts.append(sp.sub(sp.projects, rel, path, seed=path))
                        continue
                    _volume(ctx, spec, f"slotserve-u{uid}-t{tid}-{slug}", f"users/u{uid}/t{tid}/{slug}", path)
        if sp is not None:
            spec.mounts.extend(m for m in sp.mounts if not any(m is x for x in spec.mounts))
