"""The settings registry (A04): every setting with its metadata, and the admin overrides of the editable ones.

Values from the environment / ``.env`` are the defaults. An override is stored in the ``app_settings`` table and
applied onto the shared ``Settings`` object, so every reader of ``get_settings()`` (and the runtime) sees it
immediately.

``REGISTRY`` describes every ``Settings`` field, in display order. One entry drives the admin UI (group, tier,
policy card, ``depends_on``, search keywords), safety (``secret``: never in the audit log or an export, ``danger``:
equivalent to root on the host), the change preview (``impact``, see ``settings_impact.py``) and the generated
reference ``docs/configuration*.md`` (``doc``, ``tools/gen_settings_doc.py``). Entries with
``restart_required=True`` are deployment parameters: read-only in the UI, set in ``.env``.

Adding a setting: a field in ``config.Settings`` + one entry here (+ labels in the frontend i18n), then run
``python tools/gen_settings_doc.py``.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..orm import AppSetting
from ..runtime.engines import split_args
from . import settings_impact

Tier = Literal["basic", "advanced", "expert"]

# Root-equivalent settings ("danger": "root") need sudo mode (password entered within the last 5 minutes, A06) to be
# changed - once the web UI can ask for the password (W1-07). Until then the change is allowed but audited as
# ``settings.sensitive_update`` and announced to all admins. W1-07 flips this to True when it ships the prompt.
ENFORCE_SUDO_FOR_DANGER = False

# Groups in display order: (id, English heading, Chinese heading). The frontend has its own labels for the ids.
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("access", "Sign-up and access", "注册与访问"),
    ("booking", "Booking and fair use", "预约与公平使用"),
    ("notify", "Notifications and time", "通知与时间"),
    ("devenv", "Dev environments", "开发环境"),
    ("models", "Models and downloads", "模型与下载"),
    ("runtime", "Model loading", "模型加载"),
    ("engines", "Engines", "推理后端"),
    ("feishu", "Feishu / Lark", "飞书 / Lark"),
    ("dingtalk", "DingTalk", "钉钉"),
    ("wecom", "WeCom", "企业微信"),
    ("qq", "QQ", "QQ"),
    ("webhooks", "Group webhooks", "群机器人 Webhook"),
    ("monitor", "Monitoring", "监控"),
    ("maintenance", "Housekeeping and backups", "清理与备份"),
    ("deployment", "Deployment (restart to apply)", "部署参数（重启后生效）"),
)
# Policy cards of the basic view: (id, English title, Chinese title).
CARDS: tuple[tuple[str, str, str], ...] = (
    ("join", "Who can join", "谁能加入"),
    ("fairness", "Fair use", "公平使用"),
    ("notify", "Notifications", "通知"),
    ("devenv", "Dev environments", "开发环境"),
    ("models", "Models", "模型"),
)

ImpactFn = Callable[[AsyncSession, Settings, str, Any, dict], Awaitable[dict]]


@dataclass(frozen=True)
class Editable:
    type: type
    min: float | None = None
    max: float | None = None
    secret: bool = False  # never returned in the audit log or an export; the audit log keeps a fingerprint
    unit: str | None = None  # s | min | h | d | GB | MB/s | CPU | % - shown next to the input
    choices: tuple | None = None
    group: str = "other"
    tier: Tier = "advanced"
    card: str | None = None  # policy card of the basic view
    depends_on: str | None = None  # "key" (hidden while that setting is off/empty) or "key=value"
    keywords: tuple[str, ...] = ()  # extra search words (labels and the variable name are searched anyway)
    scope: Literal["global", "node", "profile"] = "global"
    danger: Literal["root"] | None = None  # changing it can run commands on the host as root
    restart_required: bool = False  # deployment parameter: set in .env, read-only in the UI
    impact: ImpactFn | None = None  # dry-run: who and which bookings a change affects
    doc: tuple[str, str] = ("", "")  # (English, Chinese) description for docs/configuration*.md
    doc_default: tuple[str, str] | None = None  # shown instead of the default value in the docs

    @property
    def kind(self) -> str:
        return {bool: "bool", int: "int", float: "float"}.get(self.type, "str")


def env_var(key: str) -> str:
    return "SLOTSERVE_" + key.upper()


_ROOT = "root"
_IMP_QUOTA = settings_impact.quota_weekly_hours
_IMP_UPCOMING = settings_impact.quota_upcoming
_IMP_LENGTH = settings_impact.max_reservation_hours
_IMP_ADVANCE = settings_impact.max_advance_days
_IMP_APPROVAL = settings_impact.approval

REGISTRY: dict[str, Editable] = {
    # ---- access ------------------------------------------------------------------------------------------------
    "registration_open": Editable(
        bool,
        group="access",
        tier="basic",
        card="join",
        keywords=("sign up", "register", "注册"),
        doc=("Allow sign-up with the invite code", "是否允许凭邀请码注册"),
    ),
    "invite_code": Editable(
        str,
        min=1,
        max=128,
        secret=True,
        group="access",
        tier="basic",
        card="join",
        depends_on="registration_open",
        keywords=("invite", "邀请"),
        doc=(
            "Code required to sign up; while it is `change-me` (or empty) only the first admin can sign up, with "
            "the [setup code](#first-admin-and-setup-code)",
            "注册所需邀请码；为 `change-me`（或为空）时只有第一个管理员能注册"
            "（使用[初始化码](#初始化码与第一个管理员)）",
        ),
    ),
    "approval_mode": Editable(
        str,
        choices=("off", "long", "all"),
        group="access",
        tier="basic",
        card="join",
        keywords=("approve", "review", "审批"),
        impact=_IMP_APPROVAL,
        doc=(
            "`off`, `long` or `all`: which bookings need an admin's approval",
            "`off`、`long` 或 `all`：哪些预约需要管理员审批",
        ),
    ),
    "approval_hours": Editable(
        int,
        min=1,
        max=24 * 7,
        unit="h",
        group="access",
        depends_on="approval_mode=long",
        impact=_IMP_APPROVAL,
        doc=("With `long`, bookings longer than this need approval", "`long` 模式下，超过这个时长的预约需要审批"),
    ),
    "api_key_reveal": Editable(
        bool,
        group="access",
        keywords=("api key", "密钥"),
        doc=(
            "Keep an encrypted copy of new API keys so owners can view them again",
            "为新建的 API 密钥保存加密副本，所有者之后可以再次查看",
        ),
    ),
    # ---- booking -----------------------------------------------------------------------------------------------
    "quota_weekly_hours": Editable(
        int,
        min=0,
        max=24 * 7,
        unit="h",
        group="booking",
        tier="basic",
        card="fairness",
        keywords=("quota", "limit", "配额", "每周", "时长"),
        impact=_IMP_QUOTA,
        doc=(
            "Hours one person may book per week (0 = unlimited; admins exempt; admins can grant extra hours per "
            "person)",
            "每人每周可预约小时数（0 不限，管理员不受限；管理员可为个人临时增加额度）",
        ),
    ),
    "max_reservation_hours": Editable(
        int,
        min=1,
        max=24 * 7,
        unit="h",
        group="booking",
        tier="basic",
        card="fairness",
        keywords=("longest", "length", "最长"),
        impact=_IMP_LENGTH,
        doc=("Longest single booking", "单次预约最长时长"),
    ),
    "max_advance_days": Editable(
        int,
        min=1,
        max=365,
        unit="d",
        group="booking",
        tier="basic",
        card="fairness",
        keywords=("ahead", "advance", "提前"),
        impact=_IMP_ADVANCE,
        doc=("How far ahead people can book", "最多可提前几天预约"),
    ),
    "quota_upcoming": Editable(
        int,
        min=0,
        max=100,
        group="booking",
        keywords=("quota", "配额"),
        impact=_IMP_UPCOMING,
        doc=("Open bookings one person may hold (0 = unlimited)", "每人可持有的未结束预约数（0 不限）"),
    ),
    "slot_minutes": Editable(
        int,
        unit="min",
        choices=(5, 10, 15, 20, 30, 60),
        group="booking",
        keywords=("calendar", "日历"),
        doc=("Calendar granularity", "日历时间粒度"),
    ),
    "reminder_minutes": Editable(
        int,
        min=1,
        max=120,
        unit="min",
        group="booking",
        doc=("Lead time of start and end reminders", "开始和结束提醒的提前量（分钟）"),
    ),
    # ---- notify ------------------------------------------------------------------------------------------------
    "timezone": Editable(
        str,
        min=1,
        max=64,
        group="notify",
        tier="basic",
        card="notify",
        keywords=("time zone", "时区"),
        doc=("IANA time zone for notifications and weekly quotas", "IANA 时区，用于通知和每周配额"),
    ),
    "public_url": Editable(
        str,
        max=300,
        group="notify",
        tier="basic",
        card="notify",
        keywords=("url", "address", "地址"),
        doc=(
            "Address people use to reach SlotServe; used in links and to recognise SlotServe's own pages",
            "大家访问 SlotServe 的地址，用于链接及识别 SlotServe 自己的页面",
        ),
    ),
    "chat_default_locale": Editable(
        str,
        choices=("en", "zh-CN"),
        group="notify",
        keywords=("language", "语言"),
        doc=(
            "`en` or `zh-CN`: language of group webhooks, group chats and chats not linked yet",
            "`en` 或 `zh-CN`：群机器人 Webhook、群聊和尚未绑定的聊天使用的语言",
        ),
    ),
    "telegram_bot_token": Editable(
        str,
        max=200,
        secret=True,
        group="notify",
        keywords=("telegram",),
        doc=("Telegram bot token; empty disables the bot", "Telegram 机器人 Token，留空则不启用"),
    ),
    "telegram_proxy": Editable(
        str,
        max=300,
        group="notify",
        depends_on="telegram_bot_token",
        keywords=("telegram", "proxy", "代理"),
        doc=("`http://` or `socks5://` proxy for Telegram", "连接 Telegram 的 `http://` 或 `socks5://` 代理"),
    ),
    # ---- devenv ------------------------------------------------------------------------------------------------
    "devenv_enabled": Editable(
        bool,
        group="devenv",
        tier="basic",
        card="devenv",
        keywords=("docker", "ssh", "container", "容器"),
        doc=(
            "Dev environments: book GPU time as a Docker container with SSH",
            "开发环境：把 GPU 时段预约成带 SSH 的 Docker 容器",
        ),
    ),
    "devenv_cpu_enabled": Editable(
        bool,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("cpu", "no gpu", "无 GPU"),
        doc=(
            "Let people start dev environments without a GPU (no booking)",
            "允许以无 GPU 模式启动开发环境（无需预约）",
        ),
    ),
    "devenv_cpu_per_user": Editable(
        int,
        min=1,
        max=100,
        group="devenv",
        tier="basic",
        card="devenv",
        depends_on="devenv_cpu_enabled",
        doc=("Environments without a GPU one person can run at once", "每人同时可运行的无 GPU 环境数"),
    ),
    "devenv_cpu_cores": Editable(
        float,
        min=0,
        max=1024,
        unit="CPU",
        group="devenv",
        tier="basic",
        card="devenv",
        depends_on="devenv_cpu_enabled",
        doc=(
            "CPU cores one environment without a GPU may use (0 = the whole shared pool)",
            "单个无 GPU 环境可用的 CPU 核心数（0 表示整个共享池）",
        ),
    ),
    "devenv_cpu_memory_gb": Editable(
        float,
        min=0,
        max=8192,
        unit="GB",
        group="devenv",
        tier="basic",
        card="devenv",
        depends_on="devenv_cpu_enabled",
        doc=("Memory one environment without a GPU may use (0 = no limit)", "单个无 GPU 环境可用的内存（0 表示不限）"),
    ),
    "devenv_cpu_memory_pool_gb": Editable(
        float,
        min=0,
        max=65536,
        unit="GB",
        group="devenv",
        tier="expert",
        depends_on="devenv_cpu_enabled",
        doc=(
            "Memory all environments without a GPU may claim together (0 = no limit)",
            "所有无 GPU 环境合计可占用的内存（0 表示不限）",
        ),
    ),
    "devenv_cpu_cores_per_gpu": Editable(
        int,
        min=0,
        max=1024,
        unit="CPU",
        group="devenv",
        tier="expert",
        depends_on="devenv_enabled",
        doc=(
            "CPU cores each lane reserves per GPU for its bookings (cpuset); 0 = none",
            "每个通道按每块 GPU 为其预约保留的 CPU 核心数（cpuset）；0 表示不保留",
        ),
    ),
    "devenv_ssh_host": Editable(
        str,
        max=255,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("ssh", "host", "主机"),
        doc=(
            "Host name shown in ssh commands; empty = the address the browser uses",
            "ssh 命令中显示的主机名；留空则使用浏览器访问的地址",
        ),
    ),
    "devenv_ssh_ports": Editable(
        str,
        min=1,
        max=32,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("ssh", "port", "端口"),
        doc=("Host ports given to dev environments", "分配给开发环境的宿主机端口"),
    ),
    "devenv_password_login": Editable(
        bool,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("ssh", "password", "密码"),
        doc=(
            "Allow SSH logins with the environment password as well as keys",
            "除公钥外也允许用环境密码 SSH 登录",
        ),
    ),
    "devenv_cpus": Editable(
        float,
        min=0,
        max=1024,
        unit="CPU",
        group="devenv",
        depends_on="devenv_enabled",
        doc=("CPU limit of one environment with a GPU (0 = no limit)", "单个 GPU 环境的 CPU 上限（0 表示不限）"),
    ),
    "devenv_memory_gb": Editable(
        float,
        min=0,
        max=8192,
        unit="GB",
        group="devenv",
        depends_on="devenv_enabled",
        doc=("Memory limit of one environment with a GPU (0 = no limit)", "单个 GPU 环境的内存上限（0 表示不限）"),
    ),
    "devenv_shm_gb": Editable(
        float,
        min=0,
        max=1024,
        unit="GB",
        group="devenv",
        depends_on="devenv_enabled",
        doc=("Shared memory (`/dev/shm`) of one environment", "单个环境的共享内存（`/dev/shm`）"),
    ),
    "devenv_custom_images": Editable(
        bool,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("image", "镜像"),
        doc=("Let people create templates from any image", "允许用户用任意镜像新建模板"),
    ),
    "devenv_allowed_registries": Editable(
        str,
        max=500,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("image", "registry", "镜像", "仓库"),
        doc=(
            "Comma-separated registries images may come from; empty = any",
            "允许的镜像仓库，逗号分隔；留空表示不限",
        ),
    ),
    "devenv_saved_images": Editable(
        int,
        min=0,
        max=100,
        group="devenv",
        depends_on="devenv_enabled",
        doc=("Saved environment images per person", "每人可保存的环境镜像数"),
    ),
    "devenv_pull_minutes": Editable(
        int,
        min=0,
        max=24 * 60,
        unit="min",
        group="devenv",
        tier="expert",
        depends_on="devenv_enabled",
        doc=("Download the image this long before a booking", "预约开始前多久下载镜像"),
    ),
    "devenv_projects": Editable(
        int,
        min=0,
        max=100,
        group="devenv",
        depends_on="devenv_enabled",
        doc=("Kept (paused) environments per person; 0 = none", "每人可保留（暂停）的环境数；0 表示不允许"),
    ),
    "devenv_project_days": Editable(
        int,
        min=0,
        max=3650,
        unit="d",
        group="devenv",
        depends_on="devenv_enabled",
        doc=(
            "Remove kept environments unused this many days (0 = never)",
            "自动删除闲置这么多天的保留环境（0 表示从不）",
        ),
    ),
    "devenv_public_ports_enabled": Editable(
        bool,
        group="devenv",
        depends_on="devenv_enabled",
        keywords=("direct port", "port", "端口", "直连"),
        doc=(
            "Let people publish container ports directly on host ports (no SlotServe login)",
            "允许用户把容器端口直接发布到宿主机端口（无需登录 SlotServe）",
        ),
    ),
    "devenv_public_ports": Editable(
        str,
        min=1,
        max=32,
        group="devenv",
        depends_on="devenv_public_ports_enabled",
        keywords=("direct port", "port", "端口", "直连"),
        doc=(
            "Host ports for direct ports (keep apart from the SSH ports)",
            "直连端口使用的宿主机端口（与 SSH 端口分开）",
        ),
    ),
    "devenv_public_port_limit": Editable(
        int,
        min=0,
        max=1000,
        group="devenv",
        depends_on="devenv_public_ports_enabled",
        keywords=("direct port", "port", "端口", "直连"),
        doc=(
            "Direct ports per person; admins have no limit; 0 = only admins",
            "每人直连端口数；管理员不受限制；0 表示只有管理员",
        ),
    ),
    "devenv_web_url": Editable(
        str,
        max=300,
        group="devenv",
        tier="expert",
        depends_on="devenv_enabled",
        keywords=("web", "port", "reverse proxy", "端口", "反向代理"),
        doc=(
            "How browsers reach the web port of dev environments, e.g. behind a reverse proxy; empty = the same "
            "host on `DEVENV_WEB_PORT`",
            "浏览器访问开发环境网页端口的地址，例如经过反向代理时；留空则为同一主机的 `DEVENV_WEB_PORT`",
        ),
    ),
    "devenv_web_compat": Editable(
        bool,
        group="devenv",
        tier="expert",
        depends_on="devenv_enabled",
        keywords=("web", "compatibility", "兼容"),
        doc=(
            "Compatibility mode for web apps that break on the shared web origin's rules between environments "
            "(weakens the isolation between environments)",
            "兼容模式：用于在共享网页源的环境隔离规则下无法正常工作的网页应用（会削弱环境之间的隔离）",
        ),
    ),
    "devenv_forwarder_image": Editable(
        str,
        min=1,
        max=255,
        group="devenv",
        tier="expert",
        depends_on="devenv_public_ports_enabled",
        danger=_ROOT,
        keywords=("socat", "image", "镜像"),
        doc=(
            "Image of the per-port forwarder containers (needs `socat` on its PATH)",
            "每个端口的转发容器所用镜像（PATH 中需要有 `socat`）",
        ),
    ),
    "devenv_storage_dir": Editable(
        str,
        max=500,
        group="devenv",
        tier="expert",
        depends_on="devenv_enabled",
        danger=_ROOT,
        keywords=("bind mount", "volume", "目录", "挂载"),
        doc=(
            "Directory on the Docker host for persistent data (bind mounts); empty = Docker named volumes. System "
            "directories (`/etc`, `/root`, `/var/lib/docker`, `/proc`, `/sys`, `/boot`, `/usr`, `/bin`, `/lib*`, "
            "`/var/run` …) are refused",
            "Docker 主机上存放持久数据的目录（绑定挂载）；留空则使用 Docker 命名卷。系统目录（`/etc`、`/root`、"
            "`/var/lib/docker`、`/proc`、`/sys`、`/boot`、`/usr`、`/bin`、`/lib*`、`/var/run` 等）会被拒绝",
        ),
    ),
    # ---- models ------------------------------------------------------------------------------------------------
    "default_llama_args": Editable(
        str,
        max=4000,
        group="models",
        tier="basic",
        card="models",
        keywords=("arguments", "llama", "参数"),
        doc=("Arguments given to newly registered models", "新登记模型的默认启动参数"),
    ),
    "hf_endpoint": Editable(
        str,
        min=1,
        max=300,
        group="models",
        tier="basic",
        card="models",
        keywords=("hugging face", "mirror", "镜像"),
        doc=("Hugging Face endpoint or mirror", "Hugging Face 地址或镜像"),
    ),
    "hf_token": Editable(
        str,
        max=200,
        secret=True,
        group="models",
        keywords=("hugging face", "token"),
        doc=("Token for gated or private repositories", "受限或私有仓库所需的 Token"),
    ),
    "download_min_free_gb": Editable(
        float,
        min=0,
        max=10_000,
        unit="GB",
        group="models",
        keywords=("disk", "磁盘"),
        doc=("Disk space that must remain free after a download", "下载完成后磁盘至少保留的空间"),
    ),
    "playground_enabled": Editable(
        bool,
        group="models",
        keywords=("playground", "chat", "试用"),
        doc=(
            "The web page's chat with a model (uses the browser session, never a key)",
            "网页上与模型对话的试用页（使用浏览器会话，不使用密钥）",
        ),
    ),
    "playground_rate_per_minute": Editable(
        int,
        min=0,
        max=10_000,
        unit="/min",
        group="models",
        depends_on="playground_enabled",
        doc=("Playground requests per person and minute; 0 = no limit", "每人每分钟的试用请求数；0 表示不限"),
    ),
    "playground_default_model_only": Editable(
        bool,
        group="models",
        depends_on="playground_enabled",
        doc=(
            "Playground uses only the default model, never another loaded one",
            "试用页只使用默认模型，不使用其他已加载的模型",
        ),
    ),
    # ---- runtime -----------------------------------------------------------------------------------------------
    "preload_min_minutes": Editable(
        int,
        min=0,
        max=120,
        unit="min",
        group="runtime",
        doc=(
            "Start loading a booked model at least this many minutes early",
            "预约的模型至少提前多少分钟开始加载",
        ),
    ),
    "load_timeout_seconds": Editable(
        int,
        min=30,
        max=7200,
        unit="s",
        group="runtime",
        doc=("Give up if a model is not ready after this long", "超过此时间仍未就绪则判定加载失败"),
    ),
    "drain_seconds": Editable(
        int,
        min=0,
        max=600,
        unit="s",
        group="runtime",
        doc=(
            "Before a model switch, wait this long for running requests to finish",
            "切换模型前等待进行中请求完成的最长时间",
        ),
    ),
    "preload_buffer_seconds": Editable(
        int,
        min=0,
        max=3600,
        unit="s",
        group="runtime",
        tier="expert",
        doc=("Extra margin when preloading before a booking", "预加载时额外预留的时间"),
    ),
    "stop_timeout_seconds": Editable(
        int,
        min=1,
        max=300,
        unit="s",
        group="runtime",
        tier="expert",
        doc=("Grace period before a model server is killed", "强制结束推理进程前的等待时间"),
    ),
    "disk_read_mbps": Editable(
        float,
        min=1,
        max=100_000,
        unit="MB/s",
        group="runtime",
        tier="expert",
        doc=("Used to estimate load time before a model's first load", "模型首次加载前用于估算加载时间"),
    ),
    "scheduler_interval_seconds": Editable(
        float,
        min=1,
        max=60,
        unit="s",
        group="runtime",
        tier="expert",
        doc=("How often the scheduler re-evaluates", "调度器重新评估的间隔"),
    ),
    # ---- engines -----------------------------------------------------------------------------------------------
    "vllm_cmd": Editable(
        str,
        min=1,
        max=1000,
        group="engines",
        tier="expert",
        danger=_ROOT,
        keywords=("vllm", "command", "命令"),
        doc=("Command for the vLLM engine", "vLLM 后端的启动命令"),
    ),
    "sglang_cmd": Editable(
        str,
        min=1,
        max=1000,
        group="engines",
        tier="expert",
        danger=_ROOT,
        keywords=("sglang", "command", "命令"),
        doc=("Command for the SGLang engine", "SGLang 后端的启动命令"),
    ),
    "custom_engine_cmd": Editable(
        str,
        max=2000,
        group="engines",
        tier="expert",
        danger=_ROOT,
        keywords=("custom engine", "command", "命令"),
        doc=(
            "Template for the custom engine: `{model}` `{host}` `{port}` `{name}`",
            "自定义后端命令模板：`{model}` `{host}` `{port}` `{name}`",
        ),
    ),
    "custom_engine_health": Editable(
        str,
        min=1,
        max=200,
        group="engines",
        tier="expert",
        depends_on="custom_engine_cmd",
        doc=("Path polled until the custom engine is ready", "轮询判断自定义后端就绪的路径"),
    ),
    # ---- chat platforms ----------------------------------------------------------------------------------------
    "feishu_enabled": Editable(
        bool,
        group="feishu",
        keywords=("lark", "飞书"),
        doc=(
            "Feishu / Lark bot ([setup](integrations.md#feishu-lark))",
            "飞书 / Lark 机器人（[配置方法](integrations.zh.md#feishu-lark)）",
        ),
    ),
    "feishu_app_id": Editable(
        str, max=100, group="feishu", depends_on="feishu_enabled", doc=("App ID of the Feishu app", "飞书应用的 App ID")
    ),
    "feishu_app_secret": Editable(
        str,
        max=200,
        secret=True,
        group="feishu",
        depends_on="feishu_enabled",
        doc=("App secret of the Feishu app", "飞书应用的 App Secret"),
    ),
    "feishu_domain": Editable(
        str,
        min=1,
        max=200,
        group="feishu",
        tier="expert",
        depends_on="feishu_enabled",
        doc=(
            "Open platform address; Lark uses `https://open.larksuite.com`",
            "开放平台地址；Lark 使用 `https://open.larksuite.com`",
        ),
    ),
    "dingtalk_enabled": Editable(
        bool, group="dingtalk", keywords=("钉钉",), doc=("DingTalk bot in Stream mode", "钉钉 Stream 模式机器人")
    ),
    "dingtalk_client_id": Editable(
        str,
        max=100,
        group="dingtalk",
        depends_on="dingtalk_enabled",
        doc=("Client ID (AppKey) of the DingTalk app", "钉钉应用的 Client ID（AppKey）"),
    ),
    "dingtalk_client_secret": Editable(
        str,
        max=200,
        secret=True,
        group="dingtalk",
        depends_on="dingtalk_enabled",
        doc=("Client secret (AppSecret) of the DingTalk app", "钉钉应用的 Client Secret（AppSecret）"),
    ),
    "dingtalk_robot_code": Editable(
        str,
        max=100,
        group="dingtalk",
        tier="expert",
        depends_on="dingtalk_enabled",
        doc=("Robot code; empty = the client ID", "RobotCode；留空即 Client ID"),
    ),
    "wecom_enabled": Editable(
        bool, group="wecom", keywords=("企业微信",), doc=("WeCom self-built app", "企业微信自建应用")
    ),
    "wecom_corp_id": Editable(
        str, max=100, group="wecom", depends_on="wecom_enabled", doc=("Corp ID of the WeCom app", "企业 ID（CorpID）")
    ),
    "wecom_agent_id": Editable(
        str, max=32, group="wecom", depends_on="wecom_enabled", doc=("Agent ID of the WeCom app", "应用的 AgentId")
    ),
    "wecom_secret": Editable(
        str,
        max=200,
        secret=True,
        group="wecom",
        depends_on="wecom_enabled",
        doc=("Secret of the WeCom app", "应用的 Secret"),
    ),
    "wecom_token": Editable(
        str,
        max=100,
        secret=True,
        group="wecom",
        depends_on="wecom_enabled",
        doc=("Callback token; only for receiving commands", "回调 Token；只在接收命令时需要"),
    ),
    "wecom_aes_key": Editable(
        str,
        max=64,
        secret=True,
        group="wecom",
        depends_on="wecom_enabled",
        doc=(
            "Callback EncodingAESKey (43 characters); only for receiving commands",
            "回调 EncodingAESKey（43 位）；只在接收命令时需要",
        ),
    ),
    "qq_enabled": Editable(bool, group="qq", doc=("QQ official bot", "QQ 官方机器人")),
    "qq_app_id": Editable(
        str, max=100, group="qq", depends_on="qq_enabled", doc=("App ID of the QQ bot", "QQ 机器人的 AppID")
    ),
    "qq_app_secret": Editable(
        str,
        max=200,
        secret=True,
        group="qq",
        depends_on="qq_enabled",
        doc=("App secret of the QQ bot", "QQ 机器人的 AppSecret"),
    ),
    "qq_sandbox": Editable(
        bool,
        group="qq",
        tier="expert",
        depends_on="qq_enabled",
        doc=("Use the QQ sandbox environment", "使用 QQ 沙箱环境"),
    ),
    # ---- group webhooks ----------------------------------------------------------------------------------------
    "group_events": Editable(
        str,
        max=200,
        group="webhooks",
        doc=(
            "What group webhooks receive: `admin_alerts`, `approvals`, `announcements`",
            "发到群里的内容：`admin_alerts`、`approvals`、`announcements`",
        ),
    ),
    "feishu_webhook_url": Editable(
        str,
        max=500,
        secret=True,
        group="webhooks",
        doc=("Feishu / Lark group robot webhook", "飞书 / Lark 群机器人 Webhook"),
    ),
    "feishu_webhook_secret": Editable(
        str,
        max=200,
        secret=True,
        group="webhooks",
        depends_on="feishu_webhook_url",
        doc=("Signing secret of the Feishu / Lark group robot", "飞书 / Lark 群机器人的签名密钥"),
    ),
    "dingtalk_webhook_url": Editable(
        str, max=500, secret=True, group="webhooks", doc=("DingTalk group robot webhook", "钉钉群机器人 Webhook")
    ),
    "dingtalk_webhook_secret": Editable(
        str,
        max=200,
        secret=True,
        group="webhooks",
        depends_on="dingtalk_webhook_url",
        doc=("Signing secret of the DingTalk group robot", "钉钉群机器人的加签密钥"),
    ),
    "wecom_webhook_url": Editable(
        str, max=500, secret=True, group="webhooks", doc=("WeCom group robot webhook", "企业微信群机器人 Webhook")
    ),
    # ---- monitor -----------------------------------------------------------------------------------------------
    "metrics_token": Editable(
        str,
        max=200,
        secret=True,
        group="monitor",
        tier="expert",
        keywords=("prometheus", "metrics", "监控"),
        doc=("Bearer token for `/metrics`; empty disables it", "`/metrics` 的 Bearer Token，留空则关闭"),
    ),
    "usage_busy_percent": Editable(
        int,
        min=1,
        max=100,
        unit="%",
        group="monitor",
        tier="expert",
        doc=(
            "A GPU counts as used in a minute when its utilization is at least this (applies to new samples)",
            "某一分钟内 GPU 利用率不低于此值时计为“使用中”（对新采样生效）",
        ),
    ),
    # ---- maintenance -------------------------------------------------------------------------------------------
    "usage_retention_days": Editable(
        int,
        min=0,
        max=3650,
        unit="d",
        group="maintenance",
        doc=(
            "Keep per-request rows this long, then fold them into daily totals (0 = forever; otherwise ≥ 90)",
            "逐条请求记录保留天数，之后合并为按天汇总（0 表示永久；否则至少 90）",
        ),
    ),
    "usage_sample_days": Editable(
        int,
        min=0,
        max=3650,
        unit="d",
        group="maintenance",
        tier="expert",
        doc=(
            "Keep per-minute GPU usage samples this long, then fold them into hourly totals (0 = never fold)",
            "按分钟的 GPU 使用采样保留天数，之后合并为按小时汇总（0 表示不合并）",
        ),
    ),
    "audit_retention_days": Editable(
        int,
        min=0,
        max=3650,
        unit="d",
        group="maintenance",
        doc=("Keep audit log entries this long (0 = forever)", "审计日志保留天数（0 表示永久）"),
    ),
    "backup_keep": Editable(
        int,
        min=0,
        max=100,
        group="maintenance",
        keywords=("backup", "备份"),
        doc=(
            "Daily backup bundles to keep (0 = no automatic backups)",
            "保留的每日备份包份数（0 表示关闭自动备份）",
        ),
    ),
    "backup_target": Editable(
        str,
        max=500,
        group="maintenance",
        tier="basic",
        keywords=("backup", "nas", "copy", "off-site", "备份", "异地"),
        doc=(
            "Where each backup bundle is also copied: an absolute path or `storage:<id>` (empty = no copy off this "
            "machine)",
            "每个备份包另外复制到哪里：绝对路径或 `storage:<id>`（留空表示不复制到本机以外）",
        ),
    ),
    "backup_hour": Editable(
        int,
        min=-1,
        max=23,
        unit="h",
        group="maintenance",
        tier="advanced",
        keywords=("backup", "schedule", "备份", "时间"),
        doc=(
            "Local hour of the daily backup (-1 = every 24 hours from start)",
            "每日备份的本地时间（小时；-1 表示从启动起每 24 小时一次）",
        ),
    ),
    # ---- deployment (read-only, .env) --------------------------------------------------------------------------
    "secret_key": Editable(
        str,
        secret=True,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc_default=("*generated*", "*自动生成*"),
        doc=(
            "Signs session cookies and encrypts stored secrets. Unset, empty or `change-me`: SlotServe generates a "
            "random key on first start and keeps it in `DATA_DIR/secret_key` (owner-only). A value you set always "
            "wins",
            "会话 Cookie 签名与已存密钥加密所用的密钥。未设置、为空或为 `change-me` 时，SlotServe 首次启动会生成"
            "随机密钥并保存在 `DATA_DIR/secret_key`（仅属主可读）。显式设置的值始终优先",
        ),
    ),
    "host": Editable(str, group="deployment", tier="expert", restart_required=True, doc=("Listen address", "监听地址")),
    "port": Editable(
        int,
        group="deployment",
        tier="expert",
        restart_required=True,
        keywords=("port", "端口"),
        doc=("Listen port", "监听端口"),
    ),
    "cookie_secure": Editable(
        bool,
        group="deployment",
        tier="expert",
        restart_required=True,
        keywords=("https",),
        doc=("Mark cookies `Secure`; set `true` behind HTTPS", "Cookie 加 `Secure` 标记；在 HTTPS 后面时设为 `true`"),
    ),
    "forwarded_allow_ips": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        keywords=("reverse proxy", "x-forwarded-for", "反向代理"),
        doc=(
            "Reverse proxies whose `X-Forwarded-For` is trusted (comma separated, `*` = any), so sign-in throttling "
            "and logs see the real client address",
            "信任其 `X-Forwarded-For` 的反向代理地址（逗号分隔，`*` 表示任意），以便登录限速和日志看到真实客户端地址",
        ),
    ),
    "data_dir": Editable(
        str, group="deployment", tier="expert", restart_required=True, doc=("Database and logs", "数据库与日志目录")
    ),
    "models_dir": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=("Model directory (scanned, and target of downloads)", "模型目录（扫描及下载目标）"),
    ),
    "static_dir": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc_default=("*bundled*", "*内置*"),
        doc=("Built frontend directory", "前端构建产物目录"),
    ),
    "llama_server_cmd": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        danger=_ROOT,
        doc=(
            "Command used to start llama-server; may include a prefix such as `taskset -c 0-15 llama-server`",
            "启动 llama-server 的命令，可带前缀，如 `taskset -c 0-15 llama-server`",
        ),
    ),
    "llama_host": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=("Address the model servers bind to (keep it local)", "推理进程监听地址（请保持本地）"),
    ),
    "llama_port": Editable(
        int,
        group="deployment",
        tier="expert",
        restart_required=True,
        keywords=("port", "端口"),
        doc=(
            "Port of the first lane's model server; lane *N* uses this + *N* - 1",
            "第一个通道推理进程的端口；第 *N* 个通道为此值 + *N* - 1",
        ),
    ),
    "docker_host": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        danger=_ROOT,
        keywords=("docker", "podman"),
        doc=("Docker (or Podman) API used for dev environments", "开发环境使用的 Docker（或 Podman）接口"),
    ),
    "dropbear_path": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=(
            "Static Dropbear (`dropbearmulti`) copied into each dev environment; bundled in the image",
            "复制进每个开发环境的静态 Dropbear（`dropbearmulti`）；镜像中已自带",
        ),
    ),
    "devenv_web_port": Editable(
        int,
        group="deployment",
        tier="expert",
        restart_required=True,
        keywords=("web", "port", "端口"),
        doc=(
            "Port of the dev environment web interfaces, a separate origin (0 = serve them on `PORT`, not recommended)",
            "开发环境网页界面的端口，作为独立的源（0 表示在 `PORT` 上提供，不推荐）",
        ),
    ),
    "storage_helper_image": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=(
            "Image of the short-lived containers that check storages from **Admin → Storage** (pulled when missing)",
            "检查**管理 → 存储**中各存储的临时容器所用的镜像（缺少时自动拉取）",
        ),
    ),
    "storage_check_minutes": Editable(
        int,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=(
            "How often storages are checked (free space, per-person usage); 0 = only on demand",
            "多久检查一次存储（可用空间、每人用量）；0 表示只在手动检查时",
        ),
    ),
    "telegram_api_base": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=(
            "Telegram Bot API address, for a self-hosted Bot API server",
            "Telegram Bot API 地址，用于自建的 Bot API 服务",
        ),
    ),
    "dingtalk_api_base": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=("DingTalk API address (only for proxies or testing)", "钉钉 API 地址（仅用于代理或测试）"),
    ),
    "wecom_api_base": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=("WeCom API address (only for proxies or testing)", "企业微信 API 地址（仅用于代理或测试）"),
    ),
    "qq_api_base": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=("QQ bot API address (only for proxies or testing)", "QQ 机器人 API 地址（仅用于代理或测试）"),
    ),
    "qq_token_url": Editable(
        str,
        group="deployment",
        tier="expert",
        restart_required=True,
        doc=("QQ bot token address (only for proxies or testing)", "QQ 机器人 Token 地址（仅用于代理或测试）"),
    ),
}

# Settings admins can change at runtime; the rest of REGISTRY is read-only deployment configuration.
EDITABLE: dict[str, Editable] = {k: v for k, v in REGISTRY.items() if not v.restart_required}
DANGER_KEYS = frozenset(k for k, v in EDITABLE.items() if v.danger)

# Changing one of these restarts the Telegram bot.
TELEGRAM_KEYS = {"telegram_bot_token", "telegram_proxy"}
# Changing a key that starts with "<channel>_" restarts that chat channel.
CHAT_CHANNELS = ("feishu", "dingtalk", "wecom", "qq")
GROUP_EVENT_KINDS = ("admin_alerts", "approvals", "announcements")

# devenv_storage_dir: host directories that must never be bind-mounted into containers (R1-09).
_SYSTEM_DIRS = (
    "/etc",
    "/root",
    "/var/lib/docker",
    "/var/run",
    "/run",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/usr",
    "/bin",
    "/sbin",
)
_LIB_DIR = re.compile(r"^/lib(32|64|x32)?(/|$)")  # /lib*


class InvalidSetting(ValueError):
    def __init__(self, key: str, reason: str):
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


def system_dir(path: str) -> bool:
    """Whether ``path`` is (inside) a host system directory."""
    return bool(_LIB_DIR.match(path)) or any(path == d or path.startswith(d + "/") for d in _SYSTEM_DIRS)


def validate(key: str, value: Any) -> Any:
    spec = EDITABLE.get(key)
    if spec is None:
        if key in REGISTRY:
            raise InvalidSetting(key, "set in .env, needs a restart")
        raise InvalidSetting(key, "unknown setting")
    if spec.type is bool:
        if not isinstance(value, bool):
            raise InvalidSetting(key, "expected true or false")
        return value
    if spec.type in (int, float):
        numeric = (int, float) if spec.type is float else (int,)
        if isinstance(value, bool) or not isinstance(value, numeric):
            raise InvalidSetting(key, "expected a number" if spec.type is float else "expected an integer")
        value = spec.type(value)
        if spec.choices is not None and value not in spec.choices:
            raise InvalidSetting(key, "must be one of " + ", ".join(str(c) for c in spec.choices))
        if (spec.min is not None and value < spec.min) or (spec.max is not None and value > spec.max):
            raise InvalidSetting(key, f"must be between {_fmt(spec.min)} and {_fmt(spec.max)}")
        if key == "usage_retention_days" and 0 < value < 90:
            raise InvalidSetting(key, "must be 0 (keep forever) or at least 90")
        return value
    if not isinstance(value, str):
        raise InvalidSetting(key, "expected text")
    value = value.strip()
    if spec.choices is not None and value not in spec.choices:
        raise InvalidSetting(key, "must be one of " + ", ".join(spec.choices))
    if (spec.min is not None and len(value) < spec.min) or (spec.max is not None and len(value) > spec.max):
        raise InvalidSetting(key, "invalid length")
    if key in ("default_llama_args", "vllm_cmd", "sglang_cmd", "custom_engine_cmd"):
        try:
            split_args(value)
        except ValueError as exc:
            raise InvalidSetting(key, str(exc)) from exc
    if key == "backup_target" and value:
        from .maintenance import BackupError, parse_target

        try:
            parse_target(value)
        except BackupError as exc:
            raise InvalidSetting(key, "an absolute path or storage:<id>") from exc
    if key in ("devenv_ssh_ports", "devenv_public_ports"):
        from .devenv import parse_port_range

        try:
            parse_port_range(value)
        except ValueError as exc:
            raise InvalidSetting(key, str(exc)) from exc
    if key == "devenv_storage_dir" and value:
        if not value.startswith("/") or ".." in value.split("/"):
            raise InvalidSetting(key, "expected an absolute path on the Docker host")
        value = re.sub(r"/+", "/", value).rstrip("/") or "/"
        if value == "/":
            raise InvalidSetting(key, "expected an absolute path on the Docker host")
        if system_dir(value):
            raise InvalidSetting(key, "a system directory of the host cannot be used")
    if key == "custom_engine_health" and not value.startswith("/"):
        raise InvalidSetting(key, "must start with /")
    if key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidSetting(key, "unknown time zone") from exc
    elif key == "group_events":
        kinds = [k.strip() for k in value.split(",") if k.strip()]
        if unknown := [k for k in kinds if k not in GROUP_EVENT_KINDS]:
            raise InvalidSetting(key, "unknown kind " + ", ".join(unknown))
        value = ",".join(dict.fromkeys(kinds))
    elif key.endswith("_webhook_url") and value:
        if not value.lower().startswith("https://"):
            raise InvalidSetting(key, "expected an https:// URL")
    elif key == "feishu_domain":
        if not value.lower().startswith(("http://", "https://")):
            raise InvalidSetting(key, "expected a URL")
        value = value.rstrip("/")
    elif key == "wecom_aes_key" and value and len(value) != 43:
        raise InvalidSetting(key, "must be 43 characters")
    elif key == "devenv_forwarder_image":
        from .devenv import IMAGE_RE

        if not IMAGE_RE.match(value):
            raise InvalidSetting(key, "expected an image reference")
    elif key in ("public_url", "hf_endpoint", "telegram_proxy", "devenv_web_url") and value:
        if not value.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise InvalidSetting(key, "expected a URL")
        value = value.rstrip("/")
    return value


def _fmt(n: float | None) -> str:
    return "" if n is None else (str(int(n)) if float(n).is_integer() else str(n))


def plain(value: Any) -> Any:
    """A setting value as JSON can carry it (paths become text)."""
    return value if value is None or isinstance(value, bool | int | float | str) else str(value)


def snapshot_defaults(settings: Settings) -> dict[str, Any]:
    """The values from the environment / ``.env``, before any admin override (taken once at start)."""
    return {key: plain(getattr(settings, key)) for key in REGISTRY}


def source(key: str, defaults: dict[str, Any], overridden: set[str]) -> str:
    """Where the current value comes from: ``admin`` (override in the database), ``env`` (environment or
    ``.env``) or ``default`` (built in)."""
    if key in overridden:
        return "admin"
    field = Settings.model_fields.get(key)
    builtin = plain(field.default) if field is not None else None
    return "default" if defaults.get(key) == builtin else "env"


def fingerprint(value: Any, secret_key: str) -> str:
    """What the audit log keeps of a secret: 8 hex digits of an HMAC, enough to tell values apart, useless to
    recover them. An empty secret stays empty ("not set")."""
    if value in (None, ""):
        return ""
    digest = hmac.new(secret_key.encode(), str(value).encode(), hashlib.sha256).hexdigest()
    return "fp:" + digest[:8]


def audit_value(key: str, value: Any, secret_key: str) -> Any:
    spec = REGISTRY.get(key)
    return fingerprint(value, secret_key) if spec is not None and spec.secret else plain(value)


async def load_overrides(db: AsyncSession, settings: Settings) -> None:
    for row in (await db.execute(select(AppSetting))).scalars():
        if row.key not in EDITABLE:
            continue
        try:
            setattr(settings, row.key, validate(row.key, json.loads(row.value)))
        except (InvalidSetting, ValueError):
            continue  # ignore a stale or corrupt override; the default stays in effect


async def overridden_keys(db: AsyncSession) -> set[str]:
    return set((await db.execute(select(AppSetting.key))).scalars())


@dataclass(frozen=True)
class Change:
    key: str
    before: Any
    after: Any
    reset: bool  # back to the .env default (override removed)
    was_default: bool  # no override before the change


async def plan(db: AsyncSession, settings: Settings, defaults: dict[str, Any], changes: dict[str, Any]) -> list[Change]:
    """Validates ``changes`` (``None`` resets a key to its default) and returns what would really change.

    Raises ``InvalidSetting`` for the first invalid value; nothing is stored."""
    overridden = await overridden_keys(db)
    out: list[Change] = []
    for key, value in changes.items():
        cleaned = None if value is None else validate(key, value)
        if key not in EDITABLE:  # None for a deployment key
            raise InvalidSetting(key, "set in .env, needs a restart" if key in REGISTRY else "unknown setting")
        before = plain(getattr(settings, key))
        after = defaults[key] if cleaned is None else cleaned
        was_default = key not in overridden
        if before == after and not (cleaned is None and not was_default):
            continue  # nothing to do (only removing an override that equals the default is still a change)
        out.append(Change(key, before, after, cleaned is None, was_default))
    return out


async def store(db: AsyncSession, settings: Settings, planned: list[Change]) -> None:
    """Writes planned changes and applies them to ``settings``. Caller commits."""
    for change in planned:
        row = await db.get(AppSetting, change.key)
        if change.reset:
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(AppSetting(key=change.key, value=json.dumps(change.after)))
        else:
            row.value = json.dumps(change.after)
        setattr(settings, change.key, change.after)


async def apply(db: AsyncSession, settings: Settings, defaults: dict[str, Any], changes: dict[str, Any]) -> dict:
    """Validates and stores ``changes``; returns ``{key: new value}`` with secrets masked. Caller commits."""
    planned = await plan(db, settings, defaults, changes)
    await store(db, settings, planned)
    return {c.key: "***" if EDITABLE[c.key].secret else c.after for c in planned}


async def dry_run(db: AsyncSession, settings: Settings, planned: list[Change]) -> list[dict]:
    """The impact of planned changes, for the settings that can compute one (``Editable.impact``).

    Each impact: ``{key, text_key, params, users[], reservations[]}``; see ``settings_impact.py``. One function
    covering several keys (approval mode + hours) runs once, on the combined change.
    """
    after = {c.key: c.after for c in planned}
    done: set[ImpactFn] = set()
    out = []
    for change in planned:
        fn = EDITABLE[change.key].impact
        if fn is None or fn in done:
            continue
        done.add(fn)
        out.append(await fn(db, settings, change.key, change.before, after))
    return [i for i in out if i]
