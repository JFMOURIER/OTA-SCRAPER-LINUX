from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback
from services.normalizer import parse_price_to_decimal, parse_review_count, parse_review_score, parse_star_rating


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class SerpApiGoogleHotelsCollector(BaseCollector):
    source_name = "SerpApi Google Hotels"
    provider_name = "SerpApi"
    endpoint = "https://serpapi.com/search.json"

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
        options = options or CollectorOptions()
        load_dotenv(dotenv_path=ENV_PATH, override=False)
        api_key = os.getenv("SERPAPI_API_KEY", "").strip()
        if not api_key:
            message = "SERPAPI_API_KEY is missing. Add it to .env to use this source."
            self.log(log_callback, message)
            return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)

        params = {
            "engine": "google_hotels",
            "q": city_or_region,
            "check_in_date": checkin_date.isoformat(),
            "check_out_date": checkout_date.isoformat(),
            "adults": adults,
            "currency": currency,
            "api_key": api_key,
        }
        url = f"{self.endpoint}?{urlencode(params)}"
        self.progress(progress_callback, 0.1, "Calling SerpApi Google Hotels")
        self.log(log_callback, "Calling SerpApi Google Hotels API.")
        try:
            payload = self._get_json(url)
            options.stats["api_requests_used"] = int(options.stats.get("api_requests_used", 0)) + 1
        except Exception as exc:
            message = f"SerpApi Google Hotels request failed: {exc}"
            self.log(log_callback, message)
            return self.unavailable_result(message, city_or_region, checkin_date, checkout_date, adults, currency)

        hotels = self._extract_hotels(payload)
        if not collect_all_available_hotels:
            hotels = hotels[: max(1, int(max_hotels))]

        rows: list[dict[str, Any]] = []
        for index, hotel in enumerate(hotels, start=1):
            price = self._price_total(hotel)
            rows.append(
                self.normalized_row(
                    city_or_region=city_or_region,
                    checkin_date=checkin_date,
                    checkout_date=checkout_date,
                    adults=adults,
                    currency=hotel.get("currency") or currency,
                    hotel_name=hotel.get("name") or hotel.get("title"),
                    hotel_url=hotel.get("link") or hotel.get("hotel_link") or hotel.get("booking_link"),
                    star_rating=parse_star_rating(hotel.get("hotel_class") or hotel.get("stars") or hotel.get("star_rating")),
                    review_score=parse_review_score(hotel.get("overall_rating") or hotel.get("rating") or hotel.get("review_score")),
                    review_count=parse_review_count(hotel.get("reviews") or hotel.get("review_count")),
                    cheapest_price_total=price,
                    room_name=self._room_name(hotel),
                    taxes_and_fees_text=self._taxes_text(hotel),
                    raw_source_payload=hotel,
                    ranking_position_on_page=index,
                    search_url=url,
                )
            )
            self.progress(progress_callback, 0.15 + (index / max(len(hotels), 1)) * 0.8, f"Parsed SerpApi hotel {index} of {len(hotels)}")

        if not rows:
            rows = self.unavailable_result("SerpApi returned no hotel rows for this search.", city_or_region, checkin_date, checkout_date, adults, currency)
        self.progress(progress_callback, 1.0, f"SerpApi complete: {len(rows)} row(s)")
        return rows

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "Hotel Price Collector/1.0"})
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))

    def _extract_hotels(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("properties", "hotels", "hotel_results", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _price_total(self, hotel: dict[str, Any]) -> Decimal | None:
        candidates = [
            hotel.get("total_rate"),
            hotel.get("price"),
            hotel.get("lowest_price"),
            hotel.get("extracted_total_rate"),
        ]
        for nested_key in ("total_rate", "rate_per_night", "price"):
            nested = hotel.get(nested_key)
            if isinstance(nested, dict):
                candidates.extend([nested.get("extracted_lowest"), nested.get("lowest"), nested.get("extracted_total"), nested.get("total")])
        for candidate in candidates:
            parsed = parse_price_to_decimal(candidate)
            if parsed is not None:
                return parsed
        return None

    def _room_name(self, hotel: dict[str, Any]) -> str | None:
        prices = hotel.get("prices")
        if isinstance(prices, list) and prices:
            first = prices[0]
            if isinstance(first, dict):
                return first.get("room") or first.get("room_name") or first.get("source")
        return hotel.get("room_name")

    def _taxes_text(self, hotel: dict[str, Any]) -> str | None:
        for key in ("taxes_and_fees", "taxes_and_fees_text", "taxes"):
            value = hotel.get(key)
            if value:
                return str(value)
        return None
