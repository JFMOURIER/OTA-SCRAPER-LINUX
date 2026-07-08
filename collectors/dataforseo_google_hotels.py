from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class DataForSeoGoogleHotelsCollector(BaseCollector):
    source_name = "DataForSEO Google Hotels"
    provider_name = "DataForSEO"

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
        if not os.getenv("DATAFORSEO_LOGIN") or not os.getenv("DATAFORSEO_PASSWORD"):
            message = "DataForSEO Google Hotels requires DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in .env plus confirmed API task documentation."
        else:
            message = "DataForSEO credentials are present, but this collector is a placeholder until the target Google Hotels endpoint/task mapping is confirmed."
        self.log(log_callback, message)
        return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)
