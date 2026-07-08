from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta


def calculate_checkout_date(checkin_date: date, number_of_nights: int) -> date:
    return checkin_date + timedelta(days=int(number_of_nights))


def split_date_range_into_blocks(start_date: date, end_date: date, split_mode: str) -> list[tuple[date, date]]:
    mode = (split_mode or "custom").strip().lower()
    if end_date < start_date:
        end_date = start_date
    if mode in {"single", "custom"}:
        return [(start_date, end_date)]
    if mode not in {"weekly", "monthly"}:
        raise ValueError(f"Unsupported split mode: {split_mode}")

    blocks: list[tuple[date, date]] = []
    cursor = start_date
    while cursor <= end_date:
        if mode == "weekly":
            block_end = min(cursor + timedelta(days=6), end_date)
        else:
            last_day = calendar.monthrange(cursor.year, cursor.month)[1]
            block_end = min(date(cursor.year, cursor.month, last_day), end_date)
        blocks.append((cursor, block_end))
        cursor = block_end + timedelta(days=1)
    return blocks


def format_date_for_filename(date_value: date | datetime | str) -> str:
    if isinstance(date_value, datetime):
        value = date_value.date()
    elif isinstance(date_value, date):
        value = date_value
    else:
        return str(date_value).replace("-", "_")
    return value.strftime("%Y_%m_%d")


def format_timestamp_for_filename(datetime_value: datetime | None = None) -> str:
    value = datetime_value or datetime.now()
    return value.strftime("%Y_%m_%d_%H%M%S")
