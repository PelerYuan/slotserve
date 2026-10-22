"""The built-in health checks (registered on import by ``health.HealthMonitor``).

Each check reads state that its feature already keeps (lane runtimes, the storage checks, chat connections, the
backup status) or probes something cheap (Docker's version, free disk space, a database write); none of them
changes anything. Titles are in ``TITLES`` (English, Chinese); the frontend may translate ``checks.<title_key>``.
"""

import asyncio
import json
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, text

from ..db import SessionLocal, utcnow
from ..orm import ApiKey, Lane, LlmModel, Storage, User
from ..runtime.engines import split_args
from ..security import decrypt_api_key
from . import maintenance
from .health import TASKS, CheckResult, Ctx, register_check, register_titles

GB = 1e9
CHAT_LABELS = {"telegram": "Telegram", "feishu": "Feishu", "dingtalk": "DingTalk", "wecom": "WeCom", "qq": "QQ"}

TITLES: dict[str, tuple[str, str]] = {
    # generic
    "check_timeout": ("Check {check} did not finish within {seconds} s", "检查项 {check} 在 {seconds} 秒内没有完成"),
    "check_crashed": ("Check {check} failed to run", "检查项 {check} 运行出错"),
    "component_ok": ("{component} is fine", "{component} 正常"),
    "component_problem": ("{component} has a problem", "{component} 出现问题"),
    # database and disk
    "database_ok": ("The database is writable", "数据库可正常写入"),
    "database_error": ("The database cannot be written", "数据库无法写入"),
    "disk_ok": ("Enough free disk space ({detail})", "磁盘空间充足（{detail}）"),
    "disk_low": ("Low disk space: {detail}", "磁盘空间不足：{detail}"),
    "disk_full": ("Disk almost full: {detail}", "磁盘即将写满：{detail}"),
    # inference engine, lanes, models, GPUs
    "engine_found": ("Inference engine found: {command}", "已找到推理引擎：{command}"),
    "engine_missing": ("Cannot find the inference engine program: {command}", "找不到推理引擎程序：{command}"),
    "lanes_ok": ("{count} lane(s) working", "{count} 个通道工作正常"),
    "lane_failed": ("Lane {lanes} failed to load its model", "通道 {lanes} 加载模型失败"),
    "models_none": ("No model added yet", "还没有添加模型"),
    "default_model_unset": ("No idle default model on lane {lanes}", "通道 {lanes} 没有设置空闲时的默认模型"),
    "models_ok": ("{count} model(s); idle default set", "共 {count} 个模型，已设置空闲默认模型"),
    "gpu_ok": ("{count} GPU(s): {names}", "{count} 张 GPU：{names}"),
    "gpu_none": ("No GPU detected (nvidia-smi reports none)", "没有检测到 GPU（nvidia-smi 没有返回任何 GPU）"),
    # dev environments and storage
    "devenv_off": ("Dev environments are off", "开发环境未开启"),
    "docker_ok": ("Docker is reachable (version {version})", "Docker 可正常访问（版本 {version}）"),
    "docker_unreachable": ("Docker is not reachable", "无法连接 Docker"),
    "storage_ok": ("{count} storage(s) working", "{count} 个存储工作正常"),
    "storage_down": ("Storage {names} cannot be used", "存储 {names} 无法使用"),
    "storage_unchecked": ("Storage {names} has not been checked yet", "存储 {names} 尚未检查"),
    # chat platforms
    "chat_none": (
        "No chat platform connected: alerts are only shown in SlotServe",
        "未连接任何聊天平台：告警只在 SlotServe 站内可见",
    ),
    "chat_ok": ("{connected} of {enabled} chat platform(s) connected", "已连接 {connected}/{enabled} 个聊天平台"),
    "chat_down": ("Chat platform {names} is not connected", "聊天平台 {names} 未连接"),
    # backups
    "backup_off": ("Automatic backups are off", "自动备份已关闭"),
    "backup_failed": ("The last backup failed", "最近一次备份失败"),
    "backup_copy_failed": ("Copying the backup to {target} failed", "备份复制到 {target} 失败"),
    "backup_target_invalid": ("The backup target {target} is not valid", "备份目标 {target} 无效"),
    "backup_same_disk": (
        "Backups are only on this machine's disk; set a backup target to keep a copy elsewhere",
        "备份只保存在本机磁盘上；请设置备份目标，在别处保留一份",
    ),
    "backup_ok": ("Last backup {age} ago, copied to {target}", "最近一次备份在 {age} 前，已复制到 {target}"),
    "backup_none": ("No backup yet", "还没有备份"),
    "backup_stale": ("The newest backup is {hours} hours old", "最新的备份已是 {hours} 小时前"),
    "backup_fresh": ("The newest backup is {hours} hours old", "最新的备份是 {hours} 小时前"),
    "restore_waiting": (
        "Restored from {bundle}: waiting for Docker to compare",
        "已从 {bundle} 恢复：等待 Docker 可用后核对",
    ),
    "restore_report": (
        "Restored from {bundle}: {containers_missing} environment(s) missing, {containers_unknown} unknown "
        "container(s), {models_missing} model file(s) missing",
        "已从 {bundle} 恢复：{containers_missing} 个环境缺失，{containers_unknown} 个未知容器，"
        "{models_missing} 个模型文件缺失",
    ),
    "restore_clean": ("Restored from {bundle}: everything matches", "已从 {bundle} 恢复：数据与实际状态一致"),
    "migration_rows_dropped": (
        "The last upgrade could not keep {count} row(s)",
        "最近一次升级有 {count} 行数据无法保留",
    ),
    # background tasks
    "tasks_ok": ("{count} background task(s) running", "{count} 个后台任务运行正常"),
    "task_stopped": ("Background task {names} stopped", "后台任务 {names} 已停止"),
    "task_failing": ("Background task {names} keeps failing", "后台任务 {names} 持续出错"),
    "task_stalled": (
        "Background task {names} has not finished a round for {minutes} min",
        "后台任务 {names} 已 {minutes} 分钟没有完成一轮",
    ),
    # security
    "secrets_ok": ("Stored secrets can be decrypted", "已保存的密钥均可解密"),
    "secret_undecryptable": (
        "{count} stored secret(s) cannot be decrypted with the current secret key",
        "{count} 项已保存的密钥无法用当前的会话密钥解密",
    ),
    "security_devenv_root": (
        "Dev environments are on: SlotServe controls this host as root; {admins} admin(s)",
        "开发环境已开启：SlotServe 拥有宿主机 root 权限；管理员 {admins} 人",
    ),
    "security_ok": ("{admins} admin(s)", "管理员 {admins} 人"),
    "invite_unset": (
        "No invite code: nobody else can sign up yet",
        "尚未设置邀请码：其他人暂时无法注册",
    ),
    "invite_set": ("Invite code set", "已设置邀请码"),
}


