from __future__ import annotations

from abc import ABC, abstractmethod
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable

from services.date_utils import calculate_checkout_date


ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]
StatusCallback = Callable[[dict[str, Any]], None]
PartialResultsCallback = Callable[[date, list[dict[str, Any]], dict[str, Any]], None]


@dataclass(slots=True)
class CollectorOptions:
    collect_all_available: bool = False
    max_scroll_minutes: int = 5
    selected_star_ratings: tuple[int, ...] = (1, 2, 3, 4, 5)
    include_unknown_star_rating: bool = True
    debug_mode: bool = False
    screenshots_enabled: bool = True
    headless: bool = True
    fast_mode: bool = False
    performance_mode: str = "balanced"
    block_images_and_fonts: bool = False
    stop_event: Event | None = None
    hotels_only: bool = True
    disable_filters_during_complete_collection: bool = False
    ultra_reliable_loading_mode: bool = False
    screenshot_dir: Path | None = None
    screenshots_dir: Path | None = None
    debug_dir: Path | None = None
    checkpoint_dir: Path | None = None
    status_dir: Path | None = None
    exports_dir: Path | None = None
    log_dir: Path | None = None
    instance_data_dir: Path | None = None
    browser_profile_dir: Path | None = None
    status_callback: StatusCallback | None = None
    partial_results_callback: PartialResultsCallback | None = None
    partial_dir: Path | None = None
    resume_partial_results: bool = False
    resource_check_callback: Callable[[], tuple[str, dict[str, object]]] | None = None
    current_attempt: int = 1
    stats: dict[str, object] = field(default_factory=dict)


class BaseCollector(ABC):
    source_name = "Base"
    provider_name = "Base"

    @abstractmethod
    def collect(
        self,
        city_or_region: str,
        checkin_date: date,
        checkout_date: date,
        adults: int,
        currency: str,
        max_hotels: int,
        collect_all_available_hotels: bool,
        options: CollectorOptions | None = None,
        progress_callback: ProgressCallback | None = None,
        log_callback: LogCallback | None = None,
    ) -> list[dict]:
        raise NotImplementedError

    def log(self, log_callback: LogCallback | None, message: str) -> None:
        if log_callback:
            log_callback(message)

    def progress(self, progress_callback: ProgressCallback | None, value: float, message: str) -> None:
        if progress_callback:
            progress_callback(value, message)

    def should_stop(self, options: CollectorOptions | None) -> bool:
        return bool(options and options.stop_event and options.stop_event.is_set())

    def number_of_nights(self, checkin_date: date, checkout_date: date) -> int:
        return max((checkout_date - checkin_date).days, 1)

    def normalized_row(
        self,
        *,
        city_or_region: str,
        checkin_date: date,
        checkout_date: date | None = None,
        adults: int = 2,
        currency: str | None = None,
        hotel_name: Any = None,
        hotel_url: Any = None,
        star_rating: Any = None,
        review_score: Any = None,
        review_count: Any = None,
        cheapest_price_total: Any = None,
        room_name: Any = None,
        taxes_and_fees_text: Any = None,
        provider_name: str | None = None,
        raw_source_payload: Any = None,
        collection_status: str = "success",
        error_message: str | None = None,
        ranking_position_on_page: int | None = None,
        search_url: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checkout_date = checkout_date or calculate_checkout_date(checkin_date, 1)
        nights = self.number_of_nights(checkin_date, checkout_date)
        payload = raw_source_payload
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=True, default=str)
        row = {
            "source": self.source_name,
            "city_or_region": city_or_region,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "number_of_nights": nights,
            "adults": adults,
            "hotel_name": hotel_name,
            "hotel_url": hotel_url,
            "star_rating": star_rating,
            "review_score": review_score,
            "review_count": review_count,
            "cheapest_price_total": cheapest_price_total,
            "currency": currency,
            "room_name": room_name,
            "cheapest_room_name": room_name,
            "taxes_and_fees_text": taxes_and_fees_text,
            "provider_name": provider_name or self.provider_name,
            "raw_source_payload": payload,
            "collection_status": collection_status,
            "error_message": error_message,
            "collected_at": datetime.now(),
            "ranking_position_on_page": ranking_position_on_page,
            "search_url": search_url,
        }
        if extra:
            row.update(extra)
        return row

    def unavailable_result(
        self,
        message: str,
        city_or_region: str,
        checkin_date: date,
        checkout_date: date,
        adults: int,
        currency: str,
    ) -> list[dict[str, Any]]:
        return [
            self.normalized_row(
                city_or_region=city_or_region,
                checkin_date=checkin_date,
                checkout_date=checkout_date,
                adults=adults,
                currency=currency,
                collection_status="failed",
                error_message=message,
            )
        ]
