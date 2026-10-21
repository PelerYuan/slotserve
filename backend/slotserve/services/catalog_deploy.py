"""Catalog entries from the database, Hugging Face lookups and one-click deployment."""

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..errors import ApiError
from ..orm import CatalogDeploy, CatalogEntryRow, DownloadTask, Lane, LlmModel
from . import catalog, kv, scanner
from .catalog import Entry, Variant

log = logging.getLogger(__name__)

HIDDEN_KEY = "catalog_hidden"
ACTIVE = ("queued", "running")


# ---- entries ---------------------------------------------------------------------


@dataclass
class Listed:
    entry: Entry
    source: str  # builtin | custom
    hidden: bool


async def hidden_ids(db: AsyncSession) -> set[str]:
    return set(await kv.get(db, HIDDEN_KEY) or [])


async def set_hidden(db: AsyncSession, entry_id: str, hidden: bool) -> None:
    ids = await hidden_ids(db)
    ids = ids | {entry_id} if hidden else ids - {entry_id}
    await kv.put(db, HIDDEN_KEY, sorted(ids) or None)


async def custom_rows(db: AsyncSession) -> list[CatalogEntryRow]:
    return list((await db.execute(select(CatalogEntryRow).order_by(CatalogEntryRow.id))).scalars())


async def all_entries(db: AsyncSession, include_hidden: bool) -> list[Listed]:
    hidden = await hidden_ids(db)
    out = [Listed(e, "builtin", e.id in hidden) for e in catalog.builtin().entries]
    for row in await custom_rows(db):
        try:
            entry = Entry.model_validate_json(row.data)
        except ValueError:
            log.warning("skipping invalid custom catalog entry %s", row.entry_id)
            continue
        out.append(Listed(entry, "custom", entry.id in hidden))
    return [item for item in out if include_hidden or not item.hidden]


async def find_entry(db: AsyncSession, entry_id: str, include_hidden: bool) -> Listed:
    for item in await all_entries(db, include_hidden):
        if item.entry.id == entry_id:
            return item
    raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")


# ---- Hugging Face ----------------------------------------------------------------


def hf_url(settings: Settings, repo: str, revision: str, path: str) -> str:
    return f"{settings.hf_endpoint.rstrip('/')}/{repo}/resolve/{quote(revision, safe='')}/{quote(path)}"


async def hf_get(settings: Settings, path: str, params: dict | None = None, repo: str = ""):
    """GET on the configured Hugging Face endpoint (with the token); returns parsed JSON."""
    headers = {"Authorization": f"Bearer {settings.hf_token}"} if settings.hf_token else {}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(f"{settings.hf_endpoint.rstrip('/')}{path}", params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "hf_unreachable", reason=exc.__class__.__name__) from exc
    if resp.status_code in (401, 403):
        raise ApiError(status.HTTP_403_FORBIDDEN, "hf_forbidden")
    if resp.status_code == 404:
        raise ApiError(status.HTTP_404_NOT_FOUND, "hf_not_found", repo=repo)
    if resp.status_code != 200:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "hf_unreachable", reason=f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "hf_unreachable", reason="invalid JSON") from exc


async def hf_tree(settings: Settings, repo: str, revision: str) -> list[dict]:
    data = await hf_get(settings, f"/api/models/{repo}/tree/{quote(revision, safe='')}", {"recursive": "true"}, repo)
    return data if isinstance(data, list) else []


