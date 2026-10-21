"""Model catalog: browse curated models, see whether they fit each lane, deploy them in one click."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..cluster import inventory
from ..config import get_settings
from ..db import get_db
from ..deps import admin_user, current_user, get_downloader
from ..errors import ApiError
from ..orm import CatalogDeploy, CatalogEntryRow, DownloadTask, Lane, LlmModel, User
from ..runtime import gpu
from ..services import booking, catalog
from ..services import catalog_deploy as cd
from ..services.audit import record
from ..services.catalog import Entry, Variant
from ..services.downloader import Downloader

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


# ---- schemas -----------------------------------------------------------------------


class EstimateOut(BaseModel):
    ctx: int
    weights: int
    kv_cache: int
    overhead: int
    total: int


class LaneFitOut(BaseModel):
    lane_id: int
    status: str  # fits | tight | no | unknown (with the GPUs of the lane's own machine)


class ModelRefOut(BaseModel):
    id: int
    name: str
    engine: str


class VariantOut(BaseModel):
    id: str
    format: str
    repo: str
    revision: str
    quant: str
    files: list[str]
    size_bytes: int
    file_count: int | None
    mmproj: str | None
    mmproj_size_bytes: int
    download_bytes: int
    gated: bool
    engines: list[str]
    estimate: EstimateOut
    fits: list[LaneFitOut]
    downloaded: bool  # on this machine
    models: list[ModelRefOut]
    node_ids: list[int] = []  # machines the variant is registered on already


class EntryOut(BaseModel):
    id: str
    name: str
    family: str
    publisher: str
    license: str
    tasks: list[str]
    params_b: float
    active_params_b: float | None
    context_length: int
    default_ctx: int
    tags: list[str]
    description: dict[str, str]
    source_repo: str | None
    arch: catalog.Arch | None
    extra_args: dict[str, str]
    source: str  # builtin | custom
    hidden: bool
    variants: list[VariantOut]


class CatalogLaneOut(BaseModel):
    id: int
    name: str
    gpus: list[int]
    memory_bytes: int | None
    node_id: int | None = None
    node_name: str | None = None
    node_online: bool = True


class CatalogOut(BaseModel):
    updated: str
    lanes: list[CatalogLaneOut]
    entries: list[EntryOut]


class DeployIn(BaseModel):
    entry_id: str = Field(min_length=1, max_length=64)
    variant_id: str = Field(min_length=1, max_length=64)
    engine: str | None = Field(default=None, pattern=r"^(llama\.cpp|vllm|sglang)$")
    lane_id: int | None = None
    set_default: bool = False
    ctx: int | None = Field(default=None, ge=256, le=10_000_000)
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\s/]+$")


class DeployOut(BaseModel):
    id: int
    entry_id: str
    variant_id: str
    engine: str
    model_name: str
    args: str
    status: str  # downloading | done | failed | stalled (a download failed, was cancelled or removed)
    error: str | None
    model_id: int | None
    lane_id: int | None
    node_id: int | None = None
    set_default: bool
    files: int
    downloaded_bytes: int
    total_bytes: int | None
    created_at: datetime
    finished_at: datetime | None


class HiddenIn(BaseModel):
    hidden: bool


# ---- helpers -----------------------------------------------------------------------


class _Nodes:
    """Other machines as the catalog sees them: their GPUs and models directories (from the agents)."""

    def __init__(self, registry=None, paths: dict[int, dict[str, list[int]]] | None = None):
        self.registry = registry
        self.paths = paths or {}

    def executor(self, node_id: int | None):
        if self.registry is None or node_id is None or self.registry.is_local(node_id):
            return None
        return self.registry.find(node_id)

    def models_dirs(self) -> dict[int, str]:
        if self.registry is None:
            return {}
        out = {}
        for ex in self.registry.all():
            models_dir = (getattr(ex, "inventory", None) or {}).get("models_dir")
            if not ex.is_local and models_dir:
                out[ex.node_id] = models_dir
        return out


async def _lane_memory(db: AsyncSession, nodes: _Nodes | None = None) -> tuple[list[Lane], dict[int, int | None]]:
    """Each lane's GPU memory, with the GPUs of the machine it runs on (other nodes: from their inventory)."""
    lanes = list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())
    local = await gpu.gpu_stats()
    memory = {}
    for lane in lanes:
        executor = (nodes or _Nodes()).executor(lane.node_id)
        gpus = executor.known_gpus() if executor is not None else local
        memory[lane.id] = catalog.lane_memory(lane.devices, gpus)
    return lanes, memory


async def _nodes(request: Request, db: AsyncSession) -> _Nodes:
    return _Nodes(getattr(request.app.state, "nodes", None), await cd.node_paths(db))


