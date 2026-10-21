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

    # Prometheus /metrics; empty token disables the endpoint.
    metrics_token: str = ""

    # Housekeeping: 0 keeps forever / disables.
    usage_retention_days: int = 365
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
