from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback
from services.date_utils import format_timestamp_for_filename
from services.playwright_safe import (
    interruptible_page_wait,
    launch_managed_chromium_context,
    safe_close_browser,
    safe_page_title,
    safe_page_url,
    safe_screenshot,
)
from services.scraper_errors import AccessRestrictionError, BrowserClosedError, is_access_restriction_error, is_browser_closed_error


class ExpediaPlaywrightCollector(BaseCollector):
    source_name = "Expedia"

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
        params = {
            "destination": city_or_region,
            "startDate": checkin_date.isoformat(),
            "endDate": checkout_date.isoformat(),
            "rooms": f"1:{adults}",
        }
        search_url = f"https://www.expedia.com/Hotel-Search?{urlencode(params)}"
        screenshot_dir = options.screenshot_dir or Path("data") / "screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict] = []

        self.log(log_callback, f"Launching Expedia Chromium session with headless={options.headless}.")
        self.progress(progress_callback, 0.05, "Opening browser")

        with sync_playwright() as playwright:
            browser = None
            context = None
            page = None
            try:
                browser, context, implementation = launch_managed_chromium_context(
                    playwright,
                    headless=options.headless,
                    profile_dir=options.browser_profile_dir,
                    args=["--disable-dev-shm-usage", "--disable-background-networking"],
                    viewport={"width": 1366, "height": 900},
                    log=log_callback,
                )
                options.stats["browser_implementation"] = implementation
                page = context.new_page()
                page.set_default_timeout(15000)
                page.set_default_navigation_timeout(45000)
                self.log(log_callback, "Expedia Chromium browser launched.")
                self.log(log_callback, "Expedia Playwright page ready with 15s selector timeout and 45s navigation timeout.")
                self.log(log_callback, f"Search URL opened: {search_url}")
                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                except PlaywrightTimeoutError as exc:
                    self.log(log_callback, f"Expedia page load timed out: {exc}")
                    raise
                if not interruptible_page_wait(page, 3000, options.stop_event):
                    raise RuntimeError("stopped_by_user: cancelled during Expedia page load")
                self.log(log_callback, f"Final URL after redirect: {safe_page_url(page, log_callback)}")
                self.log(log_callback, f"Page title: {safe_page_title(page, log_callback)}")
                initial_screenshot = screenshot_dir / f"expedia_initial_{format_timestamp_for_filename()}.png"
                if options.screenshots_enabled:
                    if safe_screenshot(page, initial_screenshot, "expedia initial", log_callback):
                        self.log(log_callback, f"Initial screenshot saved: {initial_screenshot.resolve()}")

                # Expedia selectors change frequently. These target visible property cards only.
                card_selector = "[data-stid='property-listing-results'] [data-stid='property-card'], [data-stid='lodging-card-responsive']"
                try:
                    page.wait_for_selector(card_selector, timeout=20000)
                except PlaywrightTimeoutError:
                    self.log(log_callback, "Expedia opened, but zero hotel cards were detected with current selectors.")

                unchanged_attempts = 0
                last_count = page.locator(card_selector).count()
                max_seconds = max(1, int(options.max_scroll_minutes)) * 60
                started = time.monotonic()
                self.log(log_callback, f"Scroll 0: {last_count} Expedia cards found")
                for scroll_index in range(200):
                    if self.should_stop(options):
                        self.log(log_callback, "Stop requested during Expedia scrolling.")
                        break
                    if not collect_all_available_hotels and last_count >= int(max_hotels):
                        break
                    if time.monotonic() - started >= max_seconds:
                        self.log(log_callback, f"Maximum scroll time reached ({options.max_scroll_minutes} minutes); stopping Expedia scroll.")
                        break
                    page.mouse.wheel(0, 1800)
                    if not interruptible_page_wait(page, 900 if options.fast_mode else 1400, options.stop_event):
                        self.log(log_callback, "Stop requested during Expedia scroll wait.")
                        break
                    current_count = page.locator(card_selector).count()
                    self.log(log_callback, f"Scroll {scroll_index + 1}: {current_count} Expedia cards found")
                    if current_count > last_count:
                        unchanged_attempts = 0
                        last_count = current_count
                    else:
                        unchanged_attempts += 1
                        self.log(log_callback, f"No new Expedia cards after scroll attempt {unchanged_attempts} of 5")
                    if unchanged_attempts >= 5:
                        break
                    self.progress(progress_callback, 0.15 + scroll_index * 0.08, "Loading more hotel cards")

                cards = page.locator(card_selector)
                card_count = cards.count() if collect_all_available_hotels else min(cards.count(), int(max_hotels))
                self.log(log_callback, f"Found {card_count} Expedia cards to process.")

                for index in range(card_count):
                    if self.should_stop(options):
                        self.log(log_callback, "Stop requested while extracting Expedia cards.")
                        break
                    card = cards.nth(index)
                    try:
                        name = self._text(card, "[data-stid='content-hotel-title'], h3")
                        price = self._text(card, "[data-stid='price-lockup-text'], [data-test-id='price-summary']")
                        review_score = self._text(card, "[data-stid='reviews-rating']")
                        review_count = self._text(card, "[data-stid='reviews-total']")
                        link = self._attr(card, "a", "href")

                        results.append(
                            self.normalized_row(
                                city_or_region=city_or_region,
                                checkin_date=checkin_date,
                                checkout_date=checkout_date,
                                adults=adults,
                                currency=currency,
                                hotel_name=name,
                                hotel_url=link,
                                review_score=review_score,
                                review_count=review_count,
                                cheapest_price_total=price,
                                provider_name="Expedia",
                                collection_status="success" if name else "failed",
                                error_message=None if name else "Hotel name selector returned no value.",
                                ranking_position_on_page=index + 1,
                                search_url=search_url,
                                extra={
                                    "ota_hotel_id": None,
                                    "screenshot_path": str(initial_screenshot),
                                },
                            )
                        )
                    except Exception as exc:
                        results.append(self._failed_result(exc, city_or_region, search_url, checkin_date, checkout_date, number_of_nights, adults, currency, index + 1))

                    self.progress(progress_callback, 0.45 + ((index + 1) / max(card_count, 1)) * 0.45, f"Processed Expedia card {index + 1}")
                    time.sleep(0.4)

                final_screenshot = screenshot_dir / f"expedia_final_{format_timestamp_for_filename()}.png"
                if options.screenshots_enabled:
                    if safe_screenshot(page, final_screenshot, "expedia final", log_callback):
                        self.log(log_callback, f"Final screenshot saved: {final_screenshot.resolve()}")
            except Exception as exc:
                if is_browser_closed_error(exc):
                    raise BrowserClosedError(str(exc)) from exc
                if is_access_restriction_error(exc):
                    raise AccessRestrictionError(str(exc)) from exc
                self.log(log_callback, f"Expedia scraping failed: {exc}")
                self.log(log_callback, "If Chromium failed to start on Linux, run: python -m playwright install --with-deps chromium")
                raise
            finally:
                safe_close_browser(browser=browser, context=context, page=page, log=log_callback)
                self.log(log_callback, "Expedia browser closed.")

        self.progress(progress_callback, 1.0, "Expedia collection complete")
        return results

    def _text(self, locator, selector: str) -> str | None:
        child = locator.locator(selector).first()
        if child.count() == 0:
            return None
        text = child.inner_text(timeout=3000).strip()
        return text or None

    def _attr(self, locator, selector: str, attr: str) -> str | None:
        child = locator.locator(selector).first()
        if child.count() == 0:
            return None
        value = child.get_attribute(attr, timeout=3000)
        if value and value.startswith("/"):
            return f"https://www.expedia.com{value}"
        return value

    def _failed_result(self, exc, city_or_region, search_url, checkin_date, checkout_date, number_of_nights, adults, currency, position):
        return {
            **self.normalized_row(
                city_or_region=city_or_region,
                checkin_date=checkin_date,
                checkout_date=checkout_date,
                adults=adults,
                currency=currency,
                provider_name="Expedia",
                collection_status="failed",
                error_message=str(exc),
                ranking_position_on_page=position,
                search_url=search_url,
                extra={"ota_hotel_id": None, "screenshot_path": None},
            )
        }
