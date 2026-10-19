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
    cookie_secure: bool = False
    static_dir: Path | None = None

    # Storage
    data_dir: Path = Path("data")
    models_dir: Path = Path("models")

    # llama-server
    llama_server_cmd: str = "llama-server"
    llama_host: str = "127.0.0.1"
    llama_port: int = 8081
    default_llama_args: str = "-ngl 99 -c 8192"
    load_timeout_seconds: int = 1800
    stop_timeout_seconds: int = 20
    disk_read_mbps: float = 150.0

    # Scheduling
    slot_minutes: int = 30
    max_reservation_hours: int = 8
    max_advance_days: int = 30
    preload_buffer_seconds: int = 60
    scheduler_interval_seconds: float = 5.0

    # Downloads
    download_min_free_gb: float = 2.0

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
