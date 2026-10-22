"""Command line entry point.

slotserve                         run the server (same as `slotserve serve`)
slotserve reset-password <user>   set a new password (prompted)
slotserve set-admin <user>        grant admin rights (--revoke to remove)
"""

import argparse
import asyncio
import getpass
import logging
import sys

import uvicorn

from .config import get_settings


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    from . import db

    try:  # a database from a newer SlotServe: say so plainly instead of a startup traceback
        db.check(settings.data_dir / "slotserve.db", settings.data_dir / "backups")
    except db.SchemaError as exc:
        sys.exit(str(exc))
    uvicorn.run(
        "slotserve.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        log_level="info",
        # Open streams (live logs, long generations) must not keep a restart waiting.
        timeout_graceful_shutdown=5,
    )


async def _update_user(username: str, *, password: str | None = None, admin: bool | None = None) -> bool:
    from sqlalchemy import func, select

    from . import db
    from .orm import User
    from .security import hash_password

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Never upgrade from here: the upgrade (with its snapshot) belongs to the server's start.
    db.prepare(settings.data_dir / "slotserve.db", settings.data_dir / "backups", migrate=False)
    db.configure(settings.database_url)
    try:
        await db.create_all()
        async with db.SessionLocal() as session:
            user = (
                await session.execute(select(User).where(func.lower(User.username) == username.lower()))
            ).scalar_one_or_none()
            if user is None:
                return False
            if password is not None:
                user.password_hash = hash_password(password)
            if admin is not None:
                user.is_admin = admin
            await session.commit()
            return True
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="slotserve", description="SlotServe server and admin tools")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (default)")
    p_reset = sub.add_parser("reset-password", help="set a new password for a user")
    p_reset.add_argument("username")
    p_admin = sub.add_parser("set-admin", help="grant or revoke admin rights")
    p_admin.add_argument("username")
    p_admin.add_argument("--revoke", action="store_true")
    args = parser.parse_args()

    if args.command in (None, "serve"):
        serve()
        return

    from . import db

    if args.command == "reset-password":
        password = getpass.getpass("New password: ")
        if len(password) < 6:
            sys.exit("Password must be at least 6 characters.")
        if getpass.getpass("Repeat: ") != password:
            sys.exit("Passwords do not match.")
        update = _update_user(args.username, password=password)
    else:
        update = _update_user(args.username, admin=not args.revoke)
    try:
        ok = asyncio.run(update)
    except db.SchemaError as exc:
        sys.exit(str(exc))
    if not ok:
        sys.exit(f"User '{args.username}' not found.")
    print("Done.")


if __name__ == "__main__":
    main()
