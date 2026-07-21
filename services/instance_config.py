from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=False)


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_date(name: str, default: date) -> date:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return default


def _optional_env_date(name: str) -> date | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    instance_id: str
    instance_name: str
    instance_port: int
    data_dir: Path
    start_date: date | None
    end_date: date | None
    city: str
    source: str
    active: bool

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "hotel_price_collector.sqlite" if self.active else BASE_DIR / "data" / "hotel_price_collector.db"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports" if self.active else BASE_DIR / "data" / "exports"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots" if self.active else BASE_DIR / "data" / "screenshots"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug" if self.active else BASE_DIR / "data" / "debug"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs" if self.active else BASE_DIR / "data" / "logs"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser_profile" if self.active else BASE_DIR / "data" / "browser_profile"

    @property
    def checkpoint_dir(self) -> Path:
        return self.data_dir / "checkpoints" if self.active else BASE_DIR / "data" / "checkpoints"

    @property
    def partial_dir(self) -> Path:
        return self.data_dir / "partial" if self.active else BASE_DIR / "data" / "partial"

    @property
    def status_dir(self) -> Path:
        return self.data_dir / "status" if self.active else BASE_DIR / "data" / "status"

    @property
    def status_file(self) -> Path:
        return self.status_dir / "current_job_status.json"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in [self.export_dir, self.screenshot_dir, self.debug_dir, self.log_dir, self.browser_profile_dir, self.checkpoint_dir, self.partial_dir, self.status_dir]:
            path.mkdir(parents=True, exist_ok=True)


def load_instance_config(default_start: date | None = None, default_end: date | None = None) -> InstanceConfig:
    instance_id = _env_text("INSTANCE_ID", "default")
    active = bool(os.getenv("INSTANCE_DATA_DIR") or os.getenv("INSTANCE_ID"))
    data_dir_text = _env_text("INSTANCE_DATA_DIR", "data")
    data_dir = Path(data_dir_text)
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    start_date = _optional_env_date("INSTANCE_START_DATE") or default_start
    end_date = _optional_env_date("INSTANCE_END_DATE") or default_end or start_date
    try:
        instance_port = int(_env_text("INSTANCE_PORT", "8501"))
    except ValueError:
        instance_port = 8501
    return InstanceConfig(
        instance_id=instance_id,
        instance_name=_env_text("INSTANCE_NAME", "Default Hotel Price Collector"),
        instance_port=instance_port,
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
        city=_env_text("INSTANCE_CITY", "Orlando"),
        source=_env_text("INSTANCE_SOURCE", "Booking.com"),
        active=active,
    )


INSTANCE_CONFIG = load_instance_config()
