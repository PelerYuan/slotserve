"""Command line entry point.

slotserve                         run the server (same as `slotserve serve`)
slotserve agent --server URL --token TOKEN
                                  run a node agent: this machine's GPUs serve lanes of that control plane
slotserve reset-password <user>   set a new password (prompted)
slotserve set-admin <user>        grant admin rights (--revoke to remove)
"""

import argparse
import asyncio
import contextlib
import getpass
import logging
import sys

import uvicorn

from .config import get_settings


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
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


def agent(server: str | None, token: str | None, address: str | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .agent.node import NodeAgent

    settings = get_settings()
    node = NodeAgent(settings, server=server, token=token, address=address)
    if not node.server or not node.token:
        sys.exit("slotserve agent needs --server and --token (or SLOTSERVE_AGENT_SERVER and SLOTSERVE_AGENT_TOKEN).")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.models_dir.mkdir(parents=True, exist_ok=True)

    async def main() -> None:
        try:
            await node.run()
        finally:
            await node.close()  # model servers stop with the agent

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


async def _update_user(username: str, *, password: str | None = None, admin: bool | None = None) -> bool:
    from sqlalchemy import func, select

    from . import db
    from .orm import User
    from .security import hash_password

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.configure(settings.database_url)
    await db.create_all()
    try:
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
    p_agent = sub.add_parser("agent", help="run a node agent that serves lanes of a control plane")
    p_agent.add_argument("--server", help="control plane URL, e.g. http://gpu-main:8000 (SLOTSERVE_AGENT_SERVER)")
    p_agent.add_argument("--token", help="the node token from Admin > Nodes (SLOTSERVE_AGENT_TOKEN)")
    p_agent.add_argument(
        "--address", help="address the control plane uses to reach this machine (SLOTSERVE_AGENT_ADDRESS)"
    )
    p_reset = sub.add_parser("reset-password", help="set a new password for a user")
    p_reset.add_argument("username")
    p_admin = sub.add_parser("set-admin", help="grant or revoke admin rights")
    p_admin.add_argument("username")
    p_admin.add_argument("--revoke", action="store_true")
    args = parser.parse_args()

    if args.command in (None, "serve"):
        serve()
        return
    if args.command == "agent":
        agent(args.server, args.token, args.address)
        return

    if args.command == "reset-password":
        password = getpass.getpass("New password: ")
        if len(password) < 6:
            sys.exit("Password must be at least 6 characters.")
        if getpass.getpass("Repeat: ") != password:
            sys.exit("Passwords do not match.")
        ok = asyncio.run(_update_user(args.username, password=password))
    else:
        ok = asyncio.run(_update_user(args.username, admin=not args.revoke))
    if not ok:
        sys.exit(f"User '{args.username}' not found.")
    print("Done.")


if __name__ == "__main__":
    main()