def _age(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} h" if hours >= 1 else f"{max(delta.total_seconds() / 60, 0):.0f} min"


def _settings_link(key: str) -> dict[str, str]:
    return {"kind": "setting", "target": key}


# ---- database and disk -------------------------------------------------------------


async def check_database(ctx: Ctx) -> CheckResult:
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        probe = Path(ctx.settings.data_dir)
        await asyncio.to_thread(_write_probe, probe)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("error", "database_error", evidence=f"{type(exc).__name__}: {exc}")
    return CheckResult("ok", "database_ok")


def _write_probe(folder: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=folder, prefix=".slotserve-write-", delete=True) as f:
        f.write(b"ok")
        f.flush()


async def check_disk(ctx: Ctx) -> CheckResult:
    s = ctx.settings
    warn_gb = max(2 * float(s.download_min_free_gb), 5.0)
    places = [("data", s.data_dir), ("models", s.models_dir)]
    seen: set[tuple[int, int]] = set()
    rows, level = [], "ok"
    for name, path in places:
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, path)
        except OSError:
            continue
        key = (usage.total, usage.free)
        if key in seen:  # same file system as an earlier place
            continue
        seen.add(key)
        free = usage.free / GB
        row_level = "error" if free < 1 or usage.free < usage.total * 0.01 else "warn" if free < warn_gb else "ok"
        rows.append(
            {"name": name, "path": str(path), "free_gb": round(free, 1), "total_gb": round(usage.total / GB, 1)}
        )
        level = max(level, row_level, key=["ok", "warn", "error"].index)
    async with SessionLocal() as db:
        storages = (await db.execute(select(Storage).where(Storage.enabled.is_(True)))).scalars().all()
    for st in storages:  # free space the storage check measured
        status = maintenance_json(st.status)
        if status.get("ok") and status.get("free") is not None and status.get("total"):
            free = status["free"] / GB
            row_level = "warn" if free < warn_gb or status["free"] < status["total"] * 0.05 else "ok"
            rows.append(
                {"name": f"storage {st.name}", "free_gb": round(free, 1), "total_gb": round(status["total"] / GB, 1)}
            )
            level = max(level, row_level, key=["ok", "warn", "error"].index)
    detail = ", ".join(f"{r['name']} {r['free_gb']} GB free" for r in rows)
    key = {"ok": "disk_ok", "warn": "disk_low", "error": "disk_full"}[level]
    return CheckResult(level, key, {"detail": detail, "places": rows, "threshold_gb": warn_gb})


