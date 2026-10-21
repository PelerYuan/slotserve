from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings are read from environment variables prefixed with ``SLOTSERVE_``."""

    model_config = SettingsConfigDict(env_prefix="SLOTSERVE_", env_file=".env", extra="ignore")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = "change-me"
    invite_code: str = "change-me"
    registration_open: bool = True
    # Keep an encrypted copy of new API keys so their owners can view them again.
    api_key_reveal: bool = False
    cookie_secure: bool = False
    static_dir: Path | None = None
    # Base URL people use to open SlotServe (e.g. http://gpu-box:8000); used in links sent by the bot.
    public_url: str = ""
    # IANA time zone used in notifications.
    timezone: str = "UTC"

    # Storage
    data_dir: Path = Path("data")
    models_dir: Path = Path("models")

    # llama-server
    llama_server_cmd: str = "llama-server"
    llama_host: str = "127.0.0.1"
    llama_port: int = 8081
    default_llama_args: str = "-ngl 99 -c 8192"
    # Other engines (see runtime/engines.py); the command must be available where SlotServe runs.
    vllm_cmd: str = "vllm serve"
    sglang_cmd: str = "python -m sglang.launch_server"
    # Placeholders: {model} {host} {port} {name}
    custom_engine_cmd: str = ""
    custom_engine_health: str = "/health"
    load_timeout_seconds: int = 1800
    stop_timeout_seconds: int = 20
    # How long a model switch waits for in-flight requests to finish.
    drain_seconds: int = 30
    disk_read_mbps: float = 150.0

    # Scheduling
    slot_minutes: int = 30
    max_reservation_hours: int = 8
    max_advance_days: int = 30
    preload_buffer_seconds: int = 60
    # Start loading a booked model at least this many minutes before the booking (on top of the learned estimate).
    preload_min_minutes: int = 3
    scheduler_interval_seconds: float = 5.0
    # Per-person limits for non-admins; 0 means unlimited.
    quota_weekly_hours: int = 0
    # off | long (bookings longer than approval_hours) | all; admins never need approval.
    approval_mode: str = "off"
    approval_hours: int = 4
    quota_upcoming: int = 0
    reminder_minutes: int = 10

    # Downloads
    download_min_free_gb: float = 2.0
    hf_endpoint: str = "https://huggingface.co"
    hf_token: str = ""

    # Telegram bot (notifications); empty token disables it.
    telegram_bot_token: str = ""
    telegram_proxy: str = ""
    telegram_api_base: str = "https://api.telegram.org"

    # Other chat platforms (see services/chat). Each needs its switch on and its credentials.
    # Language for group chats, group webhooks and chats that are not linked yet.
    chat_default_locale: str = "en"
    # Feishu / Lark self-built app, events over the long connection. Lark: https://open.larksuite.com
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "https://open.feishu.cn"
    # DingTalk internal app robot in Stream mode. robot code empty = the client id.
    dingtalk_enabled: bool = False
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_robot_code: str = ""
    dingtalk_api_base: str = "https://api.dingtalk.com"
    # WeCom self-built app; receiving needs the callback URL to be reachable from WeCom.
    wecom_enabled: bool = False
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_token: str = ""
    wecom_aes_key: str = ""
    wecom_api_base: str = "https://qyapi.weixin.qq.com"
    # QQ official bot (QQ open platform), WebSocket gateway.
    qq_enabled: bool = False
    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_sandbox: bool = False
    qq_api_base: str = "https://api.sgroup.qq.com"
    qq_token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    # Group robot webhooks; group_events picks what they receive (admin_alerts, approvals, announcements).
    feishu_webhook_url: str = ""
    feishu_webhook_secret: str = ""
    dingtalk_webhook_url: str = ""
    dingtalk_webhook_secret: str = ""
    wecom_webhook_url: str = ""
    group_events: str = "admin_alerts,announcements"

    # Prometheus /metrics; empty token disables the endpoint.
    metrics_token: str = ""

    # Housekeeping: 0 keeps forever / disables.
    usage_retention_days: int = 365
    # GPU samples (one per GPU and minute) are folded into hourly totals after this many days.
    usage_sample_days: int = 30
    # A GPU counts as "in use" in a minute when its utilization is at least this high.
    usage_busy_percent: int = 10
    audit_retention_days: int = 730
    backup_keep: int = 7

    # Dev environments: Docker containers with SSH, booked like GPU time. Off until an admin turns them on.
    devenv_enabled: bool = False
    # Docker (or Podman's Docker-compatible) API: unix:///path/to.sock or tcp://host:port
    docker_host: str = "unix:///var/run/docker.sock"
    # Static Dropbear (dropbearmulti) copied into each container; bundled in the Docker image.
    dropbear_path: str = "/opt/slotserve/bin/dropbearmulti"
    devenv_ssh_host: str = ""  # host name shown in the ssh command; empty = the address the browser uses
    devenv_ssh_ports: str = "2200-2299"
    devenv_cpus: float = 0.0  # 0 = no limit
    devenv_memory_gb: float = 0.0  # 0 = no limit
    devenv_shm_gb: float = 8.0
    devenv_custom_images: bool = True
    devenv_allowed_registries: str = ""  # comma separated, e.g. docker.io,nvcr.io; empty = any
    devenv_saved_images: int = 3  # saved images per person
    devenv_pull_minutes: int = 60  # start pulling the image this long before the booking
    # Let people log in with their environment password as well as their SSH keys.
    devenv_password_login: bool = True
    # Host directory for persistent data (bind mounts); empty = Docker named volumes.
    devenv_storage_dir: str = ""
    devenv_projects: int = 3  # kept (paused) environments per person; 0 = none
    devenv_project_days: int = 0  # remove kept environments unused for this many days; 0 = never
    # Storage managed in Admin -> Storage (services/storage.py): the small image that checks storages (df, du),
    # and how often they are checked; 0 = only when an admin asks.
    storage_helper_image: str = "busybox:1.37"
    storage_check_minutes: int = 30
    # -- devenv_cpu: environments without a GPU (no booking), on the CPU cores no lane reserves --
    devenv_cpu_enabled: bool = True
    devenv_cpu_per_user: int = 2  # CPU-only environments one person can run at the same time
    devenv_cpu_cores_per_gpu: int = 8  # CPU cores each GPU lane reserves for whoever uses it; 0 = none
    devenv_cpu_cores: float = 4.0  # cores one CPU-only environment may use at most; 0 = the whole shared pool
    devenv_cpu_memory_gb: float = 16.0  # RAM one CPU-only environment may use at most; 0 = no limit
    devenv_cpu_memory_pool_gb: float = 0.0  # RAM all CPU-only environments together may claim; 0 = no limit
    # Direct ports: container ports published on the host as http://server:<port> (no SlotServe login), through a
    # small forwarder container per port (services/devenv_ports.py).
    devenv_public_ports_enabled: bool = True
    devenv_public_ports: str = "2300-2399"  # host ports for them; keep it apart from devenv_ssh_ports
    devenv_public_port_limit: int = 5  # direct ports per person (admins: no limit); 0 = only admins
    devenv_forwarder_image: str = "alpine/socat:1.8.1.1"  # any image with socat on its PATH
    # Web interfaces behind the SlotServe login are served on their own port, so the browser keeps them apart from
    # SlotServe itself (a separate origin). 0 = serve them on SlotServe's own port (not recommended).
    devenv_web_port: int = 8001
    # How browsers reach that port, e.g. https://gpu-box:8443 behind a reverse proxy; empty = the host name the
    # browser uses for SlotServe with devenv_web_port.
    devenv_web_url: str = ""

    # Multiple machines (see docs "Multiple machines"): node agents connect to the control plane.
    local_node_name: str = "local"  # name of the built-in node (this machine)
    node_heartbeat_seconds: float = 5.0  # how often agents report metrics
    node_offline_seconds: float = 30.0  # a node that sent nothing for this long is offline
    node_rpc_timeout_seconds: float = 30.0
    # Node agent (``slotserve agent``): the control plane URL and this node's token.
    agent_server: str = ""
    agent_token: str = ""
    # Address the control plane should use to reach this node; empty = the address it connects from.
    agent_address: str = ""
    # Interface the node's model servers listen on (the control plane forwards requests to them).
    agent_model_host: str = "0.0.0.0"
    # Wake-on-LAN: the MAC address(es) of this machine's network card (comma separated). Inside a container the
    # agent only sees virtual cards, so the install scripts pass the host's MAC here.
    agent_wol_mac: str = ""
    # A label for the local network this machine is in (machines with the same label wake each other).
    agent_network: str = ""
    # The host's operating system when the agent cannot see it (the install scripts set "windows" / "macos").
    agent_host_os: str = ""

    # Personal computers (see docs "Add a personal computer"): owners share their machine on a schedule.
    # Running work on a machine whose owner pauses sharing is warned, then stopped after this many minutes.
    sharing_grace_minutes: int = 10
    # Wake a sleeping machine this many minutes before a booking on it starts (0 = never wake automatically).
    wol_lead_minutes: int = 5
    wol_port: int = 9  # UDP port of the magic packet (7 or 9)

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{(self.data_dir / 'slotserve.db').as_posix()}"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def llama_base_url(self) -> str:
        return f"http://{self.llama_host}:{self.llama_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