def _variant_out(
    entry: Entry,
    v: Variant,
    ctx: int | None,
    memory: dict[int, int | None],
    models: dict[str, list[LlmModel]],
    nodes: _Nodes | None = None,
) -> VariantOut:
    settings = get_settings()
    est = catalog.estimate(entry, v, ctx)
    refs = [
        ModelRefOut(id=m.id, name=m.name, engine=m.engine or "llama.cpp")
        for m in models.get(str(cd.target_path(settings, v)), [])
        if (m.engine or "llama.cpp") in v.engines
    ]
    node_ids = []
    if refs and nodes is not None and nodes.registry is not None:
        node_ids.append(nodes.registry.local.node_id)
    by_id = {m.id: m for found in models.values() for m in found}
    for node_id, models_dir in (nodes.models_dirs() if nodes is not None else {}).items():
        ids = nodes.paths.get(node_id, {}).get(cd.node_target(models_dir, v), [])
        mine = [by_id[i] for i in ids if i in by_id and (by_id[i].engine or "llama.cpp") in v.engines]
        if mine:
            node_ids.append(node_id)
            refs += [
                ModelRefOut(id=m.id, name=m.name, engine=m.engine or "llama.cpp")
                for m in mine
                if all(r.id != m.id for r in refs)
            ]
    return VariantOut(
        **v.model_dump(exclude={"include", "exclude"}),
        download_bytes=v.download_bytes,
        engines=v.engines,
        estimate=EstimateOut(**est),
        fits=[LaneFitOut(lane_id=lid, status=catalog.fit(est["total"], mem, v)) for lid, mem in memory.items()],
        downloaded=cd.downloaded(settings, v),
        models=refs,
        node_ids=[i for i in node_ids if i is not None],
    )


def _entry_out(
    item: cd.Listed,
    ctx: int | None,
    memory: dict[int, int | None],
    models: dict[str, list[LlmModel]],
    nodes: _Nodes | None = None,
) -> EntryOut:
    e = item.entry
    return EntryOut(
        **e.model_dump(include={"id", "name", "family", "publisher", "license", "tasks", "params_b"}),
        active_params_b=e.active_params_b,
        context_length=e.context_length,
        default_ctx=e.ctx,
        tags=e.tags,
        description=e.description.model_dump(),
        source_repo=e.source_repo,
        arch=e.arch,
        extra_args=e.extra_args,
        source=item.source,
        hidden=item.hidden,
        variants=[_variant_out(e, v, ctx, memory, models, nodes) for v in e.variants],
    )


async def _deploy_out(db: AsyncSession, deploys: list[CatalogDeploy]) -> list[DeployOut]:
    ids = {i for d in deploys for i in json.loads(d.task_ids)}
    tasks = {}
    if ids:
        tasks = {t.id: t for t in (await db.execute(select(DownloadTask).where(DownloadTask.id.in_(ids)))).scalars()}
    out = []
    for d in deploys:
        mine = [tasks.get(i) for i in json.loads(d.task_ids)]
        state = d.status
        error = d.error
        if state == "downloading" and any(t is None or t.status in ("failed", "cancelled") for t in mine):
            state = "stalled"
            error = next((t.error for t in mine if t is not None and t.error), None)
        known = [t for t in mine if t is not None]
        total = None if any(t.total_bytes is None for t in known) else sum(t.total_bytes or 0 for t in known)
        out.append(
            DeployOut(
                id=d.id,
                entry_id=d.entry_id,
                variant_id=d.variant_id,
                engine=d.engine,
                model_name=d.model_name,
                args=d.args,
                status=state,
                error=error,
                model_id=d.model_id,
                lane_id=d.lane_id,
                node_id=d.node_id,
                set_default=d.set_default,
                files=len(mine),
                downloaded_bytes=sum(t.downloaded_bytes for t in known),
                total_bytes=total,
                created_at=d.created_at,
                finished_at=d.finished_at,
            )
        )
    return out


def _deployer(request: Request) -> cd.Deployer:
    return request.app.state.deployer


# ---- browse ------------------------------------------------------------------------


@router.get("", response_model=CatalogOut)
async def list_catalog(
    request: Request,
    ctx: int | None = Query(default=None, ge=256, le=10_000_000),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every entry with memory estimates (``ctx`` overrides each entry's default context) and lane fit."""
    nodes = await _nodes(request, db)
    lanes, memory = await _lane_memory(db, nodes)
    models = await cd.registered(db)
    items = await cd.all_entries(db, include_hidden=user.is_admin)
    out_lanes = []
    for lane in lanes:
        executor = nodes.executor(lane.node_id)
        out_lanes.append(
            CatalogLaneOut(
                id=lane.id,
                name=lane.name,
                gpus=lane.devices or [],
                memory_bytes=memory[lane.id],
                node_id=lane.node_id if executor is not None else None,
                node_name=executor.name if executor is not None else None,
                node_online=executor.online if executor is not None else True,
            )
        )
    return CatalogOut(
        updated=catalog.builtin().updated,
        lanes=out_lanes,
        entries=[_entry_out(item, ctx, memory, models, nodes) for item in items],
    )


@router.get("/deploys", response_model=list[DeployOut])
async def list_deploys(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CatalogDeploy).order_by(CatalogDeploy.id.desc()).limit(50))).scalars().all()
    return await _deploy_out(db, list(rows))