def maintenance_json(value: str | None) -> dict:
    try:
        data = json.loads(value) if value else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---- engine, lanes, models, GPUs ---------------------------------------------------------


async def check_engine(ctx: Ctx) -> CheckResult:
    cmd = ctx.settings.llama_server_cmd
    try:
        program = split_args(cmd)[0]
    except (ValueError, IndexError):
        program = cmd
    found = shutil.which(program) or (program if Path(program).is_file() else None)
    if found:
        return CheckResult("ok", "engine_found", {"command": program})
    return CheckResult(
        "error",
        "engine_missing",
        {"command": program},
        evidence=f"SLOTSERVE_LLAMA_SERVER_CMD={cmd}: {program} not found on PATH",
        actions=[{"kind": "docs", "target": "configuration#llama-server"}],
    )


async def check_lanes(ctx: Ctx) -> CheckResult | None:
    lanes = ctx.state.lanes.ordered() if getattr(ctx.state, "lanes", None) is not None else []
    if not lanes:
        return None
    failed = [(c.name, c.runtime.snapshot()) for c in lanes]
    failed = [(name, snap) for name, snap in failed if snap.state.value == "failed"]
    if failed:
        evidence = "\n".join(f"{name}: {snap.model_name or '?'}: {snap.error or ''}".strip() for name, snap in failed)
        return CheckResult(
            "error",
            "lane_failed",
            {"lanes": [n for n, _ in failed], "count": len(failed)},
            evidence=evidence,
            actions=[{"kind": "link", "target": "/admin?tab=runtime"}],
        )
    return CheckResult("ok", "lanes_ok", {"count": len(lanes)})


async def check_models(ctx: Ctx) -> CheckResult:
    async with SessionLocal() as db:
        count = (await db.execute(select(func.count(LlmModel.id)).where(LlmModel.deleted_at.is_(None)))).scalar_one()
        unset = (await db.execute(select(Lane.name).where(Lane.default_model_id.is_(None)))).scalars().all()
    if not count:
        return CheckResult("info", "models_none", actions=[{"kind": "link", "target": "/models"}])
    if unset:
        return CheckResult(
            "info",
            "default_model_unset",
            {"lanes": list(unset), "count": count},
            actions=[{"kind": "link", "target": "/admin?tab=lanes"}],
            dismissible=True,
        )
    return CheckResult("ok", "models_ok", {"count": count})


async def check_gpu(ctx: Ctx) -> CheckResult:
    sampler = getattr(ctx.state, "usage_sampler", None) if ctx.state is not None else None
    if sampler is not None:
        gpus = await sampler.stats()
    else:
        from ..runtime.gpu import gpu_stats

        gpus = await gpu_stats()
    if not gpus:
        return CheckResult("warn", "gpu_none", dismissible=True)
    names = sorted({g.name for g in gpus if g.name})
    return CheckResult("ok", "gpu_ok", {"count": len(gpus), "names": names})


# ---- dev environments and storage ---------------------------------------------------------


async def check_docker(ctx: Ctx) -> CheckResult:
    if not ctx.settings.devenv_enabled:
        return CheckResult("info", "devenv_off", actions=[_settings_link("devenv_enabled")], dismissible=True)
    devenv = getattr(ctx.state, "devenv", None) if ctx.state is not None else None
    if devenv is not None:
        status = await devenv.refresh_status(force=True)
        ok, version, error = status.get("ok"), status.get("version"), status.get("error")
    elif ctx.docker is not None:
        try:
            version = (await ctx.docker.version()).get("Version", "?")
            ok, error = True, None
        except Exception as exc:  # noqa: BLE001
            ok, version, error = False, None, f"{type(exc).__name__}: {exc}"
    else:
        return CheckResult("error", "docker_unreachable", evidence="no Docker client")
    if ok:
        return CheckResult("ok", "docker_ok", {"version": version or "?"})
    return CheckResult(
        "error",
        "docker_unreachable",
        {"host": ctx.settings.docker_host},
        evidence=error or "",
        actions=[{"kind": "docs", "target": "dev-environments#docker"}],
    )


