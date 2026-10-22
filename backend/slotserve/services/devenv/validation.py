"""Checks of what people enter (images, kept paths, port ranges), Unix account names and access."""

import json
import posixpath
import re

from sqlalchemy import select

from ...containers.names import path_slug
from ...orm import DevSession, User
from ..docker_api import registry_of
from .constants import FORBIDDEN_PATHS, IMAGE_RE, MAX_PERSIST_PATHS, PATH_RE, RESERVED_NAMES, DevEnvError


def parse_port_range(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
    if not m:
        raise ValueError("expected a range like 2200-2299")
    lo, hi = int(m.group(1)), int(m.group(2))
    if not (1 <= lo <= hi <= 65535):
        raise ValueError("ports must be between 1 and 65535, low to high")
    return lo, hi


def check_image(settings, image: str) -> str:
    image = image.strip()
    if not IMAGE_RE.match(image):
        raise DevEnvError("devenv_image_invalid")
    allowed = [r.strip().lower() for r in settings.devenv_allowed_registries.split(",") if r.strip()]
    if allowed and registry_of(image) not in allowed:
        raise DevEnvError("devenv_registry_not_allowed", registries=", ".join(allowed))
    return image


def check_persist_paths(paths: list[str]) -> list[str]:
    """Extra directories kept between bookings: absolute, normalized, not a system directory."""
    out: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        norm = posixpath.normpath(path)
        bad = (
            not PATH_RE.match(path)
            or norm != path.rstrip("/")
            or norm == "/"
            or any(norm == f or norm.startswith(f + "/") or f.startswith(norm + "/") for f in FORBIDDEN_PATHS)
        )
        if bad:
            raise DevEnvError("devenv_path_invalid", path=path[:200])
        if norm not in out:
            out.append(norm)
    if len(out) > MAX_PERSIST_PATHS:
        raise DevEnvError("devenv_path_limit", limit=MAX_PERSIST_PATHS)
    return out


def load_paths(value: str | None) -> list[str]:
    try:
        data = json.loads(value) if value else []
    except ValueError:
        return []
    return [str(p) for p in data] if isinstance(data, list) else []


def dump_paths(paths: list[str]) -> str | None:
    return json.dumps(paths) if paths else None


_slug = path_slug


def unix_names(users: list[User]) -> dict[int, str]:
    """A stable, valid Unix account name for every user (lower case; clashes get the user id appended)."""
    out: dict[int, str] = {}
    taken: set[str] = set()
    for u in sorted(users, key=lambda x: x.id):
        name = re.sub(r"[^a-z0-9_-]", "_", u.username.lower())[:28]
        if not re.match(r"^[a-z_]", name):
            name = "u" + name
        if name in RESERVED_NAMES or name in taken:
            name = f"{name[:24]}_{u.id}"
        taken.add(name)
        out[u.id] = name
    return out


async def unix_name(db, user: User) -> str:
    users = (await db.execute(select(User).where(User.disabled_at.is_(None)))).scalars().all()
    return unix_names([*users, user]).get(user.id, user.username.lower())


def can_access(session: DevSession, user: User | None) -> bool:
    """Who may see and use a dev environment (and open its web interfaces): its owner, admins, and anyone for an
    environment that belongs to no booking (a lane's shared environment). Never a disabled account."""
    if user is None or getattr(user, "disabled_at", None) is not None:
        return False
    kind = getattr(session, "kind", None)
    shared = kind == "shared" if kind else session.reservation_id is None
    if user.is_admin or shared:
        return True
    return session.user_id is not None and session.user_id == user.id
