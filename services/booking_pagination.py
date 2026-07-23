from __future__ import annotations

import os
import re
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psutil


MB = 1024 * 1024
BROWSER_MARKERS = ("chromium", "chrome", "playwright", "headless_shell")
NORMAL_PAGE_SIZE = 25


def canonical_hotel_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    if not path:
        return None
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def hotel_identity(row: dict[str, Any]) -> str:
    canonical_url = canonical_hotel_url(row.get("hotel_url") or row.get("url"))
    if canonical_url:
        return f"url:{canonical_url}"
    name = " ".join(
        str(row.get("hotel_name") or row.get("name") or "").strip().lower().split()
    )
    return f"name:{name}" if name else ""


class PaginationResults:
    """Date-scoped, deterministic hotel rows retained across small result pages."""

    def __init__(self, rows: Iterable[dict[str, Any]] = ()) -> None:
        self._rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.add(rows)

    def add(self, rows: Iterable[dict[str, Any]]) -> int:
        added = 0
        for source in rows:
            row = dict(source)
            canonical_url = canonical_hotel_url(row.get("hotel_url"))
            if canonical_url:
                row["hotel_url"] = canonical_url
            key = hotel_identity(row)
            if not key:
                continue
            if key in self._rows:
                current = self._rows[key]
                for field, value in row.items():
                    if value not in (None, "", [], {}):
                        current[field] = value
                continue
            self._rows[key] = row
            added += 1
        return added

    def values(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows.values()]

    def successful_count(self) -> int:
        return sum(
            1
            for row in self._rows.values()
            if row.get("hotel_name")
            and str(row.get("collection_status") or "success") == "success"
        )

    def __len__(self) -> int:
        return len(self._rows)


def url_with_offset(url: str, offset: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if offset > 0:
        query["offset"] = str(int(offset))
    else:
        query.pop("offset", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def next_resume_offset(unique_count: int, page_size: int = NORMAL_PAGE_SIZE) -> int:
    count = max(0, int(unique_count))
    if count == 0:
        return 0
    return ((count + page_size - 1) // page_size) * page_size


@dataclass(frozen=True, slots=True)
class PaginationResourceSnapshot:
    available_ram_mb: float
    swap_free_mb: float
    swap_percent: float
    browser_rss_mb: float
    browser_pss_mb: float
    browser_process_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pagination_resource_snapshot(owner_pid: int | None = None) -> PaginationResourceSnapshot:
    owner_pid = int(owner_pid or os.getpid())
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        descendants = psutil.Process(owner_pid).children(recursive=True)
    except psutil.Error:
        descendants = []
    rss = 0
    pss = 0
    count = 0
    for process in descendants:
        try:
            name = process.name().lower()
            command = " ".join(process.cmdline()).lower()
            if not any(marker in name or marker in command for marker in BROWSER_MARKERS):
                continue
            rss += process.memory_info().rss
            pss += int(getattr(process.memory_full_info(), "pss", 0) or 0)
            count += 1
        except psutil.Error:
            continue
    return PaginationResourceSnapshot(
        available_ram_mb=round(memory.available / MB, 1),
        swap_free_mb=round(swap.free / MB, 1),
        swap_percent=round(float(swap.percent), 1),
        browser_rss_mb=round(rss / MB, 1),
        browser_pss_mb=round(pss / MB, 1),
        browser_process_count=count,
    )


def evaluate_pagination_resources(
    snapshot: PaginationResourceSnapshot,
) -> tuple[str, str]:
    emergency_available = float(os.getenv("OTA_EMERGENCY_AVAILABLE_MB", "500"))
    soft_available = float(os.getenv("OTA_PAGINATION_SOFT_AVAILABLE_MB", "900"))
    max_swap_percent = float(os.getenv("OTA_MAX_SWAP_PERCENT", "94"))
    minimum_swap_free = float(os.getenv("OTA_MIN_SWAP_FREE_MB", "256"))
    soft_browser = float(os.getenv("OTA_PAGINATION_SOFT_BROWSER_PSS_MB", "2400"))

    if snapshot.available_ram_mb < emergency_available:
        return "hard", f"available RAM {snapshot.available_ram_mb:.0f} MB"
    swap_pressure = (
        snapshot.swap_percent >= max_swap_percent
        or snapshot.swap_free_mb < minimum_swap_free
    )
    if swap_pressure and snapshot.available_ram_mb < soft_available:
        return (
            "soft",
            f"swap {snapshot.swap_percent:.1f}% used with {snapshot.swap_free_mb:.0f} MB free "
            f"and available RAM {snapshot.available_ram_mb:.0f} MB",
        )
    effective_browser = (
        snapshot.browser_pss_mb
        if snapshot.browser_pss_mb > 0
        else snapshot.browser_rss_mb
    )
    if effective_browser >= soft_browser:
        return "soft", f"browser memory {effective_browser:.0f} MB"
    if swap_pressure:
        return (
            "warning",
            f"swap {snapshot.swap_percent:.1f}% used with {snapshot.swap_free_mb:.0f} MB free; "
            f"available RAM remains {snapshot.available_ram_mb:.0f} MB",
        )
    return "ok", "within pagination limits"
