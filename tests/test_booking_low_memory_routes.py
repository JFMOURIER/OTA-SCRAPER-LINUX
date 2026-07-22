from __future__ import annotations
import os
import unittest
from datetime import date
from unittest.mock import patch

from app import CollectionConfig, collector_options_kwargs
from collectors.booking_playwright import BookingPlaywrightCollector


class BookingLowMemoryRouteTests(unittest.TestCase):
    def test_balanced_booking_enables_low_memory_routing(self):
        config = CollectionConfig(
            source="Booking.com", city_or_region="Orlando", checkin_start=date(2026, 8, 1),
            checkin_end=date(2026, 8, 1), nights=1, adults=2, currency="USD", max_hotels=10,
            headless=False, collect_all_available=False, max_scroll_minutes=5,
            selected_star_ratings=(1, 2, 3, 4, 5), include_unknown_star_rating=True,
            debug_mode=False, screenshots_enabled=False, fast_mode=False, performance_mode="balanced",
            block_images_and_fonts=False, test_mode=False, db_backend="sqlite", hotels_only=True,
            disable_filters_during_complete_collection=False, ultra_reliable_loading_mode=False,
            resume_previous_run=False,
        )
        with patch.dict(os.environ, {"OTA_LOW_MEMORY_MODE": "1"}):
            self.assertTrue(collector_options_kwargs(config)["block_images_and_fonts"])

    def test_only_heavy_resource_types_are_blocked(self):
        collector = BookingPlaywrightCollector()
        for resource_type in ("image", "font", "media"):
            self.assertTrue(collector._should_block_resource_type(resource_type))
        for resource_type in ("document", "script", "fetch", "xhr"):
            self.assertFalse(collector._should_block_resource_type(resource_type))
