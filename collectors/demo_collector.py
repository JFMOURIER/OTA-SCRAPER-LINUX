from __future__ import annotations

import random
import time
from datetime import datetime

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback


class DemoCollector(BaseCollector):
    source_name = "Demo"

    HOTEL_WORDS = [
        "Grand",
        "Harbor",
        "Garden",
        "Riverside",
        "Metropolitan",
        "Sunset",
        "Royal",
        "Central",
        "Vista",
        "Palm",
    ]

    HOTEL_TYPES = ["Hotel", "Suites", "Resort", "Inn", "Lodge", "Apartments"]

    def collect(
        self,
        city_or_region,
        checkin_date,
        checkout_date,
        adults,
        currency,
        max_hotels,
        collect_all_available_hotels,
        options: CollectorOptions | None = None,
        progress_callback: ProgressCallback | None = None,
        log_callback: LogCallback | None = None,
    ) -> list[dict]:
        options = options or CollectorOptions()
        number_of_nights = self.number_of_nights(checkin_date, checkout_date)
        requested_count = max(1, int(max_hotels))
        results: list[dict] = []

        self.log(log_callback, "Starting demo collection with generated hotel data.")
        self.progress(progress_callback, 0.05, "Preparing demo data")
        time.sleep(0.2)

        for index in range(requested_count):
            if self.should_stop(options):
                self.log(log_callback, "Stop requested during demo collection.")
                break
            if index % 10 == 0:
                self.log(log_callback, f"Generated {index} of {requested_count} demo records.")

            star_rating = random.choice([3, 3.5, 4, 4.5, 5])
            review_score = round(random.uniform(7.2, 9.6), 1)
            base_price = random.randint(80, 390)
            total_price = base_price * int(number_of_nights)
            hotel_name = f"{random.choice(self.HOTEL_WORDS)} {city_or_region} {random.choice(self.HOTEL_TYPES)}"

            room_name = random.choice(["Standard Room", "Deluxe Room", "Queen Room", "King Suite"])
            results.append(
                self.normalized_row(
                    city_or_region=city_or_region,
                    checkin_date=checkin_date,
                    checkout_date=checkout_date,
                    adults=adults,
                    currency=currency,
                    hotel_name=hotel_name,
                    hotel_url=f"demo://hotel/{index + 1:05d}",
                    star_rating=star_rating,
                    review_score=review_score,
                    review_count=random.randint(25, 4200),
                    cheapest_price_total=total_price,
                    room_name=room_name,
                    taxes_and_fees_text="Taxes and fees included for demo data",
                    provider_name="Demo",
                    ranking_position_on_page=index + 1,
                    search_url="demo://hotel-price-collector",
                    extra={
                        "ota_hotel_id": f"DEMO-{index + 1:05d}",
                        "raw_price_text": f"{currency} {total_price}",
                        "parsed_price": total_price,
                        "screenshot_path": None,
                    },
                )
            )

            progress = 0.05 + ((index + 1) / requested_count) * 0.9
            self.progress(progress_callback, progress, f"Collected demo hotel {index + 1} of {requested_count}")
            time.sleep(0.03)

        self.log(log_callback, f"Demo collection complete with {len(results)} records.")
        self.progress(progress_callback, 1.0, "Demo collection complete")
        return results
