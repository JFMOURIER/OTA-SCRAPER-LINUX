from __future__ import annotations

from datetime import date

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback


class CustomApiCollector(BaseCollector):
    source_name = "Custom API"
    provider_name = "Custom API"

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
        message = "Custom API is a placeholder. Add a provider module that returns the normalized hotel row fields."
        self.log(log_callback, message)
        return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)