async def check_storage(ctx: Ctx) -> CheckResult | None:
    async with SessionLocal() as db:
        rows = (await db.execute(select(Storage).where(Storage.enabled.is_(True)).order_by(Storage.id))).scalars().all()
    if not rows:
        return None
    down, unchecked, evidence = [], [], []
    for st in rows:
        status = maintenance_json(st.status)
        if not status:
            unchecked.append(st.name)
        elif not status.get("ok"):
            down.append(st.name)
            evidence.append(f"{st.name}: {(status.get('error') or '').strip()[:500]}")
    action = [{"kind": "link", "target": "/admin?tab=storage"}]
    if down:
        return CheckResult("error", "storage_down", {"names": down}, evidence="\n".join(evidence), actions=action)
    if unchecked:
        return CheckResult("info", "storage_unchecked", {"names": unchecked}, actions=action)
    return CheckResult("ok", "storage_ok", {"count": len(rows)})


# ---- chat platforms -------------------------------------------------------------------


async def check_chat(ctx: Ctx) -> CheckResult:
    hub = getattr(ctx.state, "channels", None) if ctx.state is not None else None
    action = [{"kind": "link", "target": "/admin?tab=channels"}]
    if hub is not None:
        enabled = [c for c in hub if c.enabled]
        webhooks = hub.webhooks.configured_ids()
        down = [c for c in enabled if not c.connected and c.error]
        if not enabled and not webhooks:
            return CheckResult("info", "chat_none", actions=action, dismissible=True)
        if down:
            return CheckResult(
                "warn",
                "chat_down",
                {"names": [CHAT_LABELS.get(c.id, c.id) for c in down]},
                evidence="\n".join(f"{c.id}: {c.error}" for c in down),
                actions=action,
            )
        connected = sum(1 for c in enabled if c.connected)
        return CheckResult(
            "ok", "chat_ok", {"connected": connected, "enabled": len(enabled), "webhooks": len(webhooks)}
        )
    s = ctx.settings  # slotserve doctor: what is configured, not whether it connects
    configured = [cid for cid in ("feishu", "dingtalk", "wecom", "qq") if getattr(s, f"{cid}_enabled", False)]
    if s.telegram_bot_token:
        configured.append("telegram")
    hooks = [k for k in ("feishu_webhook_url", "dingtalk_webhook_url", "wecom_webhook_url") if getattr(s, k, "")]
    if not configured and not hooks:
        return CheckResult("info", "chat_none", actions=action, dismissible=True)
    return CheckResult("ok", "chat_ok", {"connected": "?", "enabled": len(configured), "webhooks": len(hooks)})


# ---- backups ---------------------------------------------------------------------------


async def check_backup(ctx: Ctx) -> CheckResult:
    s = ctx.settings
    action = [_settings_link("backup_target")]
    if s.backup_keep <= 0:
        return CheckResult("warn", "backup_off", actions=[_settings_link("backup_keep")], dismissible=True)
    async with SessionLocal() as db:
        status = await maintenance.load_status(db)
    target = (getattr(s, "backup_target", "") or "").strip()
    if status.get("last_error"):
        return CheckResult(
            "error",
            "backup_failed",
            {"at": status.get("last_error_at")},
            evidence=status["last_error"],
            actions=[{"kind": "retry", "target": "backup"}],
        )
    if target:
        try:
            maintenance.parse_target(target)
        except maintenance.BackupError:
            return CheckResult("error", "backup_target_invalid", {"target": target}, actions=action)
    offsite = status.get("offsite") or {}
    if target and offsite.get("last_error") and offsite.get("target") == target:
        return CheckResult(
            "error",
            "backup_copy_failed",
            {"target": target, "at": offsite.get("last_error_at")},
            evidence=offsite["last_error"],
            actions=action,
        )
    last = maintenance.newest(s)
    if last is None:
        return CheckResult("info", "backup_none")
    age = _age(ctx.now - last)
    if not target:
        return CheckResult(
            "warn", "backup_same_disk", {"age": age, "last_at": last.isoformat()}, actions=action, dismissible=True
        )
    if offsite.get("target") != target or not offsite.get("last_ok_at"):
        return CheckResult("info", "backup_ok", {"age": age, "target": target, "copied": False})
    return CheckResult(
        "ok",
        "backup_ok",
        {
            "age": age,
            "target": target,
            "copied": True,
            "copied_at": offsite.get("last_ok_at"),
            "where": offsite.get("where"),
        },
    )


