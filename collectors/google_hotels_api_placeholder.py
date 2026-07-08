from __future__ import annotations

from datetime import date

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback


class GoogleHotelsApiPlaceholderCollector(BaseCollector):
    source_name = "Google Hotels API provider placeholder"
    provider_name = "Google Hotels API"

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
        message = (
            "Google official Hotel APIs are mainly for Hotel Center partners to manage or send hotel prices, "
            "not a simple public competitor price retrieval API."
        )
        self.log(log_callback, message)
        return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)
