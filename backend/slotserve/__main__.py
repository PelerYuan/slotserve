"""Command line entry point.

slotserve                         run the server (same as `slotserve serve`)
slotserve reset-password <user>   set a new password (prompted)
slotserve set-admin <user>        grant admin rights (--revoke to remove)
slotserve setup-code              show the code for creating the first admin account
slotserve doctor [--json]         run the health checks here; exit code 1 when one is at level error
slotserve restore <bundle>        put a backup bundle back (the server must be stopped)
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
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level="info",
        # Open streams (live logs, long generations) must not keep a restart waiting.
        timeout_graceful_shutdown=5,
    )


async def _update_user(username: str, *, password: str | None = None, admin: bool | None = None) -> bool:
    from sqlalchemy import func, select

    from . import db
    from .orm import User
    from .security import hash_password, sessions

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
                await sessions.revoke_user(session, user.id, "password")  # signed-in browsers must sign in again
            if admin is not None:
                user.is_admin = admin
            await session.commit()
            return True
    finally:
        await db.dispose()


# ---- [health] doctor and restore (services/health.py, services/maintenance.py) ---------------------------------

LEVEL_MARK = {"ok": "ok   ", "info": "info ", "warn": "WARN ", "error": "ERROR"}


async def _doctor(as_json: bool) -> int:
    """Runs the check registry in this process, as the server would (without lanes and background tasks)."""
    import json

    from . import db
    from .containers.redact import redact, redact_mapping
    from .security import secret_key
    from .services import app_settings
    from .services.docker_api import DockerClient
    from .services.health import HealthMonitor

    settings = get_settings()
    secret_key.resolve(settings)
    db_path = settings.data_dir / "slotserve.db"
    if not db_path.is_file():
        print(f"ERROR no database at {db_path}: start the server once (or check SLOTSERVE_DATA_DIR)")
        return 1
    try:
        db.prepare(db_path, settings.data_dir / "backups", migrate=False)
    except db.SchemaError as exc:
        print(f"ERROR {exc}")
        return 1
    db.configure(settings.database_url)
    docker = None
    try:
        async with db.SessionLocal() as session:
            await app_settings.load_overrides(session, settings)
        if settings.devenv_enabled:
            docker = DockerClient(settings.docker_host)
        monitor = HealthMonitor(settings, None, docker, persist=False)
        await monitor.run_checks(force=True)
        items = monitor.items()
    finally:
        if docker is not None:
            await docker.close()
        await db.dispose()
    if as_json:
        print(json.dumps(redact_mapping(items, settings), indent=2, default=str, ensure_ascii=False))
    else:
        for item in items:
            print(f"{LEVEL_MARK[item['level']]} {item['id']:<11} {redact(item['title'], settings)}")
            evidence = redact(item["evidence"] or "", settings).strip().splitlines()
            if item["level"] in ("warn", "error"):
                for line in evidence[:8]:
                    print(f"      {line[:200]}")
    errors = [i for i in items if i["level"] == "error"]
    if not as_json:
        print()
        print(f"{len(errors)} error(s), {sum(i['level'] == 'warn' for i in items)} warning(s)")
    return 1 if errors else 0


def _restore(path: str, yes: bool, keep_key: bool) -> int:
    from pathlib import Path

    from .services import maintenance

    settings = get_settings()
    source = Path(path)
    if not source.exists() and (maintenance.backup_dir(settings) / path).exists():
        source = maintenance.backup_dir(settings) / path  # a bundle name from data/backups
    print(f"Restoring {source} into {settings.data_dir}.")
    print("Stop the SlotServe server first. The current database is kept as a 'prerestore' bundle.")
    if not yes and input("Type 'restore' to continue: ").strip() != "restore":
        print("Nothing changed.")
        return 1
    try:
        info = maintenance.restore(settings, source, restore_key=not keep_key)
    except maintenance.RestoreError as exc:
        print(f"Cannot restore: {exc}")
        return 1
    print(f"Restored the database from {info['backup_created_at']} (SlotServe {info['app_version'] or '?'}).")
    if info["prerestore"]:
        print(f"The previous state is in {info['prerestore']}.")
    for warning in info["warnings"]:
        print(f"Warning: {warning}")
    print(
        "Start the server: it compares the restored data with Docker and the disk and shows the report in "
        "Admin -> Overview (system health) and in data/restore_report.json. Until an admin dismisses it, "
        "containers the database does not know are kept. Workspaces, kept environments' data and models are not "
        "part of a backup."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="slotserve", description="SlotServe server and admin tools")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (default)")
    p_reset = sub.add_parser("reset-password", help="set a new password for a user")
    p_reset.add_argument("username")
    p_admin = sub.add_parser("set-admin", help="grant or revoke admin rights")
    p_admin.add_argument("username")
    p_admin.add_argument("--revoke", action="store_true")
    sub.add_parser("setup-code", help="show the code for creating the first admin account")
    p_doctor = sub.add_parser("doctor", help="run the health checks; exit code 1 when one fails")
    p_doctor.add_argument("--json", action="store_true", help="print the checks as JSON (secrets removed)")
    p_restore = sub.add_parser("restore", help="put a backup bundle back (stop the server first)")
    p_restore.add_argument("bundle", help="a bundle folder, or its name in data/backups")
    p_restore.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p_restore.add_argument("--keep-key", action="store_true", help="keep the current secret key")
    args = parser.parse_args()

    if args.command in (None, "serve"):
        serve()
        return

    from . import db

    if args.command == "doctor":
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        sys.exit(asyncio.run(_doctor(args.json)))
    if args.command == "restore":
        sys.exit(_restore(args.bundle, args.yes, args.keep_key))

    if args.command == "setup-code":
        from .security.setup_code import cli_setup_code

        try:
            code = asyncio.run(cli_setup_code(get_settings()))
        except db.SchemaError as exc:
            sys.exit(str(exc))
        if code is None:
            sys.exit("An admin account already exists; no setup code is needed.")
        print(code)
        return

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
