"""Builds an anonymised fixture database with the code of an older SlotServe release.

Run it with PYTHONPATH pointing at a checkout of the release (never at the current tree):

    git worktree add --detach <tmp>/v0.8.0 v0.8.0
    PYTHONPATH=<tmp>/v0.8.0/backend python make_fixture.py v0.8.0.sql
    git worktree remove <tmp>/v0.8.0

The release's own startup creates the database (exactly as a real deployment would), then every table gets
a few rows of synthetic data that follow the release's ORM: no real names, keys or addresses. The result is
written as an SQL dump (``PRAGMA user_version`` included) so reviews can read what the fixture contains.
"""

import asyncio
import hashlib
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import BigInteger, Boolean, Float, Integer, String, Text

ROWS = 3
BASE = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
PASSWORD = "fixture-pass"  # every fixture user's password

# (table, column) -> value(i); applied before the generic rules. Statuses must be ones the code understands.
SPECIAL = {
    ("users", "username"): lambda i: ["admin", "alice", "bob"][i],
    ("users", "is_admin"): lambda i: i == 0,
    ("users", "locale"): lambda i: ["en", "zh-CN", None][i],
    ("users", "notify_prefs"): lambda i: '{"reminders": true}',
    ("users", "ical_token"): lambda i: f"ical{i:060d}",
    ("api_keys", "prefix"): lambda i: f"sk-fx{i}",
    ("api_keys", "key_hash"): lambda i: hashlib.sha256(f"fixture-key-{i}".encode()).hexdigest(),
    ("api_keys", "revoked_at"): lambda i: BASE if i == 2 else None,
    ("models", "name"): lambda i: f"fixture-model-{i}-Q4_K_M",
    ("models", "file_path"): lambda i: f"fixture-model-{i}-Q4_K_M.gguf",
    ("models", "status"): lambda i: "ready",
    ("models", "is_default"): lambda i: i == 0,
    ("models", "args"): lambda i: "-c 4096",
    ("models", "engine"): lambda i: None,
    ("models", "deleted_at"): lambda i: None,
    ("model_presets", "args"): lambda i: "-c 8192",
    ("download_tasks", "url"): lambda i: f"https://example.invalid/fixture-{i}.gguf",
    ("download_tasks", "status"): lambda i: ["done", "failed", "cancelled"][i],
    ("reservations", "status"): lambda i: ["booked", "cancelled", "booked"][i],
    ("reservations", "series_id"): lambda i: None,
    ("reservations", "released_at"): lambda i: None,
    ("reservations", "reviewed_by"): lambda i: None,
    ("reservations", "review_note"): lambda i: None,
    ("usage_logs", "endpoint"): lambda i: "/v1/chat/completions",
    ("usage_logs", "status_code"): lambda i: 200,
    ("model_load_events", "success"): lambda i: i != 1,
    ("audit_logs", "action"): lambda i: ["user.register", "model.update", "settings.update"][i],
    ("audit_logs", "detail"): lambda i: "{}",
    ("waitlist", "status"): lambda i: ["waiting", "expired", "cancelled"][i],
    ("waitlist", "resolved_at"): lambda i: None,
    ("sent_notifications", "key"): lambda i: f"reminder:{i + 1}",
    ("usage_daily", "date"): lambda i: f"2025-12-0{i + 1}",
    ("usage_daily", "model_name"): lambda i: f"fixture-model-{i}-Q4_K_M",
    ("kv_store", "key"): lambda i: f"fixture-{i}",
    ("kv_store", "value"): lambda i: "{}",
    ("lanes", "gpu_indices"): lambda i: str(i + 1),
}
# Tables whose rows the release itself writes at startup or only transiently: left as startup made them.
SKIP = {"app_settings", "notification_outbox"}
SETTINGS = {"registration_open": "false", "max_reservation_hours": "8", "timezone": '"UTC"'}


