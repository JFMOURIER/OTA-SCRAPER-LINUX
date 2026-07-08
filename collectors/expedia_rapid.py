from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class ExpediaRapidCollector(BaseCollector):
    source_name = "Expedia Rapid API"
    provider_name = "Expedia Rapid"

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
        load_dotenv(dotenv_path=ENV_PATH, override=False)
        if not os.getenv("EXPEDIA_RAPID_API_KEY") or not os.getenv("EXPEDIA_RAPID_SECRET"):
            message = "Expedia Rapid API requires EXPEDIA_RAPID_API_KEY and EXPEDIA_RAPID_SECRET in .env plus approved Rapid API access."
        else:
            message = "Expedia Rapid credentials are present, but this collector is a placeholder until property search and rate-shop endpoint access is confirmed."
        self.log(log_callback, message)
        return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)