async def check_backup_age(ctx: Ctx) -> CheckResult | None:
    s = ctx.settings
    if s.backup_keep <= 0:
        return None
    last = maintenance.newest(s)
    if last is None:
        return None  # check backup says "no backup yet"; the first one is taken right after the start
    hours = (ctx.now - last).total_seconds() / 3600
    params = {"hours": int(hours), "last_at": last.isoformat()}
    if hours > maintenance.STALE_HOURS:
        return CheckResult("error", "backup_stale", params, actions=[{"kind": "retry", "target": "backup"}])
    return CheckResult("ok", "backup_fresh", params)


async def check_restore(ctx: Ctx) -> CheckResult | None:
    s = ctx.settings
    pending = maintenance.restore_pending(s)
    if pending is None:
        return None
    report = maintenance.restore_report(s)
    if report is None:
        docker = ctx.docker
        async with SessionLocal() as db:
            report = await maintenance.reconcile(s, docker, db)
        if report.get("docker") == "unreachable":
            return CheckResult(
                "warn", "restore_waiting", {"bundle": pending.get("bundle")}, evidence=report.get("docker_error", "")
            )
        report["bundle"] = pending.get("bundle")
        report["restore"] = pending
        maintenance.write_restore_report(s, report)
    summary = report.get("summary") or {}
    params = {"bundle": pending.get("bundle"), **summary}
    lines = [f"missing: {c['kind']} {c['name']} (container {c['container']})" for c in report["containers_missing"]]
    lines += [f"unknown container: {c['name']} ({c['container']})" for c in report["containers_unknown"]]
    lines += [f"model file missing: {m['name']}: {m['path']}" for m in report["models_missing"]]
    lines += [f"warning: {w}" for w in pending.get("warnings") or []]
    clean = not any(summary.values())
    return CheckResult(
        "info" if clean else "warn",
        "restore_clean" if clean else "restore_report",
        params,
        evidence="\n".join(lines),
        dismissible=True,
    )


async def check_migration(ctx: Ctx) -> CheckResult | None:
    from ..db import REPORT_NAME

    path = Path(ctx.settings.data_dir) / REPORT_NAME
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    dropped = int(report.get("dropped_rows") or 0)
    if not dropped:
        return None
    lines = []
    for step in report.get("steps") or []:
        for table, ids in (step.get("dropped") or {}).items():
            lines.append(f"{step.get('name')}: {table}: {ids[:20]}")
    return CheckResult(
        "warn",
        "migration_rows_dropped",
        {"count": dropped, "path": str(path), "from": report.get("from"), "to": report.get("to")},
        evidence="\n".join(lines),
        dismissible=True,
    )


# ---- background tasks ------------------------------------------------------------------


async def check_tasks(ctx: Ctx) -> CheckResult | None:
    if not TASKS:
        return None
    now = utcnow()
    stopped = [t for t in TASKS.values() if t.state in ("stopped", "failed", "restarting")]
    stalled = []
    for t in TASKS.values():
        if t.state == "running" and t.beat_every and t.last_beat is not None:
            limit = max(3 * t.beat_every, 120)
            if (now - t.last_beat).total_seconds() > limit:
                stalled.append(t)
    failing = [t for t in TASKS.values() if t.state == "running" and t.recent_errors(now) >= 3]

    def evidence(tasks) -> str:
        return "\n".join(f"{t.name}: {t.last_error or t.state}" for t in tasks)

    if stopped:
        return CheckResult("error", "task_stopped", {"names": [t.name for t in stopped]}, evidence=evidence(stopped))
    if stalled:
        minutes = int(max((now - t.last_beat).total_seconds() for t in stalled) / 60)
        return CheckResult(
            "error",
            "task_stalled",
            {"names": [t.name for t in stalled], "minutes": minutes},
            evidence=evidence(stalled),
        )
    if failing:
        return CheckResult(
            "warn",
            "task_failing",
            {"names": [t.name for t in failing], "errors": {t.name: t.recent_errors(now) for t in failing}},
            evidence=evidence(failing),
        )
    return CheckResult("ok", "tasks_ok", {"count": len(TASKS)})


