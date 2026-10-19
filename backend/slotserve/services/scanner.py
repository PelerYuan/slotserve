"""Discovers GGUF files in the models directory and registers them as models."""

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..orm import LlmModel

SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)


def is_primary_gguf(path: Path) -> bool:
    name = path.name.lower()
    if not name.endswith(".gguf") or "mmproj" in name:
        return False
    m = SPLIT_RE.search(path.name)
    return m is None or int(m.group(1)) == 1


def model_name_for(path: Path) -> str:
    name = SPLIT_RE.sub("", path.name)
    if name.lower().endswith(".gguf"):
        name = name[:-5]
    return re.sub(r"\s+", "-", name) or "model"


def total_size(path: Path) -> int:
    m = SPLIT_RE.search(path.name)
    if m is None:
        return path.stat().st_size
    prefix = path.name[: m.start()]
    count = int(m.group(2))
    size = 0
    for i in range(1, count + 1):
        part = path.with_name(f"{prefix}-{i:05d}-of-{count:05d}.gguf")
        if part.is_file():
            size += part.stat().st_size
    return size


async def unique_name(db: AsyncSession, base: str) -> str:
    existing = set((await db.execute(select(LlmModel.name).where(LlmModel.name.like(f"{base}%")))).scalars())
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


async def register_file(db: AsyncSession, path: Path, settings: Settings, created_by: int | None) -> LlmModel | None:
    resolved = str(path.resolve())
    found = await db.execute(select(LlmModel.id).where(LlmModel.file_path == resolved, LlmModel.deleted_at.is_(None)))
    if found.first() is not None:
        return None
    model = LlmModel(
        name=await unique_name(db, model_name_for(path)),
        file_path=resolved,
        size_bytes=total_size(path),
        status="ready",
        args=settings.default_llama_args,
        created_by=created_by,
    )
    db.add(model)
    await db.flush()
    return model


async def scan(db: AsyncSession, settings: Settings, created_by: int | None) -> tuple[list[str], int]:
    added: list[str] = []
    skipped = 0
    root = settings.models_dir
    if not root.is_dir():
        return added, skipped
    for path in sorted(root.rglob("*.gguf")):
        if not path.is_file() or not is_primary_gguf(path):
            continue
        model = await register_file(db, path, settings, created_by)
        if model is None:
            skipped += 1
        else:
            added.append(model.name)
    await db.commit()
    return added, skipped