@router.get("/{entry_id}", response_model=EntryOut)
async def get_entry(
    entry_id: str,
    request: Request,
    ctx: int | None = Query(default=None, ge=256, le=10_000_000),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await cd.find_entry(db, entry_id, include_hidden=user.is_admin)
    nodes = await _nodes(request, db)
    _lanes, memory = await _lane_memory(db, nodes)
    return _entry_out(item, ctx, memory, await cd.registered(db), nodes)


# ---- deploy ------------------------------------------------------------------------


@router.post("/deploy", response_model=DeployOut, status_code=201)
async def deploy(
    body: DeployIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    """Downloads a catalog variant and registers it as a model (at once when it is already on disk)."""
    settings = get_settings()
    entry = (await cd.find_entry(db, body.entry_id, include_hidden=user.is_admin)).entry
    variant = entry.variant(body.variant_id)
    if variant is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    engine = body.engine or variant.engines[0]
    if engine not in variant.engines:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "catalog_engine_mismatch", engine=engine)
    if body.set_default and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
    lane = await booking.resolve_lane(db, body.lane_id) if body.lane_id is not None or body.set_default else None
    executor = None
    if lane is not None and lane.node_id is not None and lane.node_id != await inventory.local_node_id(db):
        # A lane of another machine: download into that node's models directory and register it there.
        executor = _deployer(request).cluster.executor(lane.node_id) if _deployer(request).cluster else None
        if executor is None or not (executor.inventory or {}).get("models_dir"):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "catalog_remote_lane")
    if executor is not None:
        return await _deploy_remote(request, db, user, downloader, entry, variant, engine, lane, executor, body)
    ctx = min(body.ctx or entry.ctx, entry.context_length)
    gpu_count = 1
    if lane is not None:
        gpu_count = len(lane.devices or []) or len(await gpu.gpu_stats()) or 1
    target = cd.target_path(settings, variant)
    mmproj = cd.mmproj_path(settings, variant)
    row = CatalogDeploy(
        user_id=user.id,
        entry_id=entry.id,
        variant_id=variant.id,
        engine=engine,
        model_name=body.name or catalog.model_name(entry, variant),
        args=catalog.launch_args(entry, engine, ctx, gpu_count, str(mmproj) if mmproj else None),
        description=entry.description.en,
        target_path=str(target),
        lane_id=lane.id if lane else None,
        set_default=body.set_default,
    )

    # Already registered (or fully on disk): no download, just use it.
    present = any((m.engine or "llama.cpp") == engine for m in (await cd.registered(db)).get(str(target), []))
    if present or cd.downloaded(settings, variant):
        task_ids: list[int] = []
    else:
        if variant.gated and not settings.hf_token:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "hf_token_required", repo=variant.repo)
        plans = await cd.plan_files(settings, variant)
        cd.check_space(settings, variant, plans)
        task_ids = await cd.queue_files(db, settings, user.id, plans)
    row.task_ids = json.dumps(task_ids)
    db.add(row)
    await db.flush()
    record(db, user, "catalog.deploy", f"{entry.id}/{variant.id}", engine=engine, files=len(task_ids))
    if not task_ids:
        await _deployer(request).finish_now(db, row)
    await db.commit()
    if task_ids:
        downloader.poke()
    return (await _deploy_out(db, [row]))[0]