async def complete_entry(settings: Settings, entry: Entry) -> Entry:
    """Fills in what an admin left out of a custom entry: file sizes, split parts, architecture."""
    variants = []
    trees: dict[tuple[str, str], dict[str, int]] = {}
    for v in entry.variants:
        key = (v.repo, v.revision)
        if key not in trees:
            items = await hf_tree(settings, v.repo, v.revision)
            trees[key] = {
                i["path"]: int((i.get("lfs") or {}).get("size") or i.get("size") or 0)
                for i in items
                if i.get("type") == "file"
            }
        files = trees[key]
        data = v.model_dump()
        if v.format == "gguf":
            parts = list(v.files)
            m = scanner.SPLIT_RE.search(parts[0])
            if len(parts) == 1 and m is not None:  # only the first part given: add the others
                stem = parts[0][: m.start()]
                count = int(m.group(2))
                parts = [f"{stem}-{i:05d}-of-{count:05d}.gguf" for i in range(1, count + 1)]
            missing = [p for p in parts + ([v.mmproj] if v.mmproj else []) if p not in files]
            if missing:
                raise ApiError(status.HTTP_400_BAD_REQUEST, "catalog_file_missing", file=missing[0], repo=v.repo)
            data.update(files=parts, size_bytes=sum(files[p] for p in parts))
            if v.mmproj:
                data["mmproj_size_bytes"] = files[v.mmproj]
        else:
            chosen = catalog.select_files([{"type": "file", "path": p, "size": s} for p, s in files.items()], v)
            if "config.json" not in dict(chosen):
                raise ApiError(status.HTTP_400_BAD_REQUEST, "catalog_file_missing", file="config.json", repo=v.repo)
            data.update(size_bytes=sum(s for _, s in chosen), file_count=len(chosen))
        variants.append(Variant.model_validate(data))
    out = entry.model_copy(update={"variants": variants})
    cfg_repo = entry.config_repo or entry.source_repo
    if out.arch is None and cfg_repo and "embedding" not in entry.tasks:
        try:
            cfg = await hf_get(settings, f"/{cfg_repo}/resolve/main/config.json", repo=cfg_repo)
            text = cfg.get("text_config") or cfg
            heads = text["num_attention_heads"]
            out.arch = catalog.Arch(
                layers=text["num_hidden_layers"],
                kv_heads=text.get("num_key_value_heads") or heads,
                head_dim=text.get("head_dim") or text["hidden_size"] // heads,
            )
        except (ApiError, KeyError, TypeError, ValueError):
            pass  # no estimate of the KV cache then
    return out


# ---- local state -----------------------------------------------------------------


def gguf_target(settings: Settings, variant: Variant) -> Path:
    return settings.models_dir.resolve() / variant.files[0].rsplit("/", 1)[-1]


def target_path(settings: Settings, variant: Variant) -> Path:
    if variant.format == "gguf":
        return gguf_target(settings, variant)
    return settings.models_dir.resolve() / catalog.local_dir_name(variant.repo)


def mmproj_path(settings: Settings, variant: Variant) -> Path | None:
    return settings.models_dir.resolve() / variant.mmproj.rsplit("/", 1)[-1] if variant.mmproj else None


def downloaded(settings: Settings, variant: Variant) -> bool:
    """Every file is on disk already (safetensors: the folder looks complete)."""
    if variant.format == "gguf":
        root = settings.models_dir.resolve()
        names = [f.rsplit("/", 1)[-1] for f in variant.files + ([variant.mmproj] if variant.mmproj else [])]
        return all((root / n).is_file() for n in names)
    folder = target_path(settings, variant)
    return (folder / "config.json").is_file() and not any(folder.glob("*.part"))


def engine_column(engine: str) -> str | None:
    return None if engine == "llama.cpp" else engine


async def registered(db: AsyncSession) -> dict[str, list[LlmModel]]:
    """Live models by file path."""
    rows = (await db.execute(select(LlmModel).where(LlmModel.deleted_at.is_(None)))).scalars()
    out: dict[str, list[LlmModel]] = {}
    for m in rows:
        out.setdefault(m.file_path, []).append(m)
    return out


# ---- deployment ------------------------------------------------------------------


@dataclass
class FilePlan:
    filename: str  # relative to the models directory
    url: str
    size: int | None


async def plan_files(settings: Settings, variant: Variant) -> list[FilePlan]:
    """What to download, the file the model registers with last."""
    if variant.format == "gguf":
        paths = variant.files[1:] + ([variant.mmproj] if variant.mmproj else []) + variant.files[:1]
        sizes = {variant.mmproj: variant.mmproj_size_bytes} if variant.mmproj else {}
        return [
            FilePlan(p.rsplit("/", 1)[-1], hf_url(settings, variant.repo, variant.revision, p), sizes.get(p))
            for p in paths
        ]
    tree = await hf_tree(settings, variant.repo, variant.revision)
    chosen = catalog.select_files(tree, variant)
    if "config.json" not in dict(chosen):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "catalog_file_missing", file="config.json", repo=variant.repo)
    folder = catalog.local_dir_name(variant.repo)
    chosen.sort(key=lambda item: item[0] == "config.json")  # config.json last: the folder is complete after it
    return [FilePlan(f"{folder}/{p}", hf_url(settings, variant.repo, variant.revision, p), s) for p, s in chosen]


def check_space(settings: Settings, variant: Variant, plans: list[FilePlan]) -> None:
    root = settings.models_dir
    root.mkdir(parents=True, exist_ok=True)
    pending = [p for p in plans if not (root / p.filename).exists()]
    if variant.format == "gguf":
        on_disk = sum((root / p.filename).stat().st_size for p in plans if (root / p.filename).exists())
        needed = max(variant.download_bytes - on_disk, 0)
    else:
        needed = sum(p.size or 0 for p in pending)
    needed -= sum(
        (root / (p.filename + ".part")).stat().st_size for p in pending if (root / (p.filename + ".part")).exists()
    )
    free = shutil.disk_usage(root).free
    reserve = int(settings.download_min_free_gb * 1e9)
    if needed > 0 and free < needed + reserve:
        raise ApiError(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            "insufficient_disk",
            free_gb=round(free / 1e9, 1),
            need_gb=round((needed + reserve) / 1e9, 1),
        )


