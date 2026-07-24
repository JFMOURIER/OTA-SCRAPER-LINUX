from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ScheduledInstanceDefinition:
    instance_id: str
    port: int
    start_offset_days: int
    end_offset_days: int
    display: str
    timer_schedule: str
    data_dir_override: Path | None = None

    @property
    def data_dir(self) -> Path:
        return self.data_dir_override or (
            PROJECT_ROOT / "data" / "instances" / self.instance_id
        )

    @property
    def date_bucket(self) -> str:
        return self.instance_id

    def resolve_window(self, today: date | None = None) -> tuple[date, date]:
        anchor = today or date.today()
        return (
            anchor + timedelta(days=self.start_offset_days),
            anchor + timedelta(days=self.end_offset_days),
        )


SCHEDULED_INSTANCES: dict[str, ScheduledInstanceDefinition] = {
    "near_30_days": ScheduledInstanceDefinition(
        instance_id="near_30_days",
        port=8501,
        start_offset_days=0,
        end_offset_days=30,
        display=":101",
        timer_schedule="minute 05 every hour",
    ),
    "medium_31_120_days": ScheduledInstanceDefinition(
        instance_id="medium_31_120_days",
        port=8502,
        start_offset_days=31,
        end_offset_days=120,
        display=":102",
        timer_schedule="00:20 and 12:20 daily",
    ),
    "long_121_365_days": ScheduledInstanceDefinition(
        instance_id="long_121_365_days",
        port=8503,
        start_offset_days=121,
        end_offset_days=365,
        display=":103",
        timer_schedule="01:35 daily",
    ),
}


def get_scheduled_instance(instance_id: str) -> ScheduledInstanceDefinition:
    try:
        return SCHEDULED_INSTANCES[instance_id]
    except KeyError as exc:
        choices = ", ".join(SCHEDULED_INSTANCES)
        raise ValueError(
            f"Unknown scheduled instance {instance_id!r}; expected one of {choices}"
        ) from exc


def resolved_windows(
    today: date | None = None,
    definitions: Iterable[ScheduledInstanceDefinition] | None = None,
) -> dict[str, tuple[date, date]]:
    selected = definitions or SCHEDULED_INSTANCES.values()
    return {
        definition.instance_id: definition.resolve_window(today)
        for definition in selected
    }


def validate_contiguous_non_overlapping_windows(
    today: date | None = None,
) -> bool:
    windows = sorted(
        resolved_windows(today).values(),
        key=lambda value: value[0],
    )
    return all(
        current_end + timedelta(days=1) == next_start
        for (_, current_end), (next_start, _) in zip(windows, windows[1:])
    )