# ---- security -------------------------------------------------------------------------


async def check_secrets(ctx: Ctx) -> CheckResult:
    key = ctx.settings.secret_key
    bad: list[str] = []
    async with SessionLocal() as db:
        for _sid, name, enc in (
            await db.execute(
                select(Storage.id, Storage.name, Storage.password_enc).where(Storage.password_enc.is_not(None))
            )
        ).all():
            if enc and decrypt_api_key(enc, key) is None:
                bad.append(f"storage {name} password")
        keys = (
            await db.execute(
                select(ApiKey.id, ApiKey.name).where(ApiKey.key_encrypted.is_not(None), ApiKey.revoked_at.is_(None))
            )
        ).all()
        if keys:
            rows = (
                await db.execute(select(ApiKey.id, ApiKey.key_encrypted).where(ApiKey.key_encrypted.is_not(None)))
            ).all()
            bad += [f"API key #{kid}" for kid, enc in rows if enc and decrypt_api_key(enc, key) is None]
    if bad:
        return CheckResult(
            "error",
            "secret_undecryptable",
            {"count": len(bad)},
            evidence="\n".join(bad[:50]),
            actions=[{"kind": "docs", "target": "operations#secret-key"}],
        )
    return CheckResult("ok", "secrets_ok")


async def check_security(ctx: Ctx) -> CheckResult:
    async with SessionLocal() as db:
        admins = (
            await db.execute(select(func.count(User.id)).where(User.is_admin.is_(True), User.disabled_at.is_(None)))
        ).scalar_one()
    if ctx.settings.devenv_enabled:
        return CheckResult("info", "security_devenv_root", {"admins": admins}, dismissible=True)
    return CheckResult("ok", "security_ok", {"admins": admins})


async def check_invite(ctx: Ctx) -> CheckResult:
    if (ctx.settings.invite_code or "").strip() not in ("", "change-me"):  # as api/auth.invite_code_usable
        return CheckResult("ok", "invite_set")
    return CheckResult("info", "invite_unset", actions=[_settings_link("invite_code")], dismissible=True)


def register_builtin() -> None:
    register_titles(TITLES)
    register_check("database", group="system", run=check_database, every_s=60)
    register_check("disk", group="disk", run=check_disk, every_s=300, alert_level="warn")
    register_check(
        "engine",
        group="engine",
        run=check_engine,
        every_s=300,
        onboarding=True,
        doc_anchor="configuration#llama-server",
    )
    # load failures are announced by load_failed; the check only shows them
    register_check("lanes", group="engine", run=check_lanes, every_s=15, alert=False, server_only=True)
    register_check("models", group="engine", run=check_models, every_s=60, onboarding=True, alert=False)
    register_check("gpu", group="gpu", run=check_gpu, every_s=300, onboarding=True, alert=False)
    register_check(
        "docker",
        group="devenv",
        run=check_docker,
        every_s=30,
        onboarding=True,
        alert_after_s=120,
        doc_anchor="dev-environments#docker",
    )
    # storage_down is announced by the storage service itself
    register_check("storage", group="storage", run=check_storage, every_s=60, alert=False)
    register_check("chat", group="chat", run=check_chat, every_s=60, onboarding=True, alert=False)
    register_check(
        "backup", group="backup", run=check_backup, every_s=60, onboarding=True, doc_anchor="operations#backups"
    )
    register_check("backup_age", group="backup", run=check_backup_age, every_s=300, doc_anchor="operations#backups")
    register_check(
        "restore", group="backup", run=check_restore, every_s=60, alert=False, doc_anchor="operations#restore"
    )
    register_check("migration", group="system", run=check_migration, every_s=3600, alert=False)
    register_check("tasks", group="tasks", run=check_tasks, every_s=15, server_only=True)
    register_check("secrets", group="security", run=check_secrets, every_s=3600)
    register_check("security", group="security", run=check_security, every_s=300, alert=False)
    register_check("invite", group="security", run=check_invite, every_s=300, onboarding=True, alert=False)


ON_DISMISS = {"restore": maintenance.finish_restore}

register_builtin()
