"""One-time setup code for creating the first admin account.

While no admin exists, signing up requires this code instead of the invite code, and the account becomes the
admin. The code is kept in ``<data dir>/setup_code`` (owner-only), printed in the server log at every start and
shown by ``slotserve setup-code``; it is deleted as soon as an admin exists. Installations that already have an
admin never see it.
"""

import asyncio
import hmac
import logging
import secrets
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..orm import User
from .secret_key import write_private

log = logging.getLogger("slotserve")

FILE_NAME = "setup_code"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
GROUPS, GROUP_LEN = 3, 4


def generate() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(GROUP_LEN)) for _ in range(GROUPS))


def normalize(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


async def admin_exists(db: AsyncSession) -> bool:
    return (await db.execute(select(User.id).where(User.is_admin.is_(True)).limit(1))).first() is not None


class SetupCode:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / FILE_NAME
        self.lock = asyncio.Lock()  # one first admin, even when two people sign up at the same moment

    def current(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def ensure(self) -> str:
        if code := self.current():
            return code
        code = generate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        try:
            write_private(self.path, code + "\n")
        except FileExistsError:
            return self.current() or code
        return code

    def verify(self, code: str | None) -> bool:
        expected = self.current()
        if not expected or not code:
            return False
        return hmac.compare_digest(normalize(code).encode(), normalize(expected).encode())

    def consume(self) -> None:
        self.path.unlink(missing_ok=True)

    async def refresh(self, db: AsyncSession) -> str | None:
        """At startup: drop the code when an admin exists, otherwise make sure there is one and log it."""
        if await admin_exists(db):
            self.consume()
            return None
        code = self.ensure()
        log.warning(
            "\n"
            "  ============================================================\n"
            "  No admin account yet. Open SlotServe, choose Sign up and\n"
            "  enter this setup code to create the admin account:\n"
            "\n"
            "      %s\n"
            "\n"
            "  (also shown by `slotserve setup-code`; it stops working once\n"
            "  an admin exists)\n"
            "  ============================================================",
            code,
        )
        return code


async def cli_setup_code(settings) -> str | None:
    """``slotserve setup-code``: the current code, or None when an admin already exists."""
    from .. import db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.configure(settings.database_url)
    await db.create_all()
    try:
        async with db.SessionLocal() as session:
            if await admin_exists(session):
                return None
        return SetupCode(settings.data_dir).ensure()
    finally:
        await db.dispose()
