"""Which node has which model (``node_models``).

Rules (kept simple so single-machine databases need no migration):

* a model without any ``node_models`` row belongs to the local node (registered before clustering or by hand);
* otherwise a model is available exactly on the nodes that have a row;
* when the first row for another node is added to a local-only model, a row for the local node is added too,
  so the model stays available where it was.

Shared storage (NFS) simply shows up as the same file on several nodes, each with its own row and path.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import utcnow
from ..orm import LlmModel, NodeModel
from ..services.scanner import ModelFile, unique_name


async def local_node_id(db: AsyncSession) -> int | None:
    from ..orm import Node

    return (await db.execute(select(Node.id).where(Node.is_local.is_(True)).limit(1))).scalar_one_or_none()


async def nodes_by_model(db: AsyncSession, local_id: int | None) -> dict[int, set[int]]:
    """model id -> node ids that have it; models missing here are local-only (see ``nodes_of``)."""
    out: dict[int, set[int]] = {}
    for model_id, node_id in (await db.execute(select(NodeModel.model_id, NodeModel.node_id))).all():
        out.setdefault(model_id, set()).add(node_id)
    return out


def nodes_of(model_id: int, mapping: dict[int, set[int]], local_id: int | None) -> set[int]:
    found = mapping.get(model_id)
    if found:
        return found
    return {local_id} if local_id is not None else set()


async def model_on_node(db: AsyncSession, model_id: int, node_id: int | None, local_id: int | None) -> bool:
    node_id = local_id if node_id is None else node_id
    rows = set((await db.execute(select(NodeModel.node_id).where(NodeModel.model_id == model_id))).scalars())
    if not rows:
        return node_id == local_id
    return node_id in rows


async def node_path(db: AsyncSession, model: LlmModel, node_id: int) -> str:
    found = (
        await db.execute(select(NodeModel.path).where(NodeModel.node_id == node_id, NodeModel.model_id == model.id))
    ).scalar_one_or_none()
    return found or model.file_path


async def add_row(
    db: AsyncSession,
    model: LlmModel,
    node_id: int,
    path: str,
    size_bytes: int,
    source: str,
    local_id: int | None,
) -> None:
    rows = {r.node_id: r for r in (await db.execute(select(NodeModel).where(NodeModel.model_id == model.id))).scalars()}
    if not rows and local_id is not None and node_id != local_id:
        db.add(NodeModel(node_id=local_id, model_id=model.id, path=model.file_path, size_bytes=model.size_bytes))
    row = rows.get(node_id)
    if row is None:
        db.add(NodeModel(node_id=node_id, model_id=model.id, path=path, size_bytes=size_bytes, source=source))
    else:
        row.path, row.size_bytes, row.seen_at = path, size_bytes, utcnow()
    await db.flush()


async def _match(db: AsyncSession, node_id: int, path: str) -> LlmModel | None:
    """The registered model for a file on a node: by that node's row, else by the same path."""
    model = (
        await db.execute(
            select(LlmModel)
            .join(NodeModel, NodeModel.model_id == LlmModel.id)
            .where(NodeModel.node_id == node_id, NodeModel.path == path, LlmModel.deleted_at.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if model is None:
        model = (
            await db.execute(select(LlmModel).where(LlmModel.file_path == path, LlmModel.deleted_at.is_(None)).limit(1))
        ).scalar_one_or_none()
    return model


async def model_at(db: AsyncSession, node_id: int, path: str) -> int | None:
    """The id of the model registered for a file on a node."""
    model = await _match(db, node_id, path)
    return model.id if model is not None else None


async def sync_node_files(db: AsyncSession, node_id: int, files: Iterable[dict], local_id: int | None) -> None:
    """A node reported the model files it has: update where known models are available (no new models)."""
    present: set[str] = set()
    for f in files:
        path = str(f.get("path") or "")
        if not path:
            continue
        present.add(path)
        model = await _match(db, node_id, path)
        if model is not None:
            await add_row(db, model, node_id, path, int(f.get("size_bytes") or 0), "scan", local_id)
    # Files that disappeared from the node (rows added by hand stay).
    await db.execute(
        delete(NodeModel).where(
            NodeModel.node_id == node_id,
            NodeModel.source.in_(("scan", "download")),
            NodeModel.path.not_in(present) if present else NodeModel.id.is_not(None),
        )
    )


async def register_node_files(
    db: AsyncSession,
    node_id: int,
    files: Iterable[ModelFile],
    settings: Settings,
    created_by: int | None,
    local_id: int | None,
    source: str = "scan",
) -> tuple[list[str], int]:
    """Registers model files found on a node (scan or download); returns (added names, already known)."""
    added: list[str] = []
    skipped = 0
    for f in files:
        model = await _match(db, node_id, f.path)
        if model is None:
            model = LlmModel(
                name=await unique_name(db, f.name),
                file_path=f.path,
                size_bytes=f.size_bytes,
                status="ready",
                args=settings.default_llama_args,
                created_by=created_by,
            )
            db.add(model)
            await db.flush()
            # A model first found on another node is only there (not local-only): give it its own row.
            db.add(NodeModel(node_id=node_id, model_id=model.id, path=f.path, size_bytes=f.size_bytes, source=source))
            await db.flush()
            added.append(model.name)
        else:
            await add_row(db, model, node_id, f.path, f.size_bytes, source, local_id)
            skipped += 1
    return added, skipped


async def claim_local(db: AsyncSession, files: Iterable[ModelFile], local_id: int | None) -> None:
    """After a local scan: models that other nodes also have get a local row when the file is here too."""
    if local_id is None:
        return
    for f in files:
        model = (
            await db.execute(select(LlmModel).where(LlmModel.file_path == f.path, LlmModel.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if model is None:
            continue
        has_rows = (
            await db.execute(select(NodeModel.id).where(NodeModel.model_id == model.id).limit(1))
        ).first() is not None
        if has_rows:
            await add_row(db, model, local_id, f.path, f.size_bytes, "scan", local_id)