async def queue_files(db: AsyncSession, settings: Settings, user_id: int, plans: list[FilePlan]) -> list[int]:
    """Download tasks for files that are not on disk yet (re-using ones already queued)."""
    root = settings.models_dir
    ids: list[int] = []
    for plan in plans:
        if (root / plan.filename).exists():
            continue
        active = await db.execute(
            select(DownloadTask.id).where(
                DownloadTask.filename == plan.filename,
                DownloadTask.status.in_(ACTIVE),
                DownloadTask.node_id.is_(None),  # this machine's downloads (other nodes have their own)
                DownloadTask.storage_id.is_(None),
            )
        )
        task_id = active.scalar_one_or_none()
        if task_id is None:
            task = DownloadTask(user_id=user_id, url=plan.url, filename=plan.filename)
            db.add(task)
            await db.flush()
            task_id = task.id
        ids.append(task_id)
    return ids


async def sync_default_flags(db: AsyncSession) -> None:
    from ..api.models import sync_default_flags as sync  # local import: api imports services

    await sync(db)


class Deployer:
    """Registers catalog models once their downloads are complete (hooked into the Downloader)."""

    def __init__(self, settings: Settings, lanes=None):
        self.settings = settings
        self.lanes = lanes
        self._lock = asyncio.Lock()

    async def _pending(self, db: AsyncSession) -> list[CatalogDeploy]:
        return list((await db.execute(select(CatalogDeploy).where(CatalogDeploy.status == "downloading"))).scalars())

    async def owns(self, task_id: int) -> bool:
        async with SessionLocal() as db:
            return any(task_id in json.loads(d.task_ids) for d in await self._pending(db))

    async def task_done(self, task_id: int) -> None:
        async with self._lock, SessionLocal() as db:
            for deploy in await self._pending(db):
                ids = json.loads(deploy.task_ids)
                if task_id not in ids:
                    continue
                rows = await db.execute(select(DownloadTask.id, DownloadTask.status).where(DownloadTask.id.in_(ids)))
                states = {task: state for task, state in rows.all()}
                if all(states.get(i) == "done" for i in ids):
                    await self._finish(db, deploy)
            await db.commit()

    async def finish_now(self, db: AsyncSession, deploy: CatalogDeploy) -> LlmModel | None:
        async with self._lock:
            return await self._finish(db, deploy)

    async def _finish(self, db: AsyncSession, deploy: CatalogDeploy) -> LlmModel | None:
        try:
            model = await self._register(db, deploy)
        except Exception as exc:  # noqa: BLE001
            log.warning("catalog deploy %s failed: %s", deploy.id, exc)
            deploy.status, deploy.error, deploy.finished_at = "failed", str(exc)[:1000], utcnow()
            return None
        deploy.status, deploy.model_id, deploy.finished_at = "done", model.id, utcnow()
        ids = json.loads(deploy.task_ids)
        if ids:
            await db.execute(update(DownloadTask).where(DownloadTask.id.in_(ids)).values(model_id=model.id))
        if deploy.set_default and deploy.lane_id is not None:
            lane = await db.get(Lane, deploy.lane_id)
            if lane is not None:
                lane.default_model_id = model.id
                await sync_default_flags(db)
                if self.lanes is not None:
                    self.lanes.poke(lane.id)
        return model

    async def _register(self, db: AsyncSession, deploy: CatalogDeploy) -> LlmModel:
        path = Path(deploy.target_path)
        engine = engine_column(deploy.engine)
        found = await db.execute(
            select(LlmModel).where(LlmModel.file_path == str(path), LlmModel.deleted_at.is_(None)).order_by(LlmModel.id)
        )
        for model in found.scalars():
            if model.engine == engine:
                return model
        if engine is None:
            if not path.is_file():
                raise RuntimeError(f"file not found: {path.name}")
            size = scanner.total_size(path)
        else:
            if not (path / "config.json").is_file():
                raise RuntimeError(f"incomplete model folder: {path.name}")
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        model = LlmModel(
            name=await scanner.unique_name(db, deploy.model_name),
            file_path=str(path),
            size_bytes=size,
            status="ready",
            args=deploy.args,
            description=deploy.description,
            engine=engine,
            created_by=deploy.user_id,
        )
        db.add(model)
        await db.flush()
        return model