async def _deploy_remote(request, db, user, downloader, entry, variant, engine, lane, executor, body) -> DeployOut:
    """A catalog deployment onto another node: queued there, fitted to its GPUs, registered on it."""
    settings = get_settings()
    inv = executor.inventory or {}
    models_dir = str(inv["models_dir"])
    ctx = min(body.ctx or entry.ctx, entry.context_length)
    gpu_count = len(lane.devices or []) or len(executor.known_gpus()) or 1
    target = cd.node_target(models_dir, variant)
    mmproj = cd.node_mmproj(models_dir, variant)
    row = CatalogDeploy(
        user_id=user.id,
        entry_id=entry.id,
        variant_id=variant.id,
        engine=engine,
        model_name=body.name or catalog.model_name(entry, variant),
        args=catalog.launch_args(entry, engine, ctx, gpu_count, mmproj),
        description=entry.description.en,
        target_path=target,
        lane_id=lane.id,
        set_default=body.set_default,
        node_id=lane.node_id,
    )
    paths = await cd.node_paths(db)
    registered = {m.id: m for found in (await cd.registered(db)).values() for m in found}
    present = any(
        (registered[i].engine or "llama.cpp") == engine
        for i in paths.get(lane.node_id, {}).get(target, [])
        if i in registered
    )
    if not present and executor.online:
        stat = await executor.request("models.stat", {"path": target, "engine": engine})
        present = bool(stat.get("exists")) and stat.get("complete") is not False
    if present:
        task_ids: list[int] = []
    else:
        if variant.gated and not settings.hf_token:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "hf_token_required", repo=variant.repo)
        cd.check_node_space(inv.get("models_free_bytes"), variant, settings.download_min_free_gb)
        plans = await cd.plan_files(settings, variant)
        task_ids = await cd.queue_node_files(db, lane.node_id, user.id, plans)
    row.task_ids = json.dumps(task_ids)
    db.add(row)
    await db.flush()
    record(
        db, user, "catalog.deploy", f"{entry.id}/{variant.id}", engine=engine, files=len(task_ids), node=executor.name
    )
    if not task_ids:
        if not executor.online:
            raise ApiError(status.HTTP_409_CONFLICT, "node_offline", node=executor.name)
        await _deployer(request).finish_now(db, row)
    await db.commit()
    if task_ids:
        downloader.poke()
    return (await _deploy_out(db, [row]))[0]


@router.delete("/deploys/{deploy_id}", status_code=204)
async def dismiss_deploy(
    deploy_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    """Removes a deployment from the list; an unfinished one is cancelled along with its queued downloads."""
    row = await db.get(CatalogDeploy, deploy_id)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if not user.is_admin and row.user_id != user.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_owner")
    if row.status == "downloading":
        ids = json.loads(row.task_ids)
        others = (
            await db.execute(
                select(CatalogDeploy).where(CatalogDeploy.status == "downloading", CatalogDeploy.id != row.id)
            )
        ).scalars()
        shared = {i for d in others for i in json.loads(d.task_ids)}
        for task in (await db.execute(select(DownloadTask).where(DownloadTask.id.in_(ids)))).scalars():
            if task.id in shared:
                continue
            if task.status == "running":
                downloader.cancel(task.id)
            elif task.status == "queued":
                task.status = "cancelled"
    await db.delete(row)
    await db.commit()


# ---- admin: hide built-in entries, add your own ------------------------------------


@router.put("/{entry_id}/hidden", status_code=204)
async def hide_entry(
    entry_id: str, body: HiddenIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    await cd.find_entry(db, entry_id, include_hidden=True)
    await cd.set_hidden(db, entry_id, body.hidden)
    record(db, admin, "catalog.hide" if body.hidden else "catalog.show", entry_id)
    await db.commit()


async def _save_custom(
    db: AsyncSession, body: Entry, row: CatalogEntryRow | None, admin: User, request: Request | None = None
) -> EntryOut:
    entry = await cd.complete_entry(get_settings(), body)
    if row is None:
        row = CatalogEntryRow(entry_id=entry.id, data="{}", created_by=admin.id)
        db.add(row)
    row.data = entry.model_dump_json(exclude_none=True)
    record(db, admin, "catalog.save", entry.id)
    await db.commit()
    nodes = await _nodes(request, db) if request is not None else None
    _lanes, memory = await _lane_memory(db, nodes)
    hidden = entry.id in await cd.hidden_ids(db)
    return _entry_out(cd.Listed(entry, "custom", hidden), None, memory, await cd.registered(db), nodes)


@router.post("/entries", response_model=EntryOut, status_code=201)
async def create_entry(
    body: Entry, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    """Adds an entry; file sizes, split parts and the architecture are looked up on Hugging Face."""
    if any(e.id == body.id for e in catalog.builtin().entries):
        raise ApiError(status.HTTP_409_CONFLICT, "catalog_id_taken", id=body.id)
    taken = await db.execute(select(CatalogEntryRow.id).where(CatalogEntryRow.entry_id == body.id))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "catalog_id_taken", id=body.id)
    return await _save_custom(db, body, None, admin, request)


@router.put("/entries/{entry_id}", response_model=EntryOut)
async def update_entry(
    entry_id: str,
    body: Entry,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(CatalogEntryRow).where(CatalogEntryRow.entry_id == entry_id))).scalar_one_or_none()
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if body.id != entry_id:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "catalog_id_fixed")
    return await _save_custom(db, body, row, admin, request)


@router.delete("/entries/{entry_id}", status_code=204)
async def delete_entry(entry_id: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(CatalogEntryRow).where(CatalogEntryRow.entry_id == entry_id))).scalar_one_or_none()
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await db.delete(row)
    await cd.set_hidden(db, entry_id, False)
    record(db, admin, "catalog.delete", entry_id)
    await db.commit()