def value(table, column, i, ids):
    if (table.name, column.name) in SPECIAL:
        return SPECIAL[(table.name, column.name)](i)
    if column.foreign_keys:
        target = next(iter(column.foreign_keys)).column.table.name
        pool = ids.get(target) or []
        return pool[i % len(pool)] if pool else None
    nullable_none = "_at" in column.name or column.name in ("error", "total_bytes", "key_encrypted")
    if column.nullable and column.name != "id" and nullable_none:
        return None
    kind = column.type
    if column.name.endswith("_at") or column.name.startswith(("start", "end")) or "datetime" in repr(kind).lower():
        hours = {"start_at": 24 * (i + 1), "end_at": 24 * (i + 1) + 2, "finished_at": 1}.get(column.name, 0)
        return BASE + timedelta(hours=hours)
    if isinstance(kind, Boolean):
        return bool(i % 2)
    if isinstance(kind, (Integer, BigInteger)):
        return (i + 1) * 10
    if isinstance(kind, Float) or repr(kind).startswith("Float"):
        return 1.5
    if isinstance(kind, (String, Text)):
        text = f"{column.name}-{i}"
        length = getattr(kind, "length", None)
        return text[:length] if length else text
    return None


async def build(data_dir: Path) -> Path:
    from slotserve import db, orm
    from slotserve.config import get_settings
    from slotserve.main import create_app
    from slotserve.security import hash_password

    get_settings.cache_clear()
    app = create_app(start_background=False)
    async with app.router.lifespan_context(app):  # the release creates its schema the way it does in production
        pass

    db.configure(get_settings().database_url)
    password = hash_password(PASSWORD)
    ids: dict[str, list] = {}
    async with db._engine.begin() as conn:
        for table in db.Base.metadata.sorted_tables:
            existing = [r[0] for r in (await conn.exec_driver_sql(f'SELECT rowid FROM "{table.name}"')).all()]
            if table.name in SKIP:
                continue
            # the default lane exists already: add one more
            start = len(existing) if table.name == "lanes" and existing else 0
            rows = []
            for i in range(start, ROWS if table.name != "lanes" else start + 1):
                row = {}
                for column in table.columns:
                    if column.primary_key and isinstance(column.type, Integer) and len(table.primary_key) == 1:
                        continue
                    if table.name == "users" and column.name == "password_hash":
                        row[column.name] = password
                        continue
                    if table.name == "lanes" and column.name == "name":
                        row[column.name] = f"Fixture GPU {i + 1}"
                        continue
                    row[column.name] = value(table, column, i % ROWS, ids)
                rows.append(row)
            if rows:
                await conn.execute(table.insert(), rows)
            ids[table.name] = [
                r[0] for r in (await conn.exec_driver_sql(f'SELECT rowid FROM "{table.name}" ORDER BY rowid')).all()
            ]
        for key, val in SETTINGS.items():
            await conn.exec_driver_sql(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, val, BASE.replace(tzinfo=None)),
            )
    await db.dispose()
    del orm
    return data_dir / "slotserve.db"


def main() -> None:
    out = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ.update(
            SLOTSERVE_DATA_DIR=str(tmp_path / "data"),
            SLOTSERVE_MODELS_DIR=str(tmp_path / "models"),
            SLOTSERVE_INVITE_CODE="fixture-invite",
            SLOTSERVE_SECRET_KEY="fixture-secret",
            SLOTSERVE_LLAMA_SERVER_CMD="fixture-no-llama",
        )
        cwd = Path.cwd()
        os.chdir(tmp_path)  # never pick up a developer's .env
        try:
            path = asyncio.run(build(tmp_path / "data"))
        finally:
            os.chdir(cwd)
        con = sqlite3.connect(path)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        lines = [f"PRAGMA user_version={version};", *con.iterdump()]
        con.close()
        out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {out} (schema v{version}, {len(lines)} lines)")


if __name__ == "__main__":
    main()
