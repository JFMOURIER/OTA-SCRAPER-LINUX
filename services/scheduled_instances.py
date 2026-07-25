from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from dateutil.relativedelta import relativedelta

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTANCE_ORDER = (
    "near_30_days",
    "medium_31_120_days",
    "long_121_365_days",
)


@dataclass(frozen=True, slots=True)
class ScheduledInstanceDefinition:
    instance_id: str
    port: int
    display: str
    drive_folder_id: str
    drive_folder_url: str
    default_frequency_mode: str
    default_interval_minutes: int | None
    default_runs_per_day: int | None
    default_daily_run_times: tuple[str, ...]
    workspace_name: str | None = None
    window_class: str | None = None
    data_dir_override: Path | None = None

    @property
    def data_dir(self) -> Path:
        return self.data_dir_override or (
            PROJECT_ROOT / "data" / "instances" / self.instance_id
        )

    @property
    def date_bucket(self) -> str:
        return self.instance_id

    @property
    def automatic_window_profile(self) -> str:
        return self.instance_id

    def resolve_window(self, today: date | None = None) -> tuple[date, date]:
        anchor = today or date.today()
        return resolve_automatic_windows(anchor)[self.instance_id]


SCHEDULED_INSTANCES: dict[str, ScheduledInstanceDefinition] = {
    "near_30_days": ScheduledInstanceDefinition(
        instance_id="near_30_days",
        port=8501,
        display="",
        drive_folder_id="18kkxujFodzEfoOmhfpBuZI8xDzaoUk8b",
        drive_folder_url=(
            "https://drive.google.com/drive/folders/"
            "18kkxujFodzEfoOmhfpBuZI8xDzaoUk8b?usp=drive_link"
        ),
        default_frequency_mode="interval",
        default_interval_minutes=60,
        default_runs_per_day=24,
        default_daily_run_times=(),
        workspace_name="SCRAPER 1",
        window_class="ota-scraper-instance-1",
    ),
    "medium_31_120_days": ScheduledInstanceDefinition(
        instance_id="medium_31_120_days",
        port=8502,
        display=":102",
        drive_folder_id="1ATZztmP0pS9c0oprzF3LzQKYcpsXtT7S",
        drive_folder_url=(
            "https://drive.google.com/drive/folders/"
            "1ATZztmP0pS9c0oprzF3LzQKYcpsXtT7S?usp=drive_link"
        ),
        default_frequency_mode="daily",
        default_interval_minutes=None,
        default_runs_per_day=2,
        default_daily_run_times=("00:20", "12:20"),
    ),
    "long_121_365_days": ScheduledInstanceDefinition(
        instance_id="long_121_365_days",
        port=8503,
        display=":103",
        drive_folder_id="19TNxbw6-ymHmUE2dkrSLyvKwYtjP4H5c",
        drive_folder_url=(
            "https://drive.google.com/drive/folders/"
            "19TNxbw6-ymHmUE2dkrSLyvKwYtjP4H5c?usp=drive_link"
        ),
        default_frequency_mode="daily",
        default_interval_minutes=None,
        default_runs_per_day=1,
        default_daily_run_times=("01:35",),
    ),
}


def get_scheduled_instance(instance_id: str) -> ScheduledInstanceDefinition:
    if "/" in instance_id or "\\" in instance_id or ".." in instance_id:
        raise ValueError(f"Unsafe scheduled instance ID: {instance_id!r}")
    try:
        return SCHEDULED_INSTANCES[instance_id]
    except KeyError as exc:
        choices = ", ".join(SCHEDULED_INSTANCES)
        raise ValueError(
            f"Unknown scheduled instance {instance_id!r}; expected one of {choices}"
        ) from exc


def resolve_automatic_windows(anchor_date: date) -> dict[str, tuple[date, date]]:
    """Resolve exact rolling stay-date offsets in Europe/Paris.

    Scraper 1 is explicitly today through today + 30 calendar days.  The
    The later instances retain their established calendar-month boundaries.
    Scraper 2 starts immediately after scraper 1 so month-end anchors cannot
    overlap the exact 31-date scraper-1 window.
    """
    near_start = anchor_date
    near_end = anchor_date + timedelta(days=30)
    medium_start = near_end + timedelta(days=1)
    long_start = anchor_date + relativedelta(months=4)
    medium_end = long_start - timedelta(days=1)
    calendar_horizon_end = (
        anchor_date + relativedelta(months=12) - timedelta(days=1)
    )
    hard_cap_end = anchor_date + timedelta(days=365)
    windows = {
        "near_30_days": (near_start, near_end),
        "medium_31_120_days": (medium_start, medium_end),
        "long_121_365_days": (
            long_start,
            min(calendar_horizon_end, hard_cap_end),
        ),
    }
    validate_automatic_windows(anchor_date, windows)
    return windows


def validate_automatic_windows(
    anchor_date: date,
    windows: dict[str, tuple[date, date]],
) -> None:
    if tuple(windows) != INSTANCE_ORDER:
        raise ValueError("Automatic windows must contain all instances in order")
    near = windows["near_30_days"]
    medium = windows["medium_31_120_days"]
    long = windows["long_121_365_days"]
    if near[0] != anchor_date:
        raise ValueError("Near automatic window must begin on the anchor date")
    if near[1] + timedelta(days=1) != medium[0]:
        raise ValueError("Gap or overlap between near and medium windows")
    if medium[1] + timedelta(days=1) != long[0]:
        raise ValueError("Gap or overlap between medium and long windows")
    if long[1] > anchor_date + timedelta(days=365):
        raise ValueError("Long automatic window exceeds the 365-day hard cap")
    flattened: list[date] = []
    for start, end in windows.values():
        if start > end:
            raise ValueError("Automatic window start exceeds its end")
        flattened.extend(
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
        )
    if len(flattened) != len(set(flattened)):
        raise ValueError("Automatic windows overlap")
    expected = set(
        anchor_date + timedelta(days=offset)
        for offset in range((long[1] - anchor_date).days + 1)
    )
    if set(flattened) != expected:
        raise ValueError("Automatic windows contain a gap")


def resolved_windows(
    today: date | None = None,
    definitions: Iterable[ScheduledInstanceDefinition] | None = None,
) -> dict[str, tuple[date, date]]:
    anchor = today or date.today()
    all_windows = resolve_automatic_windows(anchor)
    selected = definitions or SCHEDULED_INSTANCES.values()
    return {
        definition.instance_id: all_windows[definition.instance_id]
        for definition in selected
    }


def validate_contiguous_non_overlapping_windows(
    today: date | None = None,
) -> bool:
    try:
        anchor = today or date.today()
        validate_automatic_windows(anchor, resolve_automatic_windows(anchor))
    except ValueError:
        return False
    return True
