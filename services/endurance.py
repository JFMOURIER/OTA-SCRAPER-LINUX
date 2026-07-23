from __future__ import annotations

import os
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


EPHEMERAL_PROFILE_MARKER = ".ota-ephemeral-profile"


def canonical_hotel_key(row: dict[str, Any]) -> str:
    """Return the stable, compact identity used while loading one Booking date."""

    raw_url = str(row.get("hotel_url") or row.get("url") or "").strip()
    if raw_url:
        try:
            parsed = urlsplit(raw_url)
            path = parsed.path.rstrip("/").lower()
            return "url:" + urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))
        except ValueError:
            return "url:" + raw_url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    name = " ".join(str(row.get("hotel_name") or row.get("name") or "").lower().split())
    return f"name:{name}" if name else ""


class IncrementalResultBuffer:
    """A date-scoped deduplicating buffer; it never contains previous dates."""

    def __init__(self, maximum_records: int) -> None:
        self.maximum_records = max(1, int(maximum_records))
        self._rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.duplicates_seen = 0

    def add(self, rows: Iterable[dict[str, Any]]) -> int:
        added = 0
        for row in rows:
            key = canonical_hotel_key(row)
            if not key:
                continue
            if key in self._rows:
                self.duplicates_seen += 1
                # The newest extraction may contain a price or rating that was
                # absent in the first rendering.  Enrich in place without
                # changing the stable ordering/ranking.
                current = self._rows[key]
                for field, value in row.items():
                    if value not in (None, "", [], {}) or current.get(field) in (None, "", [], {}):
                        current[field] = value
                continue
            if len(self._rows) >= self.maximum_records:
                break
            self._rows[key] = dict(row)
            added += 1
        return added

    def values(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows.values()]

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def compact_key_count(self) -> int:
        return len(self._rows)


def bounded_dom_settings() -> tuple[bool, int, int]:
    enabled = os.getenv("OTA_BOUND_BOOKING_DOM", "1").strip().lower() in {"1", "true", "yes", "on"}
    try:
        maximum = max(50, int(os.getenv("OTA_MAX_LIVE_BOOKING_CARDS", "100")))
    except ValueError:
        maximum = 100
    try:
        keep = max(10, int(os.getenv("OTA_KEEP_LIVE_BOOKING_CARDS", "25")))
    except ValueError:
        keep = 25
    return enabled, maximum, min(keep, maximum)


def profile_directory_size_mb(path: str | Path | None) -> float:
    if not path:
        return 0.0
    root = Path(path)
    total = 0
    try:
        for candidate in root.rglob("*"):
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 2)


def prepare_ephemeral_profile(path: str | Path) -> Path:
    """Mark a newly allocated attempt profile as safe for post-close removal."""

    profile = Path(path)
    profile.mkdir(parents=True, exist_ok=True)
    marker = profile / EPHEMERAL_PROFILE_MARKER
    marker.write_text("OTA per-date attempt profile\n", encoding="utf-8")
    return profile


def cleanup_ephemeral_profile(path: str | Path | None) -> bool:
    """Remove only an explicitly marked per-attempt profile after Chromium exits."""

    if not path:
        return False
    profile = Path(path)
    marker = profile / EPHEMERAL_PROFILE_MARKER
    if not profile.name.startswith("attempt_") or not marker.is_file():
        return False
    try:
        if marker.read_text(encoding="utf-8") != "OTA per-date attempt profile\n":
            return False
        shutil.rmtree(profile)
        try:
            profile.parent.rmdir()
        except OSError:
            pass
        return True
    except OSError:
        return False
