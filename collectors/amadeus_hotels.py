from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class AmadeusHotelsCollector(BaseCollector):
    source_name = "Amadeus Hotel API"
    provider_name = "Amadeus"

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
        if not os.getenv("AMADEUS_CLIENT_ID") or not os.getenv("AMADEUS_CLIENT_SECRET"):
            message = "Amadeus Hotel API requires AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in .env plus API access for the hotel search endpoints."
        else:
            message = "Amadeus credentials are present, but this collector is a placeholder until the desired hotel endpoint and access level are confirmed."
        self.log(log_callback, message)
        return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)
