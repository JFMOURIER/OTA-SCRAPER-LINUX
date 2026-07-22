from __future__ import annotations

import json
import re
import time
import traceback
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from collectors.base import BaseCollector, CollectorOptions, LogCallback, ProgressCallback
from services.date_utils import calculate_checkout_date, format_timestamp_for_filename
from services.normalizer import clean_hotel_name, is_probable_hotel, parse_price_to_decimal, parse_star_rating, property_type_guess
from services.playwright_safe import (
    interruptible_page_wait,
    launch_managed_chromium_context,
    safe_close_browser,
    safe_page_title,
    safe_page_url,
    safe_screenshot,
)
from services.scraper_errors import AccessRestrictionError, BrowserClosedError, is_browser_closed_error


def extract_booking_star_rating(card) -> int | None:
    details = extract_booking_star_rating_details(card)
    rating = details.get("rating")
    return int(rating) if rating is not None else None


def extract_booking_star_rating_details(card) -> dict:
    try:
        return card.evaluate(
            r"""
            (card) => {
                const starCharsRe = /[\u2605\u2b50\u2729]/g;
                const wordNumbers = {
                    one: 1, two: 2, three: 3, four: 4, five: 5,
                    un: 1, une: 1, deux: 2, trois: 3, quatre: 4, cinq: 5
                };

                const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
                const hasStarWord = (value) => /\b(star|stars|etoile|etoiles|\u00e9toile|\u00e9toiles)\b/i.test(value);
                const parseSignal = (value) => {
                    const text = clean(value);
                    if (!text) return null;
                    const starChars = text.match(starCharsRe) || [];
                    if (starChars.length >= 1 && starChars.length <= 5) return starChars.length;
                    const bounded = text.match(/\b([1-5])\s*(?:out\s+of|of|sur)\s*5\b/i);
                    if (bounded) return Number(bounded[1]);
                    const numeric = text.match(/\b([1-5])\s*(?:star|stars|etoile|etoiles|\u00e9toile|\u00e9toiles)\b/i);
                    if (numeric) return Number(numeric[1]);
                    const lowered = text.toLowerCase();
                    if (hasStarWord(lowered)) {
                        for (const [word, number] of Object.entries(wordNumbers)) {
                            if (new RegExp(`\\b${word}\\b`, "i").test(lowered)) return number;
                        }
                    }
                    return null;
                };

                const uniqueRoots = [];
                const seen = new Set();
                const pushRoot = (element, scope) => {
                    if (!element || seen.has(element)) return;
                    seen.add(element);
                    uniqueRoots.push({ element, scope });
                };

                const title = card.querySelector("[data-testid='title'], a[data-testid='title-link'], h3");
                if (title) {
                    pushRoot(title, "hotel title");
                    let ancestor = title.parentElement;
                    for (let depth = 1; ancestor && ancestor !== card && depth <= 5; depth += 1) {
                        pushRoot(ancestor, `title ancestor ${depth}`);
                        ancestor = ancestor.parentElement;
                    }
                    if (title.parentElement) {
                        Array.from(title.parentElement.children).slice(0, 8).forEach((child, index) => {
                            pushRoot(child, `title sibling area ${index + 1}`);
                        });
                    }
                }

                Array.from(card.querySelectorAll("[data-testid='rating-stars'], [data-testid*='rating-stars'], [aria-label*='star' i], [aria-label*='etoile' i], [aria-label*='\u00e9toile' i], [role='img'][aria-label], [title*='star' i], [title*='etoile' i], [title*='\u00e9toile' i]")).forEach((element) => {
                    pushRoot(element, "explicit star/rating element");
                    if (element.parentElement) pushRoot(element.parentElement, "explicit star/rating parent");
                });

                pushRoot(card, "full card fallback");

                const collectAttrs = (root) => {
                    const values = [];
                    const elements = [root, ...Array.from(root.querySelectorAll("[aria-label], [title], [alt]")).slice(0, 80)];
                    for (const element of elements) {
                        for (const attr of ["aria-label", "title", "alt"]) {
                            const value = clean(element.getAttribute(attr));
                            if (value) values.push({ attr, value });
                        }
                    }
                    return values;
                };

                const countStarSvgs = (root) => {
                    const ratingContainers = [];
                    if ((root.getAttribute("data-testid") || "").toLowerCase().includes("rating-stars")) {
                        ratingContainers.push(root);
                    }
                    ratingContainers.push(...Array.from(root.querySelectorAll("[data-testid='rating-stars'], [data-testid*='rating-stars']")));
                    if (ratingContainers.length) {
                        const count = ratingContainers.reduce((total, element) => total + element.querySelectorAll("svg").length, 0);
                        if (count >= 1 && count <= 5) return count;
                    }

                    const svgs = Array.from(root.querySelectorAll("svg"));
                    if (svgs.length < 1 || svgs.length > 8) return 0;
                    const yellowSvgs = svgs.filter((svg) => {
                        const style = window.getComputedStyle(svg);
                        const signal = [
                            style.color,
                            style.fill,
                            style.stroke,
                            svg.getAttribute("fill"),
                            svg.getAttribute("stroke"),
                            svg.getAttribute("class"),
                            svg.getAttribute("aria-label"),
                            svg.getAttribute("role")
                        ].join(" ").toLowerCase();
                        return signal.includes("febb02") || signal.includes("ffb700") || signal.includes("yellow") || signal.includes("rgb(254, 187, 2)");
                    });
                    if (yellowSvgs.length >= 1 && yellowSvgs.length <= 5) return yellowSvgs.length;
                    return 0;
                };

                const candidates = [];
                for (const { element, scope } of uniqueRoots) {
                    const text = clean(element.innerText);
                    const attrs = collectAttrs(element);
                    const iconCount = countStarSvgs(element);
                    const textRating = parseSignal(text);
                    if (textRating) {
                        candidates.push({
                            rating: textRating,
                            raw_star_signal: text.match(starCharsRe)?.join("") || text,
                            aria_label: null,
                            star_icon_count: iconCount,
                            strategy: "visible star text",
                            scope
                        });
                    }
                    for (const attrSignal of attrs) {
                        const attrRating = parseSignal(attrSignal.value);
                        if (attrRating) {
                            candidates.push({
                                rating: attrRating,
                                raw_star_signal: attrSignal.value,
                                aria_label: attrSignal.attr === "aria-label" ? attrSignal.value : null,
                                star_icon_count: iconCount,
                                strategy: `${attrSignal.attr} star label`,
                                scope
                            });
                        }
                    }
                    if (iconCount >= 1 && iconCount <= 5) {
                        candidates.push({
                            rating: iconCount,
                            raw_star_signal: `${iconCount} yellow star icon${iconCount === 1 ? "" : "s"}`,
                            aria_label: attrs.find((item) => hasStarWord(item.value))?.value || null,
                            star_icon_count: iconCount,
                            strategy: "svg star icon count",
                            scope
                        });
                    }
                }

                const valid = candidates.find((candidate) => candidate.rating >= 1 && candidate.rating <= 5);
                if (valid) {
                    return {
                        rating: valid.rating,
                        raw_star_signal: valid.raw_star_signal,
                        aria_label: valid.aria_label,
                        star_icon_count: valid.star_icon_count || 0,
                        strategy: valid.strategy,
                        scope: valid.scope,
                        missing_reason: null,
                        candidates: candidates.slice(0, 8)
                    };
                }
                return {
                    rating: null,
                    raw_star_signal: candidates[0]?.raw_star_signal || null,
                    aria_label: candidates[0]?.aria_label || null,
                    star_icon_count: candidates[0]?.star_icon_count || 0,
                    strategy: null,
                    scope: null,
                    missing_reason: "No yellow star glyph, star accessibility label, or rating-star SVG count found near the hotel title.",
                    candidates: candidates.slice(0, 8)
                };
            }
            """
        )
    except PlaywrightError as exc:
        return {
            "rating": None,
            "raw_star_signal": None,
            "aria_label": None,
            "star_icon_count": 0,
            "strategy": None,
            "scope": None,
            "missing_reason": f"Star extraction failed: {exc}",
            "candidates": [],
        }


class BookingPlaywrightCollector(BaseCollector):
    source_name = "Booking.com Playwright slow validation mode"
    provider_name = "Booking.com"

    FALLBACK_DESTINATIONS = {
        "orlando": {"dest_id": "20023488", "dest_type": "city"},
        "orlando, florida": {"dest_id": "20023488", "dest_type": "city"},
        "orlando fl": {"dest_id": "20023488", "dest_type": "city"},
    }

    CARD_SELECTORS = [
        "div[data-testid='property-card']",
        "div[data-testid='property-card-container']",
        "div[role='listitem']",
        "a[data-testid='title-link']",
    ]

    ACCESS_RESTRICTION_PATTERNS = [
        "captcha",
        "verify you are human",
        "access denied",
        "unusual traffic",
        "robot",
        "blocked",
        "are you a human",
    ]

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
        options.collect_all_available = collect_all_available_hotels
        number_of_nights = self.number_of_nights(checkin_date, checkout_date)
        params = {
            "ss": city_or_region,
            "ssne": city_or_region,
            "ssne_untouched": city_or_region,
            "checkin": checkin_date.isoformat(),
            "checkout": checkout_date.isoformat(),
            "group_adults": adults,
            "group_children": 0,
            "no_rooms": 1,
            "selected_currency": currency,
            "order": "price",
            "src": "searchresults",
            "src_elem": "sb",
            "sb": 1,
            "lang": "en-gb",
        }
        params.update(self._fallback_destination_params(city_or_region))
        search_url = f"https://www.booking.com/searchresults.en-gb.html?{urlencode(params)}"
        screenshot_dir = options.screenshot_dir or Path("data/screenshots")
        debug_dir = options.debug_dir or Path("data/debug")
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        debug_dir.mkdir(parents=True, exist_ok=True)
        browser_mode_label = "Headless reliable mode" if options.headless else "Visible debug mode"
        self.log(log_callback, f"Browser mode selected: {browser_mode_label}")
        self.log(log_callback, f"Launching Chromium with headless={options.headless}")
        results: list[dict] = []
        screenshot_paths: list[Path] = []
        error_message: str | None = None
        results_url: str | None = None
        headless_startup_path = debug_dir / "headless_startup_latest.json" if options.headless else None
        headless_startup = {
            "timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "source": self.source_name,
            "city": city_or_region,
            "checkin_date": checkin_date.isoformat(),
            "checkout_date": checkout_date.isoformat(),
            "headless": bool(options.headless),
            "instance_id": None,
            "data_dir": str(debug_dir.parent.resolve()),
            "browser_profile_dir": str(options.browser_profile_dir.resolve()) if options.browser_profile_dir else None,
            "browser_launched": "no",
            "page_created": "no",
            "first_url_opened": None,
            "last_url_opened": None,
            "page_title": None,
            "last_error": None,
        }
        if headless_startup_path:
            self._write_json_safely(headless_startup_path, headless_startup, log_callback)

        def update_headless_startup(**updates):
            if not headless_startup_path:
                return
            headless_startup.update(updates)
            headless_startup["timestamp"] = datetime.now().isoformat(sep=" ", timespec="seconds")
            self._write_json_safely(headless_startup_path, headless_startup, log_callback=None)

        def step(progress_value: float, message: str, **updates):
            if self.should_stop(options):
                raise RuntimeError(f"stopped_by_user: cancelled during {message}")
            self.progress(progress_callback, progress_value, message)
            self._notify_status(
                options,
                message,
                progress_percent=round(progress_value * 100, 2),
                latest_screenshot_path=options.stats.get("latest_screenshot_path"),
                **updates,
            )
        options.stats.update(
            {
                "current_city_or_region": city_or_region,
                "current_checkin_date": checkin_date.isoformat(),
                "current_checkout_date": checkout_date.isoformat(),
                "cookie_banner_detected": False,
                "cookie_banner_declined": False,
                "genius_popup_closed": False,
                "load_more_button_found": False,
                "load_more_clicks": int(options.stats.get("load_more_clicks", 0)),
                "load_more_button_seen_count": int(options.stats.get("load_more_button_seen_count", 0)),
                "load_more_click_count": int(options.stats.get("load_more_click_count", 0)),
                "load_more_success_count": int(options.stats.get("load_more_success_count", 0)),
                "load_more_click_fail_count": int(options.stats.get("load_more_click_fail_count", 0)),
                "last_load_more_error": None,
                "load_more_last_button_text": None,
                "load_more_last_button_strategy": None,
                "load_more_last_button_box": None,
                "load_more_last_button_visible": False,
                "load_more_last_button_enabled": False,
                "load_more_cookie_banner_visible_before_click": False,
                "load_more_bottom_scan_attempts": 0,
                "hotel_cards_before_load_more": 0,
                "hotel_cards_after_load_more": 0,
                "hotel_cards_before_last_click": 0,
                "hotel_cards_after_last_click": 0,
                "new_cards_after_last_click": 0,
                "consecutive_no_new_card_attempts": 0,
                "consecutive_no_button_attempts": 0,
                "maximum_scroll_time_reached": False,
                "final_extracted_hotel_count": 0,
                "final_stop_reason": None,
                "completion_status": None,
                "visible_result_count_text": None,
                "target_result_count": None,
                "visible_result_count_is_lower_bound": False,
                "booking_visible_result_count": None,
                "unique_results_loaded": 0,
                "estimated_missing_results": None,
                "visible_hotel_cards_count": 0,
                "unique_hotels_collected": 0,
                "new_hotels_added_last_cycle": 0,
                "lowest_price_collected": None,
                "highest_price_collected": None,
                "hotels_with_valid_price": 0,
                "hotels_missing_price": 0,
                "total_scroll_attempts": 0,
                "cycle_count": 0,
                "consecutive_no_new_unique_results": 0,
                "consecutive_no_load_more_seen": 0,
                "consecutive_failed_load_more_clicks": 0,
                "final_page_height": 0,
                "final_scroll_position": 0,
                "final_url": None,
                "final_page_title": None,
                "final_bottom_screenshot_path": None,
                "bottom_visible_text_path": None,
                "full_loading_debug_json_path": None,
                "unique_results_loaded_path": None,
                "booking_result_count_difference": None,
                "collection_completeness_warning": None,
                "unexpected_new_pages_closed": 0,
                "unexpected_property_navigation_count": 0,
                "hotel_records_extracted": 0,
                "hotels_extracted": 0,
                "hotels_kept_after_filters": 0,
                "hotels_removed_by_hotels_only_filter": 0,
                "hotels_with_known_star_rating": 0,
                "hotels_with_unknown_star_rating": 0,
                "hotels_removed_by_star_filter": 0,
                "hotels_kept_after_star_filter": 0,
                "error_message": None,
                "page_loaded": False,
                "search_verified": False,
                "sort_applied": False,
                "hotel_cards_detected": 0,
            }
        )

        self.log(log_callback, "Browser launched: starting Booking.com Chromium session.")
        step(0.03, "browser launched")

        launch_args = ["--disable-dev-shm-usage", "--disable-background-networking"]
        with sync_playwright() as playwright:
            browser_start = time.perf_counter()
            browser = None
            context = None
            page = None
            try:
                if options.browser_profile_dir:
                    self.log(log_callback, f"Using isolated Playwright profile: {options.browser_profile_dir.resolve()}")
                browser, context, implementation = launch_managed_chromium_context(
                    playwright,
                    headless=options.headless,
                    profile_dir=options.browser_profile_dir,
                    args=launch_args,
                    viewport={"width": 1366, "height": 900},
                    log=log_callback,
                )
                options.stats["browser_implementation"] = implementation
                context.set_default_timeout(15000)
                context.set_default_navigation_timeout(45000)
                self.log(log_callback, "Playwright context ready with 15s selector timeout and 45s navigation timeout.")
                update_headless_startup(browser_launched="yes")
            except Exception as exc:
                error_message = str(exc)
                update_headless_startup(last_error=error_message)
                self._write_headless_error(debug_dir, error_message, traceback.format_exc(), log_callback)
                self.log(log_callback, "Playwright Chromium may not be installed. Run: python -m playwright install --with-deps chromium")
                raise
            if options.fast_mode and options.block_images_and_fonts:
                self._enable_fast_mode_routes(context, log_callback)
            options.stats["browser_startup_time_seconds"] = round(time.perf_counter() - browser_start, 2)
            page = context.new_page()
            update_headless_startup(page_created="yes")
            results_page = page

            def close_unexpected_page(new_page):
                if new_page == results_page:
                    return
                try:
                    options.stats["unexpected_new_pages_closed"] = int(options.stats.get("unexpected_new_pages_closed", 0)) + 1
                    self.log(log_callback, "Unexpected new page opened and was closed.")
                    self._screenshot(new_page, screenshot_dir, "booking_unexpected_new_page", options, screenshot_paths, log_callback)
                    new_page.close()
                except PlaywrightError:
                    pass

            context.on("page", close_unexpected_page)

            try:
                page_load_start = time.perf_counter()
                page.goto("https://www.booking.com/", wait_until="domcontentloaded", timeout=45000)
                if not interruptible_page_wait(page, 1000 if options.fast_mode else 2500, options.stop_event):
                    raise RuntimeError("stopped_by_user: cancelled during initial page load")
                options.stats["page_load_time_seconds"] = round(float(options.stats.get("page_load_time_seconds", 0)) + (time.perf_counter() - page_load_start), 2)
                options.stats["page_loaded"] = True
                self.log(log_callback, "Booking.com homepage opened")
                self.log(log_callback, f"Final URL after redirect: {safe_page_url(page, log_callback)}")
                self.log(log_callback, f"Page title: {safe_page_title(page, log_callback)}")
                self._screenshot(page, screenshot_dir, "booking_after_homepage_open", options, screenshot_paths, log_callback)
                update_headless_startup(first_url_opened="https://www.booking.com/", last_url_opened=safe_page_url(page), page_title=safe_page_title(page))
                step(0.07, "homepage opened", latest_url=safe_page_url(page), page_title=safe_page_title(page))

                if self._detect_access_restriction(page, log_callback):
                    self._screenshot(page, screenshot_dir, "booking_access_restricted", options, screenshot_paths, log_callback)
                    raise RuntimeError("blocked_or_access_restricted: Booking.com showed CAPTCHA or access restriction wording.")

                self.handle_booking_cookie_banner(page, log_callback, options)
                self._screenshot(page, screenshot_dir, "booking_after_cookie", options, screenshot_paths, log_callback)
                step(0.10, "cookies handled", latest_url=page.url)
                self.handle_booking_popups(page, log_callback, options)
                self._screenshot(page, screenshot_dir, "booking_after_genius_popup", options, screenshot_paths, log_callback)

                homepage_search_applied = self._submit_homepage_search(
                    page,
                    city_or_region,
                    checkin_date,
                    checkout_date,
                    adults,
                    currency,
                    search_url,
                    screenshot_dir,
                    options,
                    screenshot_paths,
                    log_callback,
                )
                if not homepage_search_applied:
                    self.log(log_callback, "Fallback direct URL used.")
                    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                    if not interruptible_page_wait(page, 1000 if options.fast_mode else 2500, options.stop_event):
                        raise RuntimeError("stopped_by_user: cancelled during search navigation")
                step(0.20, "search clicked", latest_url=page.url)

                self._wait_for_results_page(page, log_callback)
                self._ensure_currency(page, currency, options, log_callback)
                results_url = page.url
                self._screenshot(page, screenshot_dir, "booking_after_search_results_load", options, screenshot_paths, log_callback)
                update_headless_startup(last_url_opened=safe_page_url(page), page_title=safe_page_title(page))
                step(0.27, "results loaded", latest_url=safe_page_url(page), page_title=safe_page_title(page))
                self.handle_booking_cookie_banner(page, log_callback, options)
                self._screenshot(page, screenshot_dir, "booking_after_results_cookie", options, screenshot_paths, log_callback)
                self.handle_booking_popups(page, log_callback, options)
                self._screenshot(page, screenshot_dir, "booking_after_results_popups", options, screenshot_paths, log_callback)
                try:
                    self._verify_search_results_or_raise(page, city_or_region, checkin_date, checkout_date, adults, log_callback)
                except RuntimeError as verify_exc:
                    self.log(log_callback, f"Search form verification failed; retrying with direct results URL. Reason: {verify_exc}")
                    self._notify_status(options, "search verification failed; retrying direct URL", progress_percent=29, latest_url=page.url)
                    last_verify_error = verify_exc
                    for retry_index, retry_url in enumerate(self._search_url_candidates(search_url, city_or_region, checkin_date, checkout_date, adults, currency), start=1):
                        try:
                            self.log(log_callback, f"Direct search retry {retry_index}: {retry_url}")
                            page.goto(retry_url, wait_until="domcontentloaded", timeout=45000)
                            if not interruptible_page_wait(page, 1000 if options.fast_mode else 2500, options.stop_event):
                                raise RuntimeError("stopped_by_user: cancelled during direct search retry")
                            self._wait_for_results_page(page, log_callback)
                            self._ensure_currency(page, currency, options, log_callback)
                            results_url = page.url
                            self._screenshot(page, screenshot_dir, f"booking_after_direct_search_retry_{retry_index}", options, screenshot_paths, log_callback)
                            self._verify_search_results_or_raise(page, city_or_region, checkin_date, checkout_date, adults, log_callback)
                            break
                        except RuntimeError as retry_exc:
                            last_verify_error = retry_exc
                            self.log(log_callback, f"Direct search retry {retry_index} did not verify: {retry_exc}")
                    else:
                        raise last_verify_error
                options.stats["search_verified"] = True
                self._apply_price_sort(page, search_url, screenshot_dir, options, screenshot_paths, log_callback)
                results_url = page.url
                self._screenshot(page, screenshot_dir, "booking_after_sort", options, screenshot_paths, log_callback)
                step(0.34, "sort applied", latest_url=page.url)
                if options.collect_all_available:
                    self.log(log_callback, "Full collection mode: Booking.com UI filters are skipped during loading.")
                elif options.hotels_only and not options.disable_filters_during_complete_collection:
                    self._apply_hotels_only_ui_filter(page, log_callback)
                elif options.disable_filters_during_complete_collection:
                    self.log(log_callback, "Complete collection test: Booking.com hotels-only UI filter disabled.")
                else:
                    self.log(log_callback, "Hotels only filter disabled.")

                self._log_page_verification(page, city_or_region, log_callback)
                self._wait_for_any_card_selector(page, log_callback)

                selector = self._choose_card_selector(page, log_callback)
                if not selector:
                    self._screenshot(page, screenshot_dir, "booking_zero_cards", options, screenshot_paths, log_callback)
                    self._log_zero_cards_help(page, screenshot_paths[-1] if screenshot_paths else None, log_callback, debug_dir)
                    if options.headless:
                        self._write_headless_zero_results_debug(page, debug_dir, screenshot_paths[-1] if screenshot_paths else None, city_or_region, None, log_callback)
                    return []

                card_count_before = self._valid_card_count(page, selector)
                options.stats["hotel_cards_detected"] = card_count_before
                self.log(log_callback, f"Hotel card count before scrolling: {card_count_before}")
                self._screenshot(page, screenshot_dir, "booking_first_cards", options, screenshot_paths, log_callback)
                step(0.42, "hotel cards detected", visible_card_count=card_count_before, current_hotel_card_count=card_count_before)

                load_more_start = time.perf_counter()
                step(0.45, "scrolling", visible_card_count=card_count_before)
                final_count = self._progressive_scroll(page, selector, max_hotels, options, progress_callback, log_callback, screenshot_dir, screenshot_paths, results_url)
                options.stats["load_more_time_seconds"] = round(float(options.stats.get("load_more_time_seconds", 0)) + (time.perf_counter() - load_more_start), 2)
                options.stats["final_cards_found"] = final_count
                self.log(log_callback, f"Hotel card count after scrolling: {final_count}")
                self._screenshot(page, screenshot_dir, "booking_final_scroll", options, screenshot_paths, log_callback)
                step(0.55, "final Load more cycle complete", visible_card_count=final_count, current_hotel_card_count=final_count, load_more_clicks=options.stats.get("load_more_clicks", 0))

                cards = self._valid_cards(page, selector)
                if not options.collect_all_available:
                    cards = cards[: int(max_hotels)]
                self._write_card_debug(cards[:10], debug_dir, options, log_callback)
                self.log(log_callback, "No property page clicks allowed during scraping.")

                self.log(log_callback, f"Hotel records extracted before filtering: starting extraction from {len(cards)} cards.")
                step(0.60, "extracting hotels", visible_card_count=len(cards))
                extraction_start = time.perf_counter()
                try:
                    results = self._bulk_extract_cards(
                        page,
                        selector,
                        len(cards),
                        city_or_region,
                        search_url,
                        checkin_date,
                        checkout_date,
                        number_of_nights,
                        adults,
                        currency,
                        screenshot_paths[-1] if screenshot_paths else None,
                        log_callback,
                        options,
                    )
                    self.progress(progress_callback, 0.90, f"Processed {len(results)} Booking.com cards")
                except Exception as exc:
                    self.log(log_callback, f"Bulk card extraction failed, falling back to per-card extraction: {exc}")
                    for index, card in enumerate(cards, start=1):
                        self._ensure_results_page(page, results_url, options, log_callback)
                        if self.should_stop(options):
                            self.log(log_callback, "Stop requested while extracting Booking.com cards.")
                            break
                        try:
                            result = self._extract_card(
                                card,
                                index,
                                city_or_region,
                                search_url,
                                checkin_date,
                                checkout_date,
                                number_of_nights,
                                adults,
                                currency,
                                screenshot_paths[-1] if screenshot_paths else None,
                                log_callback,
                            )
                            results.append(result)
                        except Exception as card_exc:
                            self.log(log_callback, f"Card {index}: extraction failed: {card_exc}")
                            results.append(self._failed_result(card_exc, city_or_region, search_url, checkin_date, checkout_date, number_of_nights, adults, currency, index, screenshot_paths[-1] if screenshot_paths else None))
                        self.progress(progress_callback, 0.55 + (index / max(len(cards), 1)) * 0.35, f"Processed Booking.com card {index} of {len(cards)}")
                options.stats["extraction_time_seconds"] = round(float(options.stats.get("extraction_time_seconds", 0)) + (time.perf_counter() - extraction_start), 2)
                options.stats["unique_hotels_collected"] = len({(row.get("hotel_url") or clean_hotel_name(row.get("hotel_name"))) for row in results if row.get("hotel_name")})

                self.log(log_callback, f"Hotel records extracted before filtering: {len(results)}")
                options.stats["hotel_records_extracted"] = len(results)
                options.stats["hotels_extracted"] = len(results)
                options.stats["final_extracted_hotel_count"] = len(results)
                if options.disable_filters_during_complete_collection:
                    self.log(log_callback, "Complete collection test: post-collection hotels-only and star filters skipped.")
                    options.stats["records_after_hotels_only_filter"] = len(results)
                    options.stats["hotels_only_removed"] = 0
                    options.stats["hotels_with_known_star_rating"] = sum(1 for row in results if row.get("star_rating") is not None)
                    options.stats["hotels_with_unknown_star_rating"] = sum(1 for row in results if row.get("star_rating") is None)
                    options.stats["hotels_removed_by_star_filter"] = 0
                    options.stats["hotels_kept_after_star_filter"] = len(results)
                else:
                    results = self._filter_hotels_only(results, options, log_callback)
                    results = self._filter_by_star_rating(results, options, log_callback)
                options.stats["hotels_kept_after_filters"] = len(results)
                options.stats["hotels_removed_by_hotels_only_filter"] = int(options.stats.get("hotels_only_removed", 0))
                self.log(log_callback, f"Hotel records after filtering: {len(results)}")

                if not results:
                    self._screenshot(page, screenshot_dir, "booking_zero_results_after_filter", options, screenshot_paths, log_callback)
                    if options.headless:
                        self._write_headless_zero_results_debug(page, debug_dir, screenshot_paths[-1] if screenshot_paths else None, city_or_region, None, log_callback)
            except Exception as exc:
                error_message = str(exc)
                tb = traceback.format_exc()
                options.stats["error_message"] = error_message
                cancelled = self.should_stop(options) or "stopped_by_user" in error_message
                if cancelled:
                    options.stats["final_stop_reason"] = "stopped_user_requested"
                    options.stats["completion_status"] = "stopped_by_user"
                    self._notify_status(options, "Stop requested; closing Booking.com browser.", status="stopping")
                    self.log(log_callback, "User cancellation received; skipping error diagnostics and closing the browser.")
                    raise
                if not options.stats.get("final_stop_reason"):
                    options.stats["final_stop_reason"] = (
                        "blocked_or_access_restricted"
                        if "blocked_or_access_restricted" in error_message
                        else "failed_unknown_error"
                    )
                if not options.stats.get("completion_status"):
                    options.stats["completion_status"] = (
                        "incomplete_access_restricted"
                        if "blocked_or_access_restricted" in error_message
                        else "incomplete_unknown_reason"
                    )
                self._screenshot(page, screenshot_dir, "booking_error", options, screenshot_paths, log_callback)
                update_headless_startup(last_url_opened=getattr(page, "url", None), last_error=error_message)
                if options.headless:
                    self._write_headless_error(debug_dir, error_message, tb, log_callback)
                    self._write_headless_zero_results_debug(page, debug_dir, screenshot_paths[-1] if screenshot_paths else None, city_or_region, error_message, log_callback, tb)
                self._notify_status(options, error_message, status="failed", last_error=error_message, latest_screenshot_path=options.stats.get("latest_screenshot_path"))
                if is_browser_closed_error(exc):
                    raise BrowserClosedError(error_message) from exc
                if "blocked_or_access_restricted" in error_message:
                    raise AccessRestrictionError(error_message) from exc
                raise
            finally:
                self._write_debug_json(page, results_url, screenshot_paths, options, debug_dir, error_message, log_callback)
                safe_close_browser(browser=browser, context=context, page=page, log=log_callback)
                self.log(log_callback, "Booking.com browser closed.")

        step(1.0, "Booking.com collection complete", hotels_collected_current_date=len(results))
        return results

    def _enable_fast_mode_routes(self, context, log_callback: LogCallback | None) -> None:
        self.log(log_callback, "Fast mode enabled: blocking images, fonts, and media; JavaScript and XHR remain allowed.")

        def route_handler(route):
            resource_type = route.request.resource_type
            if resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_handler)

    def _notify_status(self, options: CollectorOptions | None, message: str, **updates) -> None:
        if options is None:
            return
        payload = {
            "current_message": message,
            "current_checkin_date": options.stats.get("current_checkin_date"),
            "current_checkout_date": options.stats.get("current_checkout_date"),
            "visible_card_count": options.stats.get("visible_hotel_cards_count") or options.stats.get("final_cards_found"),
            "current_hotel_card_count": options.stats.get("visible_hotel_cards_count") or options.stats.get("final_cards_found"),
            "load_more_clicks": options.stats.get("load_more_clicks", 0),
            "load_more_click_count": options.stats.get("load_more_clicks", 0),
            "latest_screenshot_path": options.stats.get("latest_screenshot_path"),
            "last_error": options.stats.get("error_message"),
        }
        payload.update({key: value for key, value in updates.items() if value is not None})
        if options.status_callback:
            options.status_callback(payload)

    def _write_json_safely(self, path: Path, payload: dict, log_callback: LogCallback | None = None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            self.log(log_callback, f"Could not write debug JSON {path}: {exc}")

    def _write_headless_error(self, debug_dir: Path, error_message: str, traceback_text: str, log_callback: LogCallback | None) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = debug_dir.parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        error_payload = {
            "timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "error_message": error_message,
            "traceback": traceback_text,
            "playwright_install_hint": "Playwright Chromium may not be installed. Run: python -m playwright install --with-deps chromium",
        }
        self._write_json_safely(debug_dir / "headless_error_latest.json", error_payload, log_callback)
        try:
            (logs_dir / "headless_error_latest.log").write_text(
                f"{error_payload['timestamp']}\n{error_message}\n\n{traceback_text}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self.log(log_callback, f"Could not write headless error log: {exc}")

    def _write_headless_zero_results_debug(
        self,
        page,
        debug_dir: Path,
        screenshot_path: Path | None,
        city_or_region: str,
        error_message: str | None,
        log_callback: LogCallback | None,
        traceback_text: str | None = None,
    ) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            text = self._safe_body_text(page)
        except Exception:
            text = ""
        lower_text = text.lower()
        selector_counts: dict[str, int | str] = {}
        for selector in self.CARD_SELECTORS:
            try:
                selector_counts[selector] = self._valid_card_count(page, selector) if selector != "a[data-testid='title-link']" else page.locator(selector).count()
            except Exception as exc:
                selector_counts[selector] = f"error: {exc}"
        try:
            current_url = page.url
        except Exception:
            current_url = None
        try:
            page_title = safe_page_title(page)
        except Exception:
            page_title = None
        payload = {
            "current_url": current_url,
            "page_title": page_title,
            "hotel_card_selector_counts": selector_counts,
            "page_contains_booking": "booking" in lower_text,
            "page_contains_city": str(city_or_region).lower() in lower_text,
            "page_contains_price_symbols": any(symbol in text for symbol in ["$", "US$", "€", "£"]),
            "page_contains_access_restriction": any(pattern in lower_text for pattern in self.ACCESS_RESTRICTION_PATTERNS),
            "page_contains_captcha": "captcha" in lower_text,
            "screenshot_path": str(screenshot_path) if screenshot_path else None,
            "error_message": error_message,
            "traceback": traceback_text,
        }
        text_path = debug_dir / "headless_zero_results_page_text.txt"
        json_path = debug_dir / "headless_zero_results_debug.json"
        try:
            text_path.write_text(text[:5000], encoding="utf-8")
            self.log(log_callback, f"Headless zero-results page text saved: {text_path.resolve()}")
        except OSError as exc:
            self.log(log_callback, f"Could not write headless zero-results page text: {exc}")
        self._write_json_safely(json_path, payload, log_callback)
        self.log(log_callback, f"Headless zero-results debug JSON saved: {json_path.resolve()}")

    def _submit_homepage_search(
        self,
        page,
        city_or_region,
        checkin_date,
        checkout_date,
        adults,
        currency,
        fallback_url: str,
        screenshot_dir: Path,
        options: CollectorOptions,
        screenshot_paths: list[Path],
        log_callback: LogCallback | None,
    ) -> bool:
        try:
            destination = self._first_visible(
                page,
                [
                    "input[name='ss']",
                    "input[placeholder*='Where' i]",
                    "input[aria-label*='destination' i]",
                    "[data-testid='destination-container'] input",
                    "input[type='search']",
                ],
                timeout_ms=7000,
            )
            if destination is None:
                self.log(log_callback, "Booking.com destination field was not found. The page layout may have changed or a popup may be blocking the page.")
                return False
            self.log(log_callback, "destination field found")
            try:
                destination.click(timeout=3000)
            except PlaywrightError:
                self.log(log_callback, "Normal destination click was intercepted; retrying with Playwright force click.")
                destination.click(timeout=3000, force=True)
            destination.fill("", timeout=3000)
            destination.type(str(city_or_region), delay=35, timeout=7000)
            self.log(log_callback, "destination entered")
            self._notify_status(options, "city entered", progress_percent=14)
            page.wait_for_timeout(1000 if options.fast_mode else 1600)
            self._screenshot(page, screenshot_dir, "booking_after_city_entered", options, screenshot_paths, log_callback)

            if self._click_first_visible(
                page,
                [
                    "[data-testid='autocomplete-result']",
                    "button[data-testid='autocomplete-result']",
                    "li[role='option']",
                    "[role='option']",
                    "button:has-text('" + self._escape_selector_text(str(city_or_region)) + "')",
                    "[role='button']:has-text('" + self._escape_selector_text(str(city_or_region)) + "')",
                    "div:has-text('" + self._escape_selector_text(str(city_or_region)) + "')",
                ],
                log_callback,
                success_message="destination suggestion selected",
                timeout_ms=5000,
            ):
                page.wait_for_timeout(700)
            else:
                self.log(log_callback, "Destination suggestion was not visible; trying keyboard selection.")
                try:
                    page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(200)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(700)
                    self.log(log_callback, "destination suggestion selected")
                except PlaywrightError:
                    self.log(log_callback, "Destination suggestion keyboard selection failed; continuing with typed destination.")

            if not self._open_date_picker(page, log_callback):
                self.log(log_callback, "Booking.com date picker did not accept the selected dates.")
                self._apply_hidden_search_inputs(page, city_or_region, checkin_date, checkout_date, adults, currency, log_callback)
            else:
                self.log(log_callback, "date picker opened")
                if not self._select_booking_date(page, checkin_date, "check in date selected", log_callback):
                    self.log(log_callback, "Booking.com date picker did not accept the selected dates.")
                    self._apply_hidden_search_inputs(page, city_or_region, checkin_date, checkout_date, adults, currency, log_callback)
                elif not self._select_booking_date(page, checkout_date, "check out date selected", log_callback):
                    self.log(log_callback, "Booking.com date picker did not accept the selected dates.")
                    self._apply_hidden_search_inputs(page, city_or_region, checkin_date, checkout_date, adults, currency, log_callback)
            self._screenshot(page, screenshot_dir, "booking_after_dates_selected", options, screenshot_paths, log_callback)
            self._notify_status(options, "dates selected", progress_percent=16)
            self._apply_hidden_search_inputs(page, city_or_region, checkin_date, checkout_date, adults, currency, log_callback)

            self._set_adults(page, int(adults), log_callback)
            self.log(log_callback, "adults selected")
            self._notify_status(options, "adults selected", progress_percent=18)
            self._screenshot(page, screenshot_dir, "booking_after_adults_selected", options, screenshot_paths, log_callback)

            search_button = self._first_visible(
                page,
                [
                    "[data-testid='searchbox-submit-button']",
                    "button:has-text('Search')",
                    "button[aria-label*='Search' i]",
                    "button[type='submit']",
                ],
                timeout_ms=7000,
            )
            if search_button is None:
                self.log(log_callback, "Search button was not found; falling back to direct URL.")
                return False
            old_url = page.url
            search_button.click(timeout=5000)
            self.log(log_callback, "search button clicked")
            self._notify_status(options, "search clicked", progress_percent=20)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500 if options.fast_mode else 2500)
            if page.url == old_url:
                self.log(log_callback, "Search click did not change the URL yet; result detection will decide whether fallback is needed.")
                return False
            return True
        except Exception as exc:
            self.log(log_callback, f"Homepage search flow failed; falling back to direct URL. Reason: {exc}")
            try:
                page.goto(fallback_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            return False

    def _apply_hidden_search_inputs(self, page, city_or_region, checkin_date, checkout_date, adults, currency, log_callback: LogCallback | None) -> None:
        page.evaluate(
            """
            ({ city, checkin, checkout, adults, currency }) => {
                const form = document.querySelector('form') || document.body;
                const setInput = (name, value) => {
                    let input = document.querySelector(`input[name="${name}"]`);
                    if (!input) {
                        input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = name;
                        form.appendChild(input);
                    }
                    input.value = String(value);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                };
                setInput('ss', city);
                setInput('checkin', checkin);
                setInput('checkout', checkout);
                setInput('group_adults', adults);
                setInput('no_rooms', 1);
                setInput('group_children', 0);
                setInput('selected_currency', currency);
            }
            """,
            {
                "city": str(city_or_region),
                "checkin": checkin_date.isoformat(),
                "checkout": checkout_date.isoformat(),
                "adults": int(adults),
                "currency": str(currency),
            },
        )
        try:
            page.keyboard.press("Escape")
        except PlaywrightError:
            pass
        self.log(log_callback, "Booking.com hidden search inputs applied for city, dates, adults, room count, and currency.")

    def _first_visible(self, page, selectors: list[str], timeout_ms: int = 3000):
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if locator.count() and locator.is_visible(timeout=500):
                        return locator
                except PlaywrightError:
                    continue
            page.wait_for_timeout(250)
        return None

    def _click_first_visible(self, page, selectors: list[str], log_callback: LogCallback | None, success_message: str, timeout_ms: int = 3000) -> bool:
        locator = self._first_visible(page, selectors, timeout_ms=timeout_ms)
        if locator is None:
            return False
        try:
            locator.click(timeout=4000)
            self.log(log_callback, success_message)
            return True
        except PlaywrightError:
            try:
                self.log(log_callback, f"Normal click failed for {success_message}; retrying with Playwright force click.")
                locator.click(timeout=4000, force=True)
                self.log(log_callback, success_message)
                return True
            except PlaywrightError:
                return False

    def _open_date_picker(self, page, log_callback: LogCallback | None) -> bool:
        return self._click_first_visible(
            page,
            [
                "[data-testid='date-display-field-start']",
                "button[data-testid='date-display-field-start']",
                "button:has-text('Check-in')",
                "button:has-text('Check in')",
                "span:has-text('Check-in')",
                "[aria-label*='Check-in' i]",
            ],
            log_callback,
            success_message="date picker opened",
            timeout_ms=5000,
        )

    def _select_booking_date(self, page, date_value, success_message: str, log_callback: LogCallback | None) -> bool:
        iso_date = date_value.isoformat()
        month_label = f"{date_value.strftime('%B')} {date_value.day}, {date_value.year}" if hasattr(date_value, "strftime") else iso_date
        alt_month_label = f"{date_value.strftime('%A')}, {date_value.strftime('%B')} {date_value.day}, {date_value.year}" if hasattr(date_value, "strftime") else iso_date
        try:
            if page.locator(f"[data-date='{iso_date}']").count() == 0:
                self._click_first_visible(
                    page,
                    [
                        "[data-testid='searchbox-dates-container']",
                        "[data-testid='date-display-field-start']",
                        "button[data-testid='date-display-field-start']",
                        "button:has-text('Check-in')",
                        "span:has-text('Check-in')",
                    ],
                    log_callback,
                    success_message="date picker reopened",
                    timeout_ms=2000,
                )
                page.wait_for_timeout(700)
        except PlaywrightError:
            pass
        selectors = [
            f"span[data-date='{iso_date}']",
            f"td[data-date='{iso_date}']",
            f"button[data-date='{iso_date}']",
            f"[data-date='{iso_date}']",
            f"button[aria-label*='{iso_date}']",
            f"span[aria-label*='{iso_date}']",
            f"button[aria-label*='{month_label}']",
            f"span[aria-label*='{month_label}']",
            f"button[aria-label*='{alt_month_label}']",
            f"span[aria-label*='{alt_month_label}']",
        ]
        for _ in range(14):
            if self._click_first_visible(page, selectors, log_callback, success_message=success_message, timeout_ms=1200):
                page.wait_for_timeout(500)
                return True
            next_button = self._first_visible(
                page,
                [
                    "button[aria-label*='Next' i]",
                    "button:has-text('Next')",
                    "[data-testid='calendar-next-button']",
                ],
                timeout_ms=800,
            )
            if next_button is None:
                break
            try:
                next_button.click(timeout=2000)
                page.wait_for_timeout(500)
            except PlaywrightError:
                break
        try:
            clicked = page.evaluate(
                """
                (isoDate) => {
                    const element = document.querySelector(`[data-date="${isoDate}"]`);
                    if (!element) return false;
                    element.scrollIntoView({ block: "center", inline: "center" });
                    element.click();
                    return true;
                }
                """,
                iso_date,
            )
            if clicked:
                self.log(log_callback, success_message)
                page.wait_for_timeout(500)
                return True
        except Exception as exc:
            self.log(log_callback, f"Date DOM fallback failed for {iso_date}: {exc}")
        return False

    def _set_adults(self, page, adults: int, log_callback: LogCallback | None) -> None:
        opened = self._click_first_visible(
            page,
            [
                "[data-testid='occupancy-config']",
                "button[data-testid='occupancy-config']",
                "button:has-text('adult')",
                "button:has-text('Adults')",
                "[aria-label*='occupancy' i]",
            ],
            log_callback,
            success_message="occupancy selector opened",
            timeout_ms=4000,
        )
        if not opened:
            self.log(log_callback, "Adults selector was not visible; direct search URL fallback will still carry adult count if needed.")
            return
        page.wait_for_timeout(600)
        try:
            adult_text = page.locator("body").inner_text(timeout=1000)
            match = re.search(r"(\d+)\s+adult", adult_text, re.I)
            current = int(match.group(1)) if match else 2
        except PlaywrightError:
            current = 2

        if current == adults:
            return
        button_patterns = ["Increase number of Adults", "Increase number of adults"] if adults > current else ["Decrease number of Adults", "Decrease number of adults"]
        clicks = abs(adults - current)
        for _ in range(clicks):
            if not self._click_first_visible(
                page,
                [
                    f"button[aria-label='{button_patterns[0]}']",
                    f"button[aria-label='{button_patterns[-1]}']",
                    "button:has-text('+')" if adults > current else "button:has-text('-')",
                ],
                log_callback,
                success_message="adult count adjusted",
                timeout_ms=1500,
            ):
                self.log(log_callback, "Could not adjust adult count using visible controls.")
                break
            page.wait_for_timeout(250)

    def handle_booking_popups(self, page, log_callback: LogCallback | None = None, options: CollectorOptions | None = None) -> None:
        close_selectors = [
            "button[aria-label*='Dismiss' i]",
            "button[aria-label*='Close' i]",
            "button:has-text('×')",
            "button:has-text('X')",
            "[role='dialog'] button[aria-label]",
        ]
        dialog = self._first_visible(page, ["[role='dialog']", "div:has-text('Sign in')"], timeout_ms=2500)
        if dialog is None:
            self.log(log_callback, "No Genius popup detected.")
            return
        self.log(log_callback, "Genius popup detected.")
        if self._click_first_visible(page, close_selectors, log_callback, "Genius popup detected and closed.", timeout_ms=3000):
            self.log(log_callback, "Genius popup closed.")
            if options:
                options.stats["genius_popup_closed"] = True
            page.wait_for_timeout(700)
            return
        self.log(log_callback, "Genius popup detected but close button was not found.")

    def _escape_selector_text(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def _fallback_destination_params(self, city_or_region) -> dict[str, str]:
        key = str(city_or_region).strip().lower()
        return dict(self.FALLBACK_DESTINATIONS.get(key, {}))

    def _date_part_params(self, prefix: str, value) -> dict[str, int]:
        return {
            f"{prefix}_year": value.year,
            f"{prefix}_month": value.month,
            f"{prefix}_monthday": value.day,
        }

    def _search_url_candidates(self, original_url: str, city_or_region, checkin_date, checkout_date, adults, currency) -> list[str]:
        fallback = self._fallback_destination_params(city_or_region)
        params = {
            "ss": city_or_region,
            "checkin": checkin_date.isoformat(),
            "checkout": checkout_date.isoformat(),
            "group_adults": adults,
            "group_children": 0,
            "no_rooms": 1,
            "selected_currency": currency,
            "order": "price",
            "src": "searchresults",
            "sb": 1,
            "lang": "en-gb",
        }
        params.update(fallback)
        params.update(self._date_part_params("checkin", checkin_date))
        params.update(self._date_part_params("checkout", checkout_date))
        alternate_url = f"https://www.booking.com/searchresults.html?{urlencode(params)}"
        # Prefer the explicit searchresults URL. Headless Booking sessions have
        # repeatedly canonicalized the form result to a city landing page that
        # contains neither the requested dates nor result cards.
        candidates = [alternate_url, original_url]
        unique: list[str] = []
        for url in candidates:
            if url not in unique:
                unique.append(url)
        return unique

    def _wait_for_results_page(self, page, log_callback: LogCallback | None) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            self.log(log_callback, "Search results page load-state wait timed out; continuing with visible page checks.")
        for selector in self.CARD_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=5000)
                self.log(log_callback, "search results page loaded")
                return
            except PlaywrightTimeoutError:
                continue
        self.log(log_callback, "Search results page loaded, but no hotel-card selector is visible yet.")

    def _ensure_currency(self, page, currency: str, options: CollectorOptions, log_callback: LogCallback | None) -> None:
        parsed = urlparse(page.url)
        query = dict(parse_qsl(parsed.query))
        if query.get("selected_currency", "").upper() == str(currency).upper():
            return
        query["selected_currency"] = str(currency).upper()
        currency_url = urlunparse(parsed._replace(query=urlencode(query)))
        try:
            page.goto(currency_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1000 if options.fast_mode else 2000)
            self.log(log_callback, f"Requested currency applied to results URL: {currency}")
        except PlaywrightError as exc:
            self.log(log_callback, f"Could not apply requested currency {currency}: {exc}")

    def _verify_search_results_or_raise(self, page, city_or_region, checkin_date, checkout_date, adults, log_callback: LogCallback | None) -> None:
        final_url = safe_page_url(page) or ""
        title = safe_page_title(page) or ""
        text = self._safe_body_text(page)
        lowered = text.lower()
        parsed = dict(parse_qsl(urlparse(final_url).query))
        fallback = self._fallback_destination_params(city_or_region)
        city_ok = (
            city_or_region.lower() in lowered
            or city_or_region.lower() in final_url.lower()
            or parsed.get("ss", "").lower() == city_or_region.lower()
            or (fallback and parsed.get("dest_id") == fallback.get("dest_id"))
        )
        checkin_ok = checkin_date.isoformat() in final_url or checkin_date.isoformat() in text or parsed.get("checkin") == checkin_date.isoformat()
        checkout_ok = checkout_date.isoformat() in final_url or checkout_date.isoformat() in text or parsed.get("checkout") == checkout_date.isoformat()
        adults_ok = str(adults) in parsed.get("group_adults", str(adults)) or re.search(rf"\b{int(adults)}\s+adult", lowered)
        hotel_words = bool(re.search(r"\b(hotel|hotels|property|properties|stays?)\b", lowered))
        price_symbols = bool(re.search(r"[$€£]|US\\$|USD|EUR|GBP|MXN", text, re.I))

        self.log(log_callback, f"final URL: {final_url}")
        self.log(log_callback, f"page title: {title}")
        self.log(log_callback, f"visible destination text found: {'yes' if city_ok else 'no'}")
        self.log(log_callback, f"visible check in date found: {'yes' if checkin_ok else 'no'}")
        self.log(log_callback, f"visible check out date found: {'yes' if checkout_ok else 'no'}")
        self.log(log_callback, f"number of adults found: {'yes' if adults_ok else 'no'}")
        self.log(log_callback, f"page contains requested city name: {'yes' if city_ok else 'no'}")
        self.log(log_callback, f"page contains hotels or properties: {'yes' if hotel_words else 'no'}")
        self.log(log_callback, f"page contains price symbols: {'yes' if price_symbols else 'no'}")
        if not (city_ok and checkin_ok and checkout_ok):
            raise RuntimeError("Booking.com search did not apply the requested city or dates.")

    def _apply_price_sort(self, page, fallback_url: str, screenshot_dir: Path, options: CollectorOptions, screenshot_paths: list[Path], log_callback: LogCallback | None) -> None:
        applied = False
        current_query = dict(parse_qsl(urlparse(page.url).query))
        if current_query.get("order") == "price":
            applied = True
            self.log(log_callback, "sort applied using existing price URL parameter")
        try:
            sort_button = None if applied else self._first_visible(
                page,
                [
                    "[data-testid='sorters-dropdown-trigger']",
                    "button:has-text('Sort')",
                    "button[aria-label*='Sort' i]",
                    "button:has-text('Our top picks')",
                ],
                timeout_ms=5000,
            )
            if sort_button is not None:
                sort_button.click(timeout=4000)
                self.log(log_callback, "sort menu opened")
                page.wait_for_timeout(800)
                option_texts = [
                    "Price lowest first",
                    "Lowest price first",
                    "Price low to high",
                    "Price plus taxes and fees",
                    "Sort by price",
                    "Our lowest price first",
                ]
                for text in option_texts:
                    option = page.get_by_text(text, exact=False).first
                    try:
                        if option.count() and option.is_visible(timeout=1000):
                            option.click(timeout=4000)
                            self.log(log_callback, "price low to high option selected")
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                            page.wait_for_timeout(1500 if options.fast_mode else 2500)
                            applied = True
                            break
                    except PlaywrightError:
                        continue
        except PlaywrightError as exc:
            self.log(log_callback, f"Sort UI interaction failed: {exc}")

        if not applied:
            price_url = self._url_with_price_sort(page.url or fallback_url)
            if price_url != page.url:
                try:
                    page.goto(price_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500 if options.fast_mode else 2500)
                    applied = True
                    self.log(log_callback, "sort applied using price URL parameter")
                except PlaywrightError as exc:
                    self.log(log_callback, f"Direct price-sort URL failed: {exc}")

        if applied:
            self.log(log_callback, "sort applied")
            self.log(log_callback, f"final sorted URL: {page.url}")
            self.log(log_callback, f"first visible prices after sorting: {self._first_visible_prices(page)}")
        else:
            self.log(log_callback, "Warning: price low to high sorting could not be applied.")
        options.stats["sort_applied"] = applied
        self._screenshot(page, screenshot_dir, "booking_sort_applied", options, screenshot_paths, log_callback)

    def _url_with_price_sort(self, current_url: str) -> str:
        parsed = urlparse(current_url)
        query = dict(parse_qsl(parsed.query))
        query["order"] = "price"
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _first_visible_prices(self, page) -> str:
        prices: list[str] = []
        selectors = [
            "[data-testid='price-and-discounted-price']",
            "span:has-text('$')",
            "span:has-text('€')",
            "span:has-text('£')",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(locator.count(), 5)):
                    text = locator.nth(index).inner_text(timeout=1000).strip()
                    if text and text not in prices:
                        prices.append(text)
                if prices:
                    break
            except PlaywrightError:
                continue
        return " | ".join(prices[:5]) if prices else "No visible prices found"

    def _safe_body_text(self, page) -> str:
        try:
            return page.locator("body").inner_text(timeout=5000)
        except PlaywrightError:
            return ""

    def _ensure_results_page(self, page, results_url: str | None, options: CollectorOptions, log_callback: LogCallback | None) -> None:
        if not results_url:
            return
        current_url = page.url
        if "/hotel/" not in current_url and "searchresults" in current_url:
            return
        if "/hotel/" in current_url or "searchresults" not in current_url:
            options.stats["unexpected_property_navigation_count"] = int(options.stats.get("unexpected_property_navigation_count", 0)) + 1
            self.log(log_callback, "Unexpected navigation to property page detected.")
            try:
                page.goto(results_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000 if options.fast_mode else 1800)
                self.log(log_callback, "Returned to search results page.")
            except PlaywrightError as exc:
                self.log(log_callback, f"Could not return to search results page: {exc}")

    def handle_booking_cookie_banner(self, page, log_callback: LogCallback | None = None, options: CollectorOptions | None = None) -> bool:
        decline_selectors = [
            "button:has-text('Decline')",
            "button:has-text('Decline all')",
            "button:has-text('Reject all')",
            "button:has-text('Reject optional cookies')",
            "button:has-text('Reject')",
            "button:has-text('Refuser')",
            "button:has-text('Tout refuser')",
        ]
        accept_selectors = [
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
            "button:has-text('Accepter')",
        ]
        settings_selectors = ["button:has-text('Manage cookie settings')", "button:has-text('Manage settings')", "button:has-text('Param')"]
        detected = False
        for selector in decline_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=700):
                    detected = True
                    if options:
                        options.stats["cookie_banner_detected"] = True
                    self.log(log_callback, "Cookie banner detected.")
                    locator.click(timeout=3000)
                    page.wait_for_timeout(800)
                    if options:
                        options.stats["cookie_banner_declined"] = True
                    self.log(log_callback, "Clicked Decline cookies.")
                    still_visible = self._cookie_banner_visible(page)
                    if still_visible:
                        self.log(log_callback, "Cookie banner still visible after click.")
                    else:
                        self.log(log_callback, "Cookie banner handled successfully.")
                    return not still_visible
            except PlaywrightError:
                continue

        for selector in accept_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=500):
                    detected = True
                    if options:
                        options.stats["cookie_banner_detected"] = True
                    self.log(log_callback, "Cookie banner detected.")
                    self.log(log_callback, "Only Accept cookies button was visible; no decline option was clicked.")
                    return False
            except PlaywrightError:
                continue

        for selector in settings_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=500):
                    detected = True
                    if options:
                        options.stats["cookie_banner_detected"] = True
                    self.log(log_callback, "Cookie banner detected.")
                    self.log(log_callback, "Manage cookie settings was visible, but no decline option was directly available.")
                    return False
            except PlaywrightError:
                continue

        if not detected:
            self.log(log_callback, "Cookie banner not detected.")
        return False

    def _cookie_banner_visible(self, page) -> bool:
        for selector in [
            "button:has-text('Decline')",
            "button:has-text('Reject')",
            "button:has-text('Refuser')",
            "button:has-text('Tout refuser')",
            "button:has-text('Accept')",
            "button:has-text('Accepter')",
            "button:has-text('Manage cookie settings')",
        ]:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=300):
                    return True
            except PlaywrightError:
                continue
        return False

    def _genius_popup_visible(self, page) -> bool:
        try:
            dialog = self._first_visible(page, ["[role='dialog']", "div:has-text('Sign in')"], timeout_ms=700)
            return dialog is not None
        except PlaywrightError:
            return False

    def _choose_card_selector(self, page, log_callback: LogCallback | None) -> str | None:
        best_selector = None
        best_valid_count = 0
        for selector in self.CARD_SELECTORS:
            raw_count = page.locator(selector).count()
            valid_count = 0 if selector == "a[data-testid='title-link']" else self._valid_card_count(page, selector)
            self.log(log_callback, f"Hotel card selector tested: {selector} -> raw {raw_count}, valid {valid_count}")
            if valid_count > best_valid_count:
                best_selector = selector
                best_valid_count = valid_count

        if best_selector and best_valid_count > 0:
            self.log(log_callback, f"Search results detected. Selected card selector: {best_selector} ({best_valid_count} valid cards)")
            return best_selector
        self.log(log_callback, "Search results not detected with current Booking.com selectors.")
        return None

    def _wait_for_any_card_selector(self, page, log_callback: LogCallback | None) -> None:
        self.log(log_callback, "Waiting for initial Booking.com hotel cards.")
        for selector in self.CARD_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=6000)
                self.log(log_callback, f"Initial hotel-card signal detected with selector: {selector}")
                return
            except PlaywrightTimeoutError:
                self.log(log_callback, f"Initial wait: selector not found yet: {selector}")
        self.log(log_callback, "Initial hotel-card wait ended with no selector match.")

    def _valid_card_count(self, page, selector: str) -> int:
        return len(self._valid_cards(page, selector))

    def _best_hotel_card_count(self, page, selector: str | None = None, log_callback: LogCallback | None = None) -> int:
        if selector:
            try:
                return self._valid_card_count(page, selector)
            except PlaywrightError as exc:
                self.log(log_callback, f"Selected hotel card selector failed: {exc}")
                return 0
        counts: dict[str, int] = {}
        selectors = [selector] if selector else []
        selectors.extend(item for item in self.CARD_SELECTORS if item not in selectors)
        for item in selectors:
            if not item:
                continue
            try:
                if item == "a[data-testid='title-link']":
                    count = page.locator("a[data-testid='title-link']").count()
                else:
                    count = self._valid_card_count(page, item)
                counts[item] = count
            except PlaywrightError as exc:
                counts[item] = 0
                self.log(log_callback, f"Hotel card count selector failed: {item}: {exc}")
        self.log(log_callback, f"Hotel card selector counts: {counts}")
        return max(counts.values()) if counts else 0

    def _valid_cards(self, page, selector: str):
        if selector == "a[data-testid='title-link']":
            return []
        else:
            locator = page.locator(selector)
            locators = [locator.nth(index) for index in range(locator.count())]

        valid = []
        for card in locators:
            try:
                text = card.inner_text(timeout=1500).strip()
                name = self._text(card, "[data-testid='title'], a[data-testid='title-link'], h3")
                link = self._attr(card, "a[data-testid='title-link'], a[href*='/hotel/']", "href")
                has_price_or_review = bool(re.search(r"[$€£]|review|score|scored|rating|US\\$", text, re.I))
                if (name or link) and has_price_or_review:
                    valid.append(card)
            except PlaywrightError:
                continue
        return valid

    def _progressive_scroll(
        self,
        page,
        selector: str,
        max_hotels: int,
        options: CollectorOptions,
        progress_callback: ProgressCallback | None,
        log_callback: LogCallback | None,
        screenshot_dir: Path,
        screenshot_paths: list[Path],
        results_url: str | None,
    ) -> int:
        return self.load_all_booking_results(
            page,
            max_hotels,
            options.collect_all_available,
            options.max_scroll_minutes,
            log_callback,
            selector=selector,
            options=options,
            progress_callback=progress_callback,
            screenshot_dir=screenshot_dir,
            screenshot_paths=screenshot_paths,
            results_url=results_url,
        )

    def find_load_more_button(self, page, log_callback: LogCallback | None = None):
        pattern = re.compile(
            r"Load more results|Show more results|Load more|See more results|Afficher plus(?: de r(?:\u00e9|e)sultats)?|Charger plus(?: de r(?:\u00e9|e)sultats)?|Voir plus(?: de r(?:\u00e9|e)sultats)?",
            re.I,
        )
        strategies = [
            ("role button text regex", lambda: page.get_by_role("button", name=pattern)),
            ("button filter text regex", lambda: page.locator("button").filter(has_text=pattern)),
            ("page text regex", lambda: page.locator("text=/Load more|Afficher plus|Charger plus|Voir plus/i")),
            ("button text: Load more results", lambda: page.locator("button:has-text('Load more results')")),
            ("button text: Load more", lambda: page.locator("button:has-text('Load more')")),
            ("button text: Show more", lambda: page.locator("button:has-text('Show more')")),
            ("button text: See more results", lambda: page.locator("button:has-text('See more results')")),
            ("button text: Afficher plus", lambda: page.locator("button:has-text('Afficher plus')")),
            ("button text: Charger plus", lambda: page.locator("button:has-text('Charger plus')")),
            ("button text: Voir plus", lambda: page.locator("button:has-text('Voir plus')")),
        ]
        for label, locator_factory in strategies:
            try:
                locator = locator_factory()
                count = locator.count()
                for index in range(min(count, 4)):
                    button = locator.nth(index)
                    if button.is_visible(timeout=800):
                        text = (button.inner_text(timeout=800) or "").strip()
                        self.log(log_callback, f"Load more button found by {label}: {text or '<no visible text>'}")
                        return {"button": button, "strategy": label, "text": text}
            except PlaywrightError as exc:
                self.log(log_callback, f"Load more locator failed ({label}): {exc}")
                continue
        self.log(log_callback, "Load more results button not found.")
        return None

    def click_load_more_results(
        self,
        page,
        previous_card_count: int,
        log_callback: LogCallback | None = None,
        *,
        selector: str | None = None,
        options: CollectorOptions | None = None,
        screenshot_dir: Path | None = None,
        screenshot_paths: list[Path] | None = None,
        results_url: str | None = None,
    ) -> bool:
        options = options or CollectorOptions()
        screenshot_dir = screenshot_dir or Path("data/screenshots")
        screenshot_paths = screenshot_paths if screenshot_paths is not None else []

        self.log(log_callback, "Cookie banner checked before Load more.")
        cookie_visible = self._cookie_banner_visible(page)
        options.stats["load_more_cookie_banner_visible_before_click"] = bool(cookie_visible)
        cookie_handled = self.handle_booking_cookie_banner(page, log_callback, options)
        if cookie_handled:
            self.log(log_callback, "Cookie banner declined before Load more.")
        elif not cookie_visible:
            self.log(log_callback, "No cookie banner detected before Load more.")

        self.log(log_callback, "Genius popup checked before Load more.")
        popup_visible = self._genius_popup_visible(page)
        self.handle_booking_popups(page, log_callback, options)
        if popup_visible:
            self.log(log_callback, "Genius popup closed before Load more.")

        found = self.find_load_more_button(page, log_callback)
        if found is None:
            self.log(log_callback, "Load more was not immediately visible; scanning at the bottom of the page.")
            found = self._scroll_to_bottom_until_load_more(
                page,
                selector,
                options,
                log_callback,
                wait_after_scroll_ms=900,
            )
        if found is None:
            return False

        options.stats["load_more_button_found"] = True
        options.stats["load_more_button_seen_count"] = int(options.stats.get("load_more_button_seen_count", 0)) + 1
        button = found["button"]
        options.stats["load_more_last_button_text"] = found.get("text")
        options.stats["load_more_last_button_strategy"] = found.get("strategy")
        options.stats["hotel_cards_before_load_more"] = previous_card_count
        options.stats["hotel_cards_before_last_click"] = previous_card_count
        self.log(log_callback, "Load more button found.")
        self.log(log_callback, f"Hotel cards before Load more: {previous_card_count}.")

        self._force_screenshot(page, screenshot_dir, "before_load_more_click", screenshot_paths, options, log_callback)
        try:
            self.log(log_callback, "Scrolling Load more results button into view.")
            button.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(100)
            visible = button.is_visible(timeout=1000)
            enabled = button.is_enabled(timeout=1000)
            try:
                options.stats["load_more_last_button_box"] = button.bounding_box(timeout=1000)
            except PlaywrightError:
                options.stats["load_more_last_button_box"] = None
            options.stats["load_more_last_button_visible"] = bool(visible)
            options.stats["load_more_last_button_enabled"] = bool(enabled)
            self.log(log_callback, f"Button visible: {'yes' if visible else 'no'}.")
            self.log(log_callback, f"Button enabled: {'yes' if enabled else 'no'}.")
            self.log(log_callback, f"Button bounding box: {options.stats.get('load_more_last_button_box')}.")
            if not visible or not enabled:
                raise PlaywrightError("Load more button was not visible and enabled after scrolling into view.")
            button.click(timeout=6000)
            options.stats["load_more_clicks"] = int(options.stats.get("load_more_clicks", 0)) + 1
            options.stats["load_more_click_count"] = int(options.stats.get("load_more_click_count", 0)) + 1
            self.log(log_callback, "Clicked Load more.")
        except PlaywrightError as exc:
            options.stats["last_load_more_error"] = str(exc)
            self.log(log_callback, f"Normal Load more click failed: {exc}")
            self._force_screenshot(page, screenshot_dir, "load_more_click_failed", screenshot_paths, options, log_callback)
            self.log(log_callback, "Retrying after cookie and popup handling.")
            self.handle_booking_cookie_banner(page, log_callback, options)
            self.handle_booking_popups(page, log_callback, options)
            found = self.find_load_more_button(page, log_callback)
            try:
                if found is None:
                    raise PlaywrightError("Load more button disappeared before retry.")
                button = found["button"]
                options.stats["load_more_last_button_text"] = found.get("text")
                options.stats["load_more_last_button_strategy"] = found.get("strategy")
                button.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(100)
                retry_visible = button.is_visible(timeout=1000)
                retry_enabled = button.is_enabled(timeout=1000)
                try:
                    options.stats["load_more_last_button_box"] = button.bounding_box(timeout=1000)
                except PlaywrightError:
                    options.stats["load_more_last_button_box"] = None
                options.stats["load_more_last_button_visible"] = bool(retry_visible)
                options.stats["load_more_last_button_enabled"] = bool(retry_enabled)
                if retry_visible and retry_enabled:
                    try:
                        button.click(timeout=6000)
                    except PlaywrightError:
                        button.evaluate("el => el.click()")
                        self.log(log_callback, "Fallback JavaScript click used on visible Load more button.")
                    options.stats["load_more_clicks"] = int(options.stats.get("load_more_clicks", 0)) + 1
                    options.stats["load_more_click_count"] = int(options.stats.get("load_more_click_count", 0)) + 1
                    self.log(log_callback, "Clicked Load more.")
                else:
                    raise PlaywrightError("Load more button retry target was not visible and enabled.")
            except PlaywrightError as retry_exc:
                options.stats["load_more_click_fail_count"] = int(options.stats.get("load_more_click_fail_count", 0)) + 1
                options.stats["consecutive_failed_load_more_clicks"] = int(options.stats.get("consecutive_failed_load_more_clicks", 0)) + 1
                options.stats["last_load_more_error"] = str(retry_exc)
                self.log(log_callback, f"Load more click failed after retries: {retry_exc}")
                self._force_screenshot(page, screenshot_dir, "load_more_stuck", screenshot_paths, options, log_callback)
                self._write_load_more_stuck_debug(page, log_callback, options)
                return False

        try:
            page.wait_for_load_state("networkidle", timeout=1500)
        except PlaywrightTimeoutError:
            self.log(log_callback, "Network idle wait after Load more timed out; continuing with card-count wait.")
        self._ensure_results_page(page, results_url, options, log_callback)
        loaded = self.wait_for_more_cards_after_click(page, previous_card_count, log_callback, selector=selector, options=options)
        if loaded:
            options.stats["load_more_success_count"] = int(options.stats.get("load_more_success_count", 0)) + 1
            options.stats["consecutive_failed_load_more_clicks"] = 0
        else:
            options.stats["consecutive_failed_load_more_clicks"] = int(options.stats.get("consecutive_failed_load_more_clicks", 0)) + 1
        self._force_screenshot(page, screenshot_dir, "after_load_more_click", screenshot_paths, options, log_callback)
        return loaded

    def wait_for_more_cards_after_click(
        self,
        page,
        previous_card_count: int,
        log_callback: LogCallback | None = None,
        *,
        selector: str | None = None,
        options: CollectorOptions | None = None,
    ) -> bool:
        self.log(log_callback, "Waiting for new cards.")
        wait_seconds = 15 if not options or options.ultra_reliable_loading_mode else 12
        deadline = time.monotonic() + wait_seconds
        after_count = previous_card_count
        logged_button_wait = False
        while time.monotonic() < deadline:
            if not interruptible_page_wait(page, 300, options.stop_event if options else None):
                self.log(log_callback, "Stop requested while waiting for cards after Load more.")
                return False
            after_count = self._best_hotel_card_count(page, selector, log_callback)
            if after_count > previous_card_count:
                break
            button_state = self.find_load_more_button(page, None)
            if button_state is None and after_count == previous_card_count:
                if not logged_button_wait:
                    self.log(log_callback, "Load more button disappeared; waiting for new hotel cards to render.")
                logged_button_wait = True
                continue
            if button_state is not None and after_count == previous_card_count:
                try:
                    if not button_state["button"].is_enabled(timeout=200):
                        self.log(log_callback, "Load more became disabled without adding cards; results exhausted.")
                        break
                except PlaywrightError:
                    pass

        new_cards = max(after_count - previous_card_count, 0)
        self.log(log_callback, f"Hotel cards after Load more: {after_count}.")
        self.log(log_callback, f"New cards loaded: {new_cards}.")
        if options:
            options.stats["hotel_cards_after_load_more"] = after_count
            options.stats["hotel_cards_after_last_click"] = after_count
            options.stats["new_cards_after_last_click"] = new_cards
        return new_cards > 0

    def _snapshot_loaded_hotels(self, page, selector: str | None) -> list[dict]:
        target_selector = selector or "div[data-testid='property-card']"
        try:
            return page.locator(target_selector).evaluate_all(
                r"""
                (cards) => {
                    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
                    const absoluteUrl = (href) => {
                        if (!href) return null;
                        try { return new URL(href, "https://www.booking.com").href; } catch { return href; }
                    };
                    return cards.map((card, index) => {
                        const title = card.querySelector("[data-testid='title'], a[data-testid='title-link'], h3");
                        const titleLink = card.querySelector("a[data-testid='title-link'], a[href*='/hotel/']");
                        const priceEl = card.querySelector("[data-testid='price-and-discounted-price']") || Array.from(card.querySelectorAll("span, div")).find((el) => /(?:US\$|\$|€|£)\s*\d/.test(clean(el.textContent)));
                        return {
                            index: index + 1,
                            name: clean(title?.innerText),
                            url: absoluteUrl(titleLink?.getAttribute("href")),
                            price: clean(priceEl?.innerText || priceEl?.textContent),
                        };
                    }).filter((row) => row.name || row.url);
                }
                """
            )
        except PlaywrightError:
            return []

    def _update_unique_hotels_from_page(self, page, selector: str | None, unique_hotels: dict[str, dict], options: CollectorOptions, log_callback: LogCallback | None) -> int:
        rows = self._snapshot_loaded_hotels(page, selector)
        added = 0
        for row in rows:
            name = clean_hotel_name(row.get("name"))
            key = self._canonical_hotel_key(row.get("url"), name)
            if not key or key in unique_hotels:
                continue
            row["name"] = name
            unique_hotels[key] = row
            added += 1
        prices = [parse_price_to_decimal(row.get("price")) for row in unique_hotels.values()]
        valid_prices = [price for price in prices if price is not None]
        options.stats["visible_hotel_cards_count"] = len(rows)
        options.stats["unique_hotels_collected"] = len(unique_hotels)
        options.stats["unique_results_loaded"] = len(unique_hotels)
        options.stats["new_hotels_added_last_cycle"] = added
        options.stats["hotels_with_valid_price"] = len(valid_prices)
        options.stats["hotels_missing_price"] = max(len(unique_hotels) - len(valid_prices), 0)
        target_count = options.stats.get("target_result_count") or options.stats.get("booking_visible_result_count")
        if target_count:
            options.stats["estimated_missing_results"] = max(int(target_count) - len(unique_hotels), 0)
        if valid_prices:
            options.stats["lowest_price_collected"] = float(min(valid_prices))
            options.stats["highest_price_collected"] = float(max(valid_prices))
            self.log(log_callback, f"Current price range: ${float(min(valid_prices)):.2f} to ${float(max(valid_prices)):.2f}")
        self.log(log_callback, f"Visible cards found: {len(rows)}")
        self.log(log_callback, f"Unique hotels collected so far: {len(unique_hotels)}")
        self.log(log_callback, f"New unique hotels added this cycle: {added}")
        if added and options.partial_results_callback:
            try:
                current_date_text = str(options.stats.get("current_checkin_date") or "")
                current_date = date.fromisoformat(current_date_text)
                options.partial_results_callback(
                    current_date,
                    list(unique_hotels.values()),
                    {
                        "visible_hotel_cards_count": len(rows),
                        "unique_hotels_collected": len(unique_hotels),
                        "new_hotels_added_last_cycle": added,
                    },
                )
            except Exception as exc:
                self.log(log_callback, f"Warning: partial hotel snapshot failed but collection will continue: {exc}")
        return added

    def _canonical_hotel_key(self, hotel_url: str | None, hotel_name: str | None) -> str:
        if hotel_url:
            try:
                parsed = urlparse(hotel_url)
                path = re.sub(r"/+", "/", parsed.path or "").strip("/")
                if path:
                    return path.lower()
            except ValueError:
                pass
        return clean_hotel_name(hotel_name)

    def _extract_booking_visible_result_count_details(self, page, log_callback: LogCallback | None = None) -> dict[str, object]:
        try:
            text = self._safe_body_text(page)
        except PlaywrightError:
            return {"count": None, "text": None, "is_lower_bound": False}
        compact_text = re.sub(r"\s+", " ", text)
        patterns = [
            r"\b(?P<count>\d[\d\s,.]*)(?P<plus>\+)?\s*(?:hotels|properties|results|\u00e9tablissements|etablissements)\s*(?:found|trouv\u00e9s|trouves)\b",
            r"\b(?P<prefix>plus de|over|more than)\s+(?P<count>\d[\d\s,.]*)(?P<plus>\+)?\s*(?:hotels|properties|results|\u00e9tablissements|etablissements)\b",
            r"\b(?:showing|found|environ|about|approximately)\s+(?P<count>\d[\d\s,.]*)(?P<plus>\+)?\s*(?:hotels|properties|results|\u00e9tablissements|etablissements)\b",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, compact_text, re.I):
                parsed = self._parse_result_count_match(match)
                if parsed:
                    self.log(log_callback, f"Booking.com visible result count detected: {parsed['text']}")
                    return parsed
        return {"count": None, "text": None, "is_lower_bound": False}

    def _parse_result_count_match(self, match) -> dict[str, object] | None:
        value = re.sub(r"[^\d]", "", match.group("count"))
        if not value:
            return None
        count = int(value)
        prefix = (match.groupdict().get("prefix") or "").lower()
        is_lower_bound = bool(match.groupdict().get("plus")) or prefix in {"plus de", "over", "more than"}
        return {"count": count, "text": f"{count}+" if is_lower_bound else str(count), "is_lower_bound": is_lower_bound}

    def _result_count_heading_candidates(self, page) -> list[str]:
        try:
            return page.evaluate(
                r"""
                () => {
                    const selectors = [
                        "h1",
                        "h2",
                        "[data-testid='header-title']",
                        "[data-testid='title']",
                        "[role='heading']"
                    ];
                    const texts = [];
                    for (const selector of selectors) {
                        for (const el of Array.from(document.querySelectorAll(selector))) {
                            const text = String(el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
                            if (text && /hotel|properties|property|results|établissement|etablissement/i.test(text)) {
                                texts.push(text);
                            }
                        }
                    }
                    return [...new Set(texts)].slice(0, 20);
                }
                """
            )
        except PlaywrightError:
            return []

    def _extract_booking_visible_result_count(self, page, log_callback: LogCallback | None = None) -> int | None:
        details = self._extract_booking_visible_result_count_details(page, log_callback)
        return details.get("count") if isinstance(details.get("count"), int) else None

    def load_full_booking_results_until_complete(
        self,
        page,
        target_result_count: int | None,
        collect_all_available_hotels: bool,
        max_scroll_minutes: int,
        log_callback: LogCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        *,
        selector: str | None = None,
        options: CollectorOptions | None = None,
        screenshot_dir: Path | None = None,
        screenshot_paths: list[Path] | None = None,
        results_url: str | None = None,
    ) -> int:
        options = options or CollectorOptions()
        screenshot_dir = screenshot_dir or Path("data/screenshots")
        screenshot_paths = screenshot_paths if screenshot_paths is not None else []
        start = time.monotonic()
        last_growth = start
        max_seconds = max(1, int(max_scroll_minutes)) * 60
        unique_results_by_key: dict[str, dict] = {}

        count_details = self._extract_booking_visible_result_count_details(page, log_callback)
        parsed_count = count_details.get("count") if isinstance(count_details.get("count"), int) else None
        target_count = target_result_count or parsed_count
        options.stats["booking_visible_result_count"] = target_count
        options.stats["target_result_count"] = target_count
        options.stats["visible_result_count_text"] = count_details.get("text") or (str(target_count) if target_count else None)
        options.stats["visible_result_count_is_lower_bound"] = bool(count_details.get("is_lower_bound"))
        options.stats["completion_status"] = "loading"
        options.stats["full_loading_started"] = True
        self.log(log_callback, "Full Booking.com result loader started.")

        current_count = self._best_hotel_card_count(page, selector, log_callback)
        self._update_unique_hotels_from_page(page, selector, unique_results_by_key, options, log_callback)
        last_unique_count = len(unique_results_by_key)

        while True:
            elapsed = time.monotonic() - start
            if self.should_stop(options):
                options.stats["final_stop_reason"] = "stopped_user_requested"
                options.stats["completion_status"] = "incomplete_user_stopped"
                self.log(log_callback, "Stop requested during full Booking.com loading.")
                break
            if elapsed >= max_seconds:
                options.stats["maximum_scroll_time_reached"] = True
                options.stats["final_stop_reason"] = "stopped_max_scroll_time_reached"
                options.stats["completion_status"] = "incomplete_max_scroll_time_reached"
                self.log(log_callback, f"Maximum full collection time reached ({max_scroll_minutes} minutes).")
                break
            if self._detect_access_restriction(page, log_callback):
                options.stats["final_stop_reason"] = "blocked_or_access_restricted"
                options.stats["completion_status"] = "incomplete_access_restricted"
                raise RuntimeError("blocked_or_access_restricted: Booking.com showed CAPTCHA or access restriction wording during full loading.")

            options.stats["cycle_count"] = int(options.stats.get("cycle_count", 0)) + 1
            cycle = int(options.stats["cycle_count"])
            self._ensure_results_page(page, results_url, options, log_callback)
            height_before = self._safe_page_height(page)

            before_update_count = len(unique_results_by_key)
            found = self._scroll_to_bottom_until_load_more(
                page,
                selector,
                options,
                log_callback,
                unique_hotels=unique_results_by_key,
            )
            self._update_unique_hotels_from_page(page, selector, unique_results_by_key, options, log_callback)
            load_more_visible = found is not None
            options.stats["load_more_last_button_visible"] = bool(load_more_visible)
            if load_more_visible:
                options.stats["consecutive_no_load_more_seen"] = 0
                clicked = self.click_load_more_results(
                    page,
                    current_count,
                    log_callback,
                    selector=selector,
                    options=options,
                    screenshot_dir=screenshot_dir,
                    screenshot_paths=screenshot_paths,
                    results_url=results_url,
                )
            else:
                options.stats["consecutive_no_load_more_seen"] = int(options.stats.get("consecutive_no_load_more_seen", 0)) + 1

            options.stats["total_scroll_attempts"] = int(options.stats.get("total_scroll_attempts", 0)) + 1
            current_count = self._best_hotel_card_count(page, selector, log_callback)
            added_after_click = self._update_unique_hotels_from_page(page, selector, unique_results_by_key, options, log_callback)
            unique_count = len(unique_results_by_key)
            cycle_added = unique_count - before_update_count
            if unique_count > last_unique_count or added_after_click > 0:
                options.stats["consecutive_no_new_unique_results"] = 0
                last_unique_count = unique_count
            else:
                options.stats["consecutive_no_new_unique_results"] = int(options.stats.get("consecutive_no_new_unique_results", 0)) + 1

            height_after = self._safe_page_height(page)
            scroll_position = self._safe_scroll_position(page)
            options.stats["final_page_height"] = height_after
            options.stats["final_scroll_position"] = scroll_position
            estimated_missing = max(int(target_count) - unique_count, 0) if target_count else None
            options.stats["estimated_missing_results"] = estimated_missing
            self.log(log_callback, f"Cycle {cycle}:")
            self.log(log_callback, f"Visible cards: {current_count}")
            self.log(log_callback, f"Unique loaded results: {unique_count}")
            self.log(log_callback, f"New unique results this cycle: {max(cycle_added, 0)}")
            self.log(log_callback, f"Booking.com result count: {options.stats.get('visible_result_count_text') or target_count or 'unknown'}")
            self.log(log_callback, f"Estimated missing: {estimated_missing if estimated_missing is not None else 'unknown'}")
            self.log(log_callback, f"Page height before/after: {height_before} -> {height_after}; scroll position: {scroll_position}; Load more visible: {'yes' if load_more_visible else 'no'}")

            if target_count and unique_count >= int(target_count):
                options.stats["completion_status"] = "complete_reached_visible_count"
                options.stats["final_stop_reason"] = "completed_reached_visible_count"
                self.log(log_callback, f"Loaded {unique_count} unique results, reaching Booking.com visible target {target_count}.")
                break

            no_new = int(options.stats.get("consecutive_no_new_unique_results", 0))
            no_button = int(options.stats.get("consecutive_no_load_more_seen", 0))
            threshold = self._full_loading_no_progress_threshold(unique_count, target_count)
            if target_count and unique_count < int(target_count):
                missing = int(target_count) - unique_count
                if missing > max(25, int(target_count) * 0.1):
                    self.log(log_callback, f"WARNING: Booking.com shows {options.stats.get('visible_result_count_text') or target_count} results but only {unique_count} unique results were loaded. Continuing full loading loop.")
            if no_new >= threshold and no_button >= threshold:
                self.log(log_callback, f"No new unique results and no Load more button for {threshold} cycles; running final verification before stopping.")
                verified_end = self._final_end_verification(page, selector, unique_results_by_key, options, log_callback, screenshot_dir, screenshot_paths)
                if verified_end:
                    if target_count and unique_count < int(target_count):
                        options.stats["completion_status"] = "incomplete_no_more_load_more_after_final_verification"
                        options.stats["collection_completeness_warning"] = (
                            f"Warning: Booking.com shows {options.stats.get('visible_result_count_text') or target_count} results but only {unique_count} unique results were loaded. "
                            "Final verification did not find another Load more button."
                        )
                    else:
                        options.stats["completion_status"] = "complete_no_more_load_more_after_final_verification"
                    options.stats["final_stop_reason"] = "completed_no_more_load_more_button"
                    break
                options.stats["consecutive_no_new_unique_results"] = 0
                options.stats["consecutive_no_load_more_seen"] = 0

            progress_value = min(0.52, 0.12 + (elapsed / max_seconds) * 0.35)
            self.progress(progress_callback, progress_value, f"Loading full Booking.com results: {unique_count} unique loaded")

        self._finalize_full_loading_stats(page, unique_results_by_key, options, screenshot_dir, screenshot_paths, log_callback)
        return self._best_hotel_card_count(page, selector, log_callback)

    def _full_loading_no_progress_threshold(self, unique_count: int, target_count: int | None) -> int:
        if target_count:
            if unique_count < int(target_count) * 0.9:
                return 10
            return 5
        return 8

    def _safe_page_height(self, page) -> int:
        try:
            return int(page.evaluate("document.body.scrollHeight"))
        except PlaywrightError:
            return 0

    def _safe_scroll_position(self, page) -> int:
        try:
            return int(page.evaluate("window.scrollY || document.documentElement.scrollTop || 0"))
        except PlaywrightError:
            return 0

    def _scroll_last_loaded_card_into_view(self, page, selector: str | None) -> None:
        target_selector = selector or "div[data-testid='property-card']"
        try:
            cards = page.locator(target_selector)
            count = cards.count()
            if count:
                cards.nth(count - 1).scroll_into_view_if_needed(timeout=3000)
        except PlaywrightError:
            pass

    def _scroll_to_bottom_until_load_more(
        self,
        page,
        selector: str | None,
        options: CollectorOptions | None,
        log_callback: LogCallback | None,
        *,
        unique_hotels: dict[str, dict] | None = None,
        wait_after_scroll_ms: int | None = None,
    ):
        options = options or CollectorOptions()
        delay_ms = wait_after_scroll_ms or (1600 if options.ultra_reliable_loading_mode else 1000)
        max_attempts = 8 if options.ultra_reliable_loading_mode else 5
        stable_bottom_passes = 0
        previous_height = self._safe_page_height(page)
        self.log(log_callback, "Scanning downward for the bottom Load more results button.")

        for attempt in range(1, max_attempts + 1):
            options.stats["load_more_bottom_scan_attempts"] = int(options.stats.get("load_more_bottom_scan_attempts", 0)) + 1
            try:
                self._scroll_last_loaded_card_into_view(page, selector)
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(max(350, delay_ms // 2))
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(delay_ms)
            except PlaywrightError as exc:
                options.stats["last_load_more_error"] = str(exc)
                self.log(log_callback, f"Bottom scan scroll failed: {exc}")
                return None

            if unique_hotels is not None:
                self._update_unique_hotels_from_page(page, selector, unique_hotels, options, log_callback)

            found = self.find_load_more_button(page, None)
            current_height = self._safe_page_height(page)
            current_scroll = self._safe_scroll_position(page)
            self.log(
                log_callback,
                f"Bottom scan {attempt}/{max_attempts}: height={current_height}, scroll={current_scroll}, load_more={'yes' if found else 'no'}",
            )
            if found is not None:
                text = found.get("text") or "<no visible text>"
                self.log(log_callback, f"Bottom Load more button is ready: {text}")
                return found

            if current_height <= previous_height:
                stable_bottom_passes += 1
            else:
                stable_bottom_passes = 0
            previous_height = current_height

            if stable_bottom_passes >= 2:
                self.log(log_callback, "Bottom stayed stable and no Load more button appeared.")
                return None

        self.log(log_callback, "Bottom scan ended without finding another Load more button.")
        return None

    def _bottom_visible_text(self, page) -> str:
        try:
            return page.evaluate(
                r"""
                () => {
                    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
                    const viewportBottom = window.scrollY + window.innerHeight;
                    const parts = [];
                    for (const el of Array.from(document.querySelectorAll("body *"))) {
                        const box = el.getBoundingClientRect();
                        if (!box || box.height === 0 || box.width === 0) continue;
                        const absoluteTop = box.top + window.scrollY;
                        if (absoluteTop >= viewportBottom - window.innerHeight * 1.6) {
                            const text = clean(el.innerText || el.textContent);
                            if (text && text.length < 500) parts.push(text);
                        }
                        if (parts.length >= 80) break;
                    }
                    return parts.join("\n");
                }
                """
            )
        except PlaywrightError:
            return ""

    def _detect_end_of_results_text(self, text: str) -> str | None:
        patterns = [
            r"you have reached the end of the list",
            r"no more results",
            r"that's all",
            r"end of results",
            r"vous avez atteint la fin",
            r"plus de r(?:\u00e9|e)sultats",
            r"aucun autre r(?:\u00e9|e)sultat",
        ]
        for pattern in patterns:
            match = re.search(pattern, text or "", re.I)
            if match:
                return match.group(0)
        return None

    def _finalize_full_loading_stats(
        self,
        page,
        unique_results_by_key: dict[str, dict],
        options: CollectorOptions,
        screenshot_dir: Path,
        screenshot_paths: list[Path],
        log_callback: LogCallback | None,
    ) -> None:
        try:
            options.stats["final_page_height"] = self._safe_page_height(page)
            options.stats["final_scroll_position"] = self._safe_scroll_position(page)
            options.stats["final_url"] = safe_page_url(page, log_callback)
            options.stats["final_page_title"] = safe_page_title(page, log_callback)
        except PlaywrightError:
            pass
        if not options.stats.get("final_bottom_screenshot_path"):
            self._save_final_bottom_screenshot(page, screenshot_dir, screenshot_paths, options, log_callback)
        target_count = options.stats.get("target_result_count") or options.stats.get("booking_visible_result_count")
        unique_count = len(unique_results_by_key)
        options.stats["unique_results_loaded"] = unique_count
        options.stats["unique_hotels_collected"] = unique_count
        if target_count:
            missing = max(int(target_count) - unique_count, 0)
            options.stats["estimated_missing_results"] = missing
            options.stats["booking_result_count_difference"] = missing
        else:
            options.stats["estimated_missing_results"] = None
        if not options.stats.get("completion_status") or options.stats.get("completion_status") == "loading":
            options.stats["completion_status"] = "incomplete_unknown_reason"
        if not options.stats.get("final_stop_reason"):
            options.stats["final_stop_reason"] = "failed_unknown_error"
        if target_count and unique_count < int(target_count) and options.stats.get("completion_status", "").startswith("complete"):
            options.stats["collection_completeness_warning"] = (
                f"Warning: Booking.com shows {options.stats.get('visible_result_count_text') or target_count} results but only {unique_count} unique results were loaded. "
                f"Completion status: {options.stats.get('completion_status')}."
            )
        self._write_full_loading_evidence(page, unique_results_by_key, options, screenshot_dir, screenshot_paths, log_callback)

    def _write_full_loading_evidence(
        self,
        page,
        unique_results_by_key: dict[str, dict],
        options: CollectorOptions,
        screenshot_dir: Path,
        screenshot_paths: list[Path],
        log_callback: LogCallback | None,
    ) -> None:
        debug_dir = options.debug_dir or Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        bottom_text = self._bottom_visible_text(page)
        bottom_path = debug_dir / "booking_bottom_visible_text_latest.txt"
        bottom_path.write_text(bottom_text, encoding="utf-8")
        options.stats["bottom_visible_text_path"] = str(bottom_path.resolve())
        end_text = self._detect_end_of_results_text(bottom_text)
        if end_text:
            options.stats["end_of_results_text"] = end_text
            self.log(log_callback, f"End of results text detected: {end_text}")
        final_screenshot = options.stats.get("final_bottom_screenshot_path")
        if not final_screenshot:
            saved = self._save_final_bottom_screenshot(page, screenshot_dir, screenshot_paths, options, log_callback)
            final_screenshot = str(saved) if saved else None
        summary = {
            "city": options.stats.get("current_city_or_region"),
            "checkin_date": options.stats.get("current_checkin_date"),
            "checkout_date": options.stats.get("current_checkout_date"),
            "final_url": options.stats.get("final_url"),
            "page_title": options.stats.get("final_page_title"),
            "visible_result_count_text": options.stats.get("visible_result_count_text"),
            "booking_visible_result_count": options.stats.get("booking_visible_result_count"),
            "unique_results_loaded": len(unique_results_by_key),
            "estimated_missing_results": options.stats.get("estimated_missing_results"),
            "completion_status": options.stats.get("completion_status"),
            "final_stop_reason": options.stats.get("final_stop_reason"),
            "load_more_seen_count": options.stats.get("load_more_button_seen_count", 0),
            "load_more_click_count": options.stats.get("load_more_clicks", 0),
            "load_more_success_count": options.stats.get("load_more_success_count", 0),
            "load_more_failed_click_count": options.stats.get("load_more_click_fail_count", 0),
            "total_scroll_attempts": options.stats.get("total_scroll_attempts", 0),
            "consecutive_no_new_unique_results": options.stats.get("consecutive_no_new_unique_results", 0),
            "consecutive_no_load_more_seen": options.stats.get("consecutive_no_load_more_seen", 0),
            "max_scroll_time_reached": bool(options.stats.get("maximum_scroll_time_reached")),
            "lowest_price_collected": options.stats.get("lowest_price_collected"),
            "highest_price_collected": options.stats.get("highest_price_collected"),
            "final_screenshot_path": final_screenshot,
            "bottom_visible_text_path": str(bottom_path.resolve()),
            "end_of_results_text": options.stats.get("end_of_results_text"),
        }
        summary_path = debug_dir / "booking_full_loading_summary_latest.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        results_path = debug_dir / "booking_unique_results_loaded_latest.txt"
        lines = [
            f"{index}. {row.get('name') or '<no name>'} | {row.get('price') or '<no price>'} | {row.get('url') or '<no url>'}"
            for index, row in enumerate(unique_results_by_key.values(), start=1)
        ]
        results_path.write_text("\n".join(lines), encoding="utf-8")
        options.stats["full_loading_debug_json_path"] = str(summary_path.resolve())
        options.stats["unique_results_loaded_path"] = str(results_path.resolve())
        self.log(log_callback, f"Full loading summary saved: {summary_path.resolve()}")
        self.log(log_callback, f"Unique loaded results saved: {results_path.resolve()}")
        self.log(log_callback, f"Bottom visible text saved: {bottom_path.resolve()}")

    def load_all_booking_results(
        self,
        page,
        max_hotels: int,
        collect_all_available_hotels: bool,
        max_scroll_minutes: int,
        log_callback: LogCallback | None = None,
        *,
        selector: str | None = None,
        options: CollectorOptions | None = None,
        progress_callback: ProgressCallback | None = None,
        screenshot_dir: Path | None = None,
        screenshot_paths: list[Path] | None = None,
        results_url: str | None = None,
    ) -> int:
        options = options or CollectorOptions()
        screenshot_dir = screenshot_dir or Path("data/screenshots")
        screenshot_paths = screenshot_paths if screenshot_paths is not None else []
        if collect_all_available_hotels:
            return self.load_full_booking_results_until_complete(
                page,
                None,
                collect_all_available_hotels,
                max_scroll_minutes,
                log_callback,
                progress_callback,
                selector=selector,
                options=options,
                screenshot_dir=screenshot_dir,
                screenshot_paths=screenshot_paths,
                results_url=results_url,
            )
        start = time.monotonic()
        max_seconds = max(1, int(max_scroll_minutes)) * 60
        max_unchanged = 2
        delay_ms = 450 if options.performance_mode == "fast" else 800 if options.performance_mode == "balanced" else 1500
        current_count = self._best_hotel_card_count(page, selector, log_callback)
        last_count = current_count
        scroll_index = 0
        unique_hotels: dict[str, dict] = {}
        options.stats["booking_visible_result_count"] = self._extract_booking_visible_result_count(page, log_callback)
        self._update_unique_hotels_from_page(page, selector, unique_hotels, options, log_callback)
        self.log(log_callback, f"Scroll 0: {current_count} cards found")

        while True:
            if self.should_stop(options):
                self.log(log_callback, "Stop requested during Booking.com scrolling.")
                options.stats["final_stop_reason"] = "stopped_user_requested"
                break
            if not collect_all_available_hotels and current_count >= int(max_hotels):
                self.log(log_callback, f"Reached maximum hotels setting ({max_hotels}); stopping scroll.")
                options.stats["final_stop_reason"] = "completed_max_hotels_reached"
                break
            elapsed = time.monotonic() - last_growth
            hard_elapsed = time.monotonic() - start
            if elapsed >= max_seconds or hard_elapsed >= max_seconds * 4:
                options.stats["maximum_scroll_time_reached"] = True
                options.stats["final_stop_reason"] = "completed_scroll_time_limit"
                options.stats["stopped_by_time_limit"] = True
                self.log(log_callback, f"Scroll inactivity/hard limit reached ({max_scroll_minutes} minutes inactivity); stopping scroll.")
                break
            if self._detect_access_restriction(page, log_callback):
                options.stats["final_stop_reason"] = "blocked_or_access_restricted"
                raise RuntimeError("blocked_or_access_restricted: Booking.com showed CAPTCHA or access restriction wording during scrolling.")

            self._ensure_results_page(page, results_url, options, log_callback)
            cycle_added_before_click = self._update_unique_hotels_from_page(page, selector, unique_hotels, options, log_callback)

            previous_count = current_count
            found = self._scroll_to_bottom_until_load_more(
                page,
                selector,
                options,
                log_callback,
                unique_hotels=unique_hotels,
                wait_after_scroll_ms=delay_ms,
            )
            if found is not None:
                clicked_and_loaded = self.click_load_more_results(
                    page,
                    previous_count,
                    log_callback,
                    selector=selector,
                    options=options,
                    screenshot_dir=screenshot_dir,
                    screenshot_paths=screenshot_paths,
                    results_url=results_url,
                )
                if clicked_and_loaded:
                    options.stats["consecutive_no_button_attempts"] = 0
                    options.stats["consecutive_no_new_card_attempts"] = 0
                else:
                    options.stats["consecutive_no_new_card_attempts"] = int(options.stats.get("consecutive_no_new_card_attempts", 0)) + 1
            else:
                options.stats["consecutive_no_button_attempts"] = int(options.stats.get("consecutive_no_button_attempts", 0)) + 1

            scroll_index += 1
            options.stats["total_scroll_attempts"] = scroll_index
            current_count = self._best_hotel_card_count(page, selector, log_callback)
            self.log(log_callback, f"Scroll {scroll_index}: {current_count} cards found")
            cycle_added_after_click = self._update_unique_hotels_from_page(page, selector, unique_hotels, options, log_callback)
            cycle_added = cycle_added_before_click + cycle_added_after_click
            if current_count > last_count or cycle_added > 0:
                last_count = current_count
                last_growth = time.monotonic()
                options.stats["consecutive_no_new_card_attempts"] = 0
            else:
                options.stats["consecutive_no_new_card_attempts"] = int(options.stats.get("consecutive_no_new_card_attempts", 0)) + 1
                self.log(log_callback, f"No new unique hotels after attempt {options.stats['consecutive_no_new_card_attempts']} of {max_unchanged}")

            no_new = int(options.stats.get("consecutive_no_new_card_attempts", 0))
            no_button = int(options.stats.get("consecutive_no_button_attempts", 0))
            if no_button >= max_unchanged and no_new >= max_unchanged:
                self.log(log_callback, f"No Load more button and no new unique hotels after {max_unchanged} consecutive attempts; running final end verification.")
                verified_end = self._final_end_verification(page, selector, unique_hotels, options, log_callback, screenshot_dir, screenshot_paths)
                if verified_end:
                    options.stats["final_stop_reason"] = "completed_no_new_hotels_after_repeated_attempts"
                    break
                options.stats["consecutive_no_new_card_attempts"] = 0
                options.stats["consecutive_no_button_attempts"] = 0
            progress_value = min(0.52, 0.12 + (elapsed / max_seconds) * 0.35)
            self.progress(progress_callback, progress_value, f"Scrolling Booking.com results: {current_count} cards found")

        if not options.stats.get("final_stop_reason"):
            options.stats["final_stop_reason"] = "completed_no_more_load_more_button"
        options.stats["requested_max_hotels"] = int(max_hotels)
        options.stats["available_cards"] = int(last_count)
        options.stats["results_exhausted"] = options.stats.get("final_stop_reason") in {"completed_no_more_load_more_button", "completed_results_exhausted"}
        if str(options.stats.get("final_stop_reason", "")).startswith("completed") and options.stats.get("unique_hotels_collected", 0) < int(max_hotels):
            options.stats["completion_status"] = "completed_results_exhausted"
            self.log(log_callback, f"Results exhausted: requested up to {max_hotels}, Booking.com exposed {last_count} cards, {options.stats.get('unique_hotels_collected', 0)} valid hotels retained.")
        try:
            options.stats["final_page_height"] = int(page.evaluate("document.body.scrollHeight"))
            options.stats["final_url"] = safe_page_url(page, log_callback)
            options.stats["final_page_title"] = safe_page_title(page, log_callback)
        except PlaywrightError:
            pass
        visible_count = options.stats.get("booking_visible_result_count")
        if visible_count:
            difference = int(visible_count) - int(options.stats.get("unique_hotels_collected", 0))
            options.stats["booking_result_count_difference"] = difference
            if difference > max(25, int(visible_count) * 0.2) and not options.stats.get("maximum_scroll_time_reached"):
                options.stats["collection_completeness_warning"] = (
                    f"Warning: The scraper may not have reached the full result list. Booking.com appears to show approximately {visible_count} results, "
                    f"but only {options.stats.get('unique_hotels_collected', 0)} unique hotels were collected."
                )
                self.log(log_callback, str(options.stats["collection_completeness_warning"]))
        if collect_all_available_hotels:
            self._write_complete_collection_proof(page, unique_hotels, options, screenshot_dir, screenshot_paths, log_callback)
        options.stats["final_cards_found"] = last_count
        options.stats["final_card_count"] = last_count
        return last_count

    def _final_end_verification(
        self,
        page,
        selector: str | None,
        unique_hotels: dict[str, dict],
        options: CollectorOptions,
        log_callback: LogCallback | None,
        screenshot_dir: Path,
        screenshot_paths: list[Path],
    ) -> bool:
        self.log(log_callback, "Final end verification started.")
        try:
            verification_passes = 2 if options.ultra_reliable_loading_mode else 1
            any_button_visible = False
            any_new_hotels = False
            any_height_changed = False
            end_text_found = None
            height_before = int(page.evaluate("document.body.scrollHeight"))
            before_unique = len(unique_hotels)
            for pass_index in range(verification_passes):
                self.log(log_callback, f"Final verification pass {pass_index + 1} of {verification_passes}.")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)
                self.handle_booking_cookie_banner(page, log_callback, options)
                button_first = self.find_load_more_button(page, log_callback)
                self._update_unique_hotels_from_page(page, selector, unique_hotels, options, log_callback)
                page.evaluate("window.scrollBy(0, -Math.floor(window.innerHeight * 0.5))")
                page.wait_for_timeout(1000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(5000)
                button_second = self.find_load_more_button(page, log_callback)
                added = self._update_unique_hotels_from_page(page, selector, unique_hotels, options, log_callback)
                bottom_text = self._bottom_visible_text(page)
                end_text = self._detect_end_of_results_text(bottom_text)
                if end_text:
                    end_text_found = end_text
                any_button_visible = any_button_visible or button_first is not None or button_second is not None
                any_new_hotels = any_new_hotels or len(unique_hotels) > before_unique or added > 0
                height_after = int(page.evaluate("document.body.scrollHeight"))
                any_height_changed = any_height_changed or height_after > height_before
                height_before = height_after
            self._save_final_bottom_screenshot(page, screenshot_dir, screenshot_paths, options, log_callback)
            if end_text_found:
                options.stats["end_of_results_text"] = end_text_found
                self.log(log_callback, f"Final verification found end-of-results text: {end_text_found}")
            self.log(log_callback, f"Final verification: load more visible: {'yes' if any_button_visible else 'no'}")
            self.log(log_callback, f"Final verification: new unique hotels appeared: {'yes' if any_new_hotels else 'no'}")
            self.log(log_callback, f"Final verification: page height changed: {'yes' if any_height_changed else 'no'}")
            if any_button_visible:
                self.log(log_callback, "Final verification found a Load more button; continuing collection.")
                return False
            return (not any_new_hotels and not any_height_changed) or bool(end_text_found)
        except PlaywrightError as exc:
            options.stats["last_load_more_error"] = str(exc)
            self.log(log_callback, f"Final end verification failed: {exc}")
            return True

    def _save_final_bottom_screenshot(
        self,
        page,
        screenshot_dir: Path,
        screenshot_paths: list[Path],
        options: CollectorOptions,
        log_callback: LogCallback | None,
    ) -> Path | None:
        if not options.screenshots_enabled or options.performance_mode != "debug":
            return None
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = screenshot_dir / f"booking_final_bottom_page_{format_timestamp_for_filename()}.png"
        try:
            if not safe_screenshot(page, path, "final bottom", log_callback):
                return None
            resolved = path.resolve()
            screenshot_paths.append(resolved)
            options.stats["final_bottom_screenshot_path"] = str(resolved)
            options.stats["screenshot_paths"] = [str(item) for item in screenshot_paths]
            self.log(log_callback, f"Final bottom screenshot saved: {resolved}")
            return resolved
        except Exception as exc:
            self.log(log_callback, f"Final bottom screenshot failed: {exc}")
            return None

    def _write_complete_collection_proof(
        self,
        page,
        unique_hotels: dict[str, dict],
        options: CollectorOptions,
        screenshot_dir: Path,
        screenshot_paths: list[Path],
        log_callback: LogCallback | None,
    ) -> None:
        debug_dir = options.debug_dir or Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        final_screenshot = options.stats.get("final_bottom_screenshot_path")
        if not final_screenshot:
            saved = self._save_final_bottom_screenshot(page, screenshot_dir, screenshot_paths, options, log_callback)
            final_screenshot = str(saved) if saved else None
        final_url = safe_page_url(page, log_callback) or options.stats.get("final_url")
        page_title = safe_page_title(page, log_callback) or options.stats.get("final_page_title")
        summary = {
            "city": options.stats.get("current_city_or_region"),
            "checkin_date": options.stats.get("current_checkin_date"),
            "checkout_date": options.stats.get("current_checkout_date"),
            "final_url": final_url,
            "page_title": page_title,
            "booking_visible_result_count": options.stats.get("booking_visible_result_count"),
            "unique_hotels_collected": len(unique_hotels),
            "load_more_seen_count": options.stats.get("load_more_button_seen_count", 0),
            "load_more_click_count": options.stats.get("load_more_clicks", 0),
            "load_more_failed_click_count": options.stats.get("load_more_click_fail_count", 0),
            "total_scroll_attempts": options.stats.get("total_scroll_attempts", 0),
            "final_page_height": options.stats.get("final_page_height"),
            "lowest_price_collected": options.stats.get("lowest_price_collected"),
            "highest_price_collected": options.stats.get("highest_price_collected"),
            "stop_reason": options.stats.get("final_stop_reason"),
            "maximum_scroll_time_reached": bool(options.stats.get("maximum_scroll_time_reached")),
            "final_screenshot_path": final_screenshot,
        }
        summary_path = debug_dir / "booking_complete_collection_summary_latest.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        hotels_path = debug_dir / "booking_unique_hotels_loaded_latest.txt"
        lines = [
            f"{index}. {row.get('name') or '<no name>'} | {row.get('price') or '<no price>'} | {row.get('url') or '<no url>'}"
            for index, row in enumerate(unique_hotels.values(), start=1)
        ]
        hotels_path.write_text("\n".join(lines), encoding="utf-8")
        self.log(log_callback, f"Complete collection summary saved: {summary_path.resolve()}")
        self.log(log_callback, f"Unique hotel list saved: {hotels_path.resolve()}")

    def click_load_more_results_if_visible(
        self,
        page,
        selector: str,
        options: CollectorOptions,
        log_callback: LogCallback | None = None,
        screenshot_dir: Path | None = None,
        screenshot_paths: list[Path] | None = None,
        results_url: str | None = None,
    ) -> bool:
        before_count = self._best_hotel_card_count(page, selector, log_callback)
        return self.click_load_more_results(
            page,
            before_count,
            log_callback,
            selector=selector,
            options=options,
            screenshot_dir=screenshot_dir,
            screenshot_paths=screenshot_paths,
            results_url=results_url,
        )

    def _bulk_extract_cards(
        self,
        page,
        selector: str,
        max_cards: int,
        city_or_region,
        search_url,
        checkin_date,
        checkout_date,
        number_of_nights,
        adults,
        currency,
        screenshot_path: Path | None,
        log_callback: LogCallback | None,
        options: CollectorOptions,
    ) -> list[dict]:
        raw_rows = page.locator(selector).evaluate_all(
            r"""
            (cards, maxCards) => {
                const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
                const absoluteUrl = (href) => {
                    if (!href) return null;
                    try { return new URL(href, "https://www.booking.com").href; } catch { return href; }
                };
                const parseStar = (card) => {
                    const root = card.querySelector("[data-testid='title'], a[data-testid='title-link'], h3");
                    const roots = [];
                    if (root) {
                        roots.push(root, root.parentElement, root.parentElement?.parentElement, root.parentElement?.parentElement?.parentElement);
                    }
                    roots.push(...Array.from(card.querySelectorAll("[data-testid='rating-stars'], [data-testid*='rating-stars'], [aria-label*='star' i], [aria-label*='etoile' i], [aria-label*='étoile' i], [role='img'][aria-label]")).slice(0, 8));
                    const values = [];
                    for (const item of roots.filter(Boolean)) {
                        for (const el of [item, ...Array.from(item.querySelectorAll("[aria-label], [title], [alt]")).slice(0, 20)]) {
                            for (const attr of ["aria-label", "title", "alt"]) {
                                const value = clean(el.getAttribute(attr));
                                if (value) values.push(value);
                            }
                        }
                    }
                    const parse = (value) => {
                        const text = clean(value);
                        const match = text.match(/\b([1-5])\s*(?:out\s+of|of|sur)\s*5\b/i) || text.match(/\b([1-5])\s*(?:star|stars|etoile|etoiles|étoile|étoiles)\b/i);
                        if (match) return Number(match[1]);
                        const starChars = text.match(/[\u2605\u2b50\u2729]/g) || [];
                        if (starChars.length >= 1 && starChars.length <= 5) return starChars.length;
                        return null;
                    };
                    for (const value of values) {
                        const rating = parse(value);
                        if (rating) return { rating, raw: value, aria: value, count: 0, missing: null };
                    }
                    const starContainer = card.querySelector("[data-testid='rating-stars'], [data-testid*='rating-stars']");
                    const svgCount = starContainer ? starContainer.querySelectorAll("svg").length : 0;
                    if (svgCount >= 1 && svgCount <= 5) {
                        return { rating: svgCount, raw: `${svgCount} yellow star icon${svgCount === 1 ? "" : "s"}`, aria: null, count: svgCount, missing: null };
                    }
                    return { rating: null, raw: null, aria: null, count: 0, missing: "No star signal found in bulk extraction." };
                };
                const rows = [];
                for (const card of cards.slice(0, maxCards)) {
                    const titleLink = card.querySelector("a[data-testid='title-link'], a[href*='/hotel/']");
                    const titleEl = card.querySelector("[data-testid='title'], a[data-testid='title-link'], h3");
                    const reviewEl = card.querySelector("[data-testid='review-score']");
                    const reviewText = clean(reviewEl?.innerText);
                    const reviewCountMatches = Array.from(reviewText.matchAll(/([\d,.]+)\s+reviews?\b/gi));
                    const reviewCountMatch = reviewCountMatches.length ? reviewCountMatches[reviewCountMatches.length - 1] : null;
                    const priceEl = card.querySelector("[data-testid='price-and-discounted-price']") || Array.from(card.querySelectorAll("span, div")).find((el) => /(?:US\$|\$|€|£)\s*\d/.test(clean(el.textContent)));
                    const findPriceText = (root) => {
                        const direct = root.querySelector("[data-testid='price-and-discounted-price']");
                        const candidates = [];
                        if (direct) candidates.push(clean(direct.innerText || direct.textContent));
                        Array.from(root.querySelectorAll("span, div")).slice(0, 220).forEach((el) => {
                            const value = clean(el.innerText || el.textContent);
                            if (value) candidates.push(value);
                        });
                        const joined = clean(root.innerText || root.textContent);
                        if (joined) candidates.push(joined);
                        const patterns = [
                            /US\$\s*[0-9,]+(?:\.[0-9]+)?/i,
                            /\$\s*[0-9,]+(?:\.[0-9]+)?/,
                            /\u20ac\s*[0-9,]+(?:\.[0-9]+)?/,
                            /\u00a3\s*[0-9,]+(?:\.[0-9]+)?/
                        ];
                        for (const candidate of candidates) {
                            for (const pattern of patterns) {
                                const match = candidate.match(pattern);
                                if (match) return match[0];
                            }
                        }
                        return direct ? clean(direct.innerText || direct.textContent) : null;
                    };
                    const priceText = findPriceText(card) || clean(priceEl?.innerText || priceEl?.textContent);
                    const roomEl = card.querySelector("[data-testid='recommended-units'] h4, h4");
                    const text = clean(card.innerText);
                    const star = parseStar(card);
                    rows.push({
                        raw_hotel_name: clean(titleEl?.innerText),
                        hotel_url: absoluteUrl(titleLink?.getAttribute("href")),
                        review_score: reviewText,
                        review_count: reviewCountMatch ? reviewCountMatch[0] : reviewText,
                        cheapest_room_name: clean(roomEl?.innerText) || null,
                        raw_price_text: priceText,
                        cheapest_price_total: priceText,
                        raw_card_text: text,
                        star_rating: star.rating,
                        raw_star_signal: star.raw,
                        star_aria_label: star.aria,
                        star_icon_count: star.count,
                        star_rating_missing_reason: star.missing,
                    });
                }
                return rows;
            }
            """,
            max_cards,
        )

        results: list[dict] = []
        seen: dict[str, dict] = {}
        for index, raw in enumerate(raw_rows, start=1):
            raw_name = raw.get("raw_hotel_name")
            name = clean_hotel_name(raw_name)
            key = raw.get("hotel_url") or name
            if not key or key in seen:
                continue
            row = {
                "source": self.source_name,
                "city_or_region": city_or_region,
                "search_url": search_url,
                "hotel_name": name,
                "raw_hotel_name": raw_name,
                "property_type_guess": property_type_guess(name, raw.get("raw_card_text")),
                "excluded_by_hotels_only_filter": False,
                "ota_hotel_id": None,
                "star_rating": raw.get("star_rating"),
                "raw_star_signal": raw.get("raw_star_signal"),
                "star_aria_label": raw.get("star_aria_label"),
                "star_icon_count": raw.get("star_icon_count"),
                "star_rating_missing_reason": raw.get("star_rating_missing_reason"),
                "review_score": raw.get("review_score"),
                "review_count": raw.get("review_count"),
                "room_name": raw.get("cheapest_room_name"),
                "cheapest_room_name": raw.get("cheapest_room_name"),
                "raw_price_text": raw.get("raw_price_text") or raw.get("cheapest_price_total"),
                "parsed_price": parse_price_to_decimal(raw.get("raw_price_text") or raw.get("cheapest_price_total")),
                "cheapest_price_total": raw.get("cheapest_price_total"),
                "currency": currency,
                "taxes_and_fees_text": None,
                "provider_name": self.provider_name,
                "raw_source_payload": json.dumps(raw, ensure_ascii=True, default=str),
                "checkin_date": checkin_date,
                "checkout_date": checkout_date,
                "number_of_nights": number_of_nights,
                "adults": adults,
                "hotel_url": raw.get("hotel_url"),
                "ranking_position_on_page": index,
                "screenshot_path": str(screenshot_path) if screenshot_path else None,
                "collection_status": "success" if name else "failed",
                "error_message": None if name else "Hotel name selector returned no value.",
                "collected_at": datetime.now(),
            }
            seen[key] = row
            results.append(row)
            if options.debug_mode and index <= 10:
                self.log(log_callback, f"Bulk hotel {index}: {name}; star={row['star_rating']}; price={row['cheapest_price_total']}")
        self.log(log_callback, f"Bulk extracted {len(results)} unique Booking.com hotels from {len(raw_rows)} DOM cards.")
        return results

    def _price_from_card_text(self, card_text: str | None) -> str | None:
        if not card_text:
            return None
        patterns = [
            r"US\$\s*[0-9,]+(?:\.[0-9]+)?",
            r"\$\s*[0-9,]+(?:\.[0-9]+)?",
            r"\u20ac\s*[0-9,]+(?:\.[0-9]+)?",
            r"\u00a3\s*[0-9,]+(?:\.[0-9]+)?",
        ]
        for pattern in patterns:
            match = re.search(pattern, card_text, re.I)
            if match:
                return match.group(0)
        return None

    def _extract_card(self, card, index: int, city_or_region, search_url, checkin_date, checkout_date, number_of_nights, adults, currency, screenshot_path: Path | None, log_callback: LogCallback | None) -> dict:
        raw_name = self._text(card, "a[data-testid='title-link']") or self._text(card, "[data-testid='title'], h3")
        name = clean_hotel_name(raw_name)
        try:
            card_text = card.inner_text(timeout=1500)
        except PlaywrightError:
            card_text = ""
        price = self._text(card, "[data-testid='price-and-discounted-price'], span:has-text('$'), span:has-text('€'), span:has-text('£')")
        if not price:
            price = self._price_from_card_text(card_text)
        review_score = self._text(card, "[data-testid='review-score'] div, [data-testid='review-score'], [aria-label*='Scored']")
        review_count = self._text(card, "[data-testid='review-score'] div:has-text('review'), div:has-text('reviews')")
        link = self._attr(card, "a[data-testid='title-link'], a[href*='/hotel/']", "href")
        if link:
            link = urljoin("https://www.booking.com", link)
        star_details = extract_booking_star_rating_details(card)
        star_rating = star_details.get("rating")
        raw_star_signal = star_details.get("raw_star_signal")
        star_aria_label = star_details.get("aria_label")
        star_icon_count = star_details.get("star_icon_count")
        missing_reason = star_details.get("missing_reason")
        if index <= 10:
            self.log(log_callback, f"Hotel: {name}")
            self.log(log_callback, f"Raw star signal: {raw_star_signal}")
            self.log(log_callback, f"Aria star label: {star_aria_label}")
            self.log(log_callback, f"Star icon count: {star_icon_count}")
            self.log(log_callback, f"Parsed star rating: {star_rating}")
            self.log(log_callback, f"Final star rating saved: {star_rating}")
            if missing_reason:
                self.log(log_callback, f"Star rating missing reason: {missing_reason}")

        return {
            "source": self.source_name,
            "city_or_region": city_or_region,
            "search_url": search_url,
            "hotel_name": name,
            "raw_hotel_name": raw_name,
            "property_type_guess": property_type_guess(name, card_text),
            "excluded_by_hotels_only_filter": False,
            "ota_hotel_id": None,
            "star_rating": star_rating,
            "raw_star_signal": raw_star_signal,
            "star_aria_label": star_aria_label,
            "star_icon_count": star_icon_count,
            "star_rating_missing_reason": missing_reason,
            "review_score": review_score,
            "review_count": review_count,
            "room_name": None,
            "cheapest_room_name": None,
            "raw_price_text": price,
            "parsed_price": parse_price_to_decimal(price),
            "cheapest_price_total": price,
            "currency": currency,
            "taxes_and_fees_text": None,
            "provider_name": self.provider_name,
            "raw_source_payload": card_text,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "number_of_nights": number_of_nights,
            "adults": adults,
            "hotel_url": link,
            "ranking_position_on_page": index,
            "screenshot_path": str(screenshot_path) if screenshot_path else None,
            "collection_status": "success" if name else "failed",
            "error_message": None if name else "Hotel name selector returned no value.",
            "collected_at": datetime.now(),
        }

    def _extract_star_rating(self, card) -> tuple[Decimal | None, str | None, str | None]:
        details = extract_booking_star_rating_details(card)
        rating = details.get("rating")
        if rating is not None:
            return Decimal(int(rating)), details.get("raw_star_signal"), None
        return None, details.get("raw_star_signal"), details.get("missing_reason")

    def _extract_star_rating_old(self, card) -> tuple[Decimal | None, str | None, str | None]:
        raw_values: list[str] = []
        selectors = [
            "[data-testid='rating-stars']",
            "[aria-label*='star' i]",
            "[aria-label*='stars' i]",
            "[aria-label*='étoile' i]",
            "[aria-label*='étoiles' i]",
            "[role='img'][aria-label]",
            "[data-testid*='rating' i]",
        ]
        for selector in selectors:
            try:
                elements = card.locator(selector)
                for index in range(min(elements.count(), 8)):
                    element = elements.nth(index)
                    aria = element.get_attribute("aria-label", timeout=1000)
                    title = element.get_attribute("title", timeout=1000)
                    text = element.inner_text(timeout=1000).strip()
                    for value in (aria, title, text):
                        if value and self._looks_like_star_text(value):
                            raw_values.append(value)
            except PlaywrightError:
                continue

        try:
            svg_count = card.locator("svg").count()
            if 1 <= svg_count <= 5:
                raw_values.append(f"{svg_count} stars from SVG icons")
        except PlaywrightError:
            pass

        try:
            card_text = card.inner_text(timeout=1500)
            text_match = re.search(r"\b([1-5])\s*star(?:s)?\s*hotel\b", card_text, re.I)
            if text_match:
                raw_values.append(text_match.group(0))
        except PlaywrightError:
            pass

        for raw in raw_values:
            parsed = parse_star_rating(raw)
            if parsed is not None and Decimal("1") <= parsed <= Decimal("5"):
                return parsed, raw, None
        if raw_values:
            return None, " | ".join(raw_values[:5]), "Star text found but could not parse numeric 1-5 rating."
        return None, None, "No star aria label, role img label, SVG star count, or visible star text found."

    def _looks_like_star_text(self, value: str) -> bool:
        lowered = value.lower()
        return any(
            token in lowered
            for token in [
                "1 star",
                "2 star",
                "3 star",
                "4 star",
                "5 star",
                "one star",
                "two star",
                "three star",
                "four star",
                "five star",
                "étoile",
                "étoiles",
                "stars",
            ]
        )

    def _filter_by_star_rating(self, results: list[dict], options: CollectorOptions, log_callback: LogCallback | None) -> list[dict]:
        selected = {int(value) for value in options.selected_star_ratings}
        before_count = len(results)
        known_count = sum(1 for row in results if row.get("star_rating") is not None)
        unknown_count = before_count - known_count
        kept: list[dict] = []
        skipped = 0
        for row in results:
            star = row.get("star_rating")
            if star is None:
                if options.include_unknown_star_rating:
                    kept.append(row)
                else:
                    skipped += 1
                continue
            if int(Decimal(star)) in selected:
                kept.append(row)
            else:
                skipped += 1

        self.log(log_callback, f"Hotels extracted before filtering: {before_count}")
        self.log(log_callback, f"Hotels with known star rating: {known_count}")
        self.log(log_callback, f"Hotels with unknown star rating: {unknown_count}")
        self.log(log_callback, f"Hotels skipped because star rating did not match: {skipped}")
        self.log(log_callback, f"Hotels kept after filtering: {len(kept)}")
        options.stats["hotels_with_known_star_rating"] = known_count
        options.stats["hotels_with_unknown_star_rating"] = unknown_count
        options.stats["hotels_removed_by_star_filter"] = skipped
        options.stats["hotels_kept_after_star_filter"] = len(kept)
        if before_count and not kept:
            self.log(log_callback, "WARNING: All hotels were removed by the star rating filter. Try enabling Include hotels with unknown star rating or select All star ratings.")
        return kept

    def _apply_hotels_only_ui_filter(self, page, log_callback: LogCallback | None) -> None:
        try:
            property_filter = self._first_visible(
                page,
                [
                    "button:has-text('Property type')",
                    "button:has-text('Accommodation type')",
                    "div:has-text('Property type')",
                    "div:has-text('Accommodation type')",
                ],
                timeout_ms=2500,
            )
            if property_filter is not None:
                try:
                    property_filter.click(timeout=2500)
                    page.wait_for_timeout(600)
                except PlaywrightError:
                    pass
            hotel_option = self._first_visible(
                page,
                [
                    "label:has-text('Hotels')",
                    "div[role='group'] label:has-text('Hotels')",
                    "button:has-text('Hotels')",
                    "input[aria-label='Hotels']",
                ],
                timeout_ms=3000,
            )
            if hotel_option is not None:
                self.log(log_callback, "Hotels only filter found")
                hotel_option.click(timeout=4000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.wait_for_timeout(1500)
                self.log(log_callback, "Hotels only filter applied")
                return
        except PlaywrightError:
            pass
        self.log(log_callback, "Hotels only UI filter not found. Applying local post extraction filter.")

    def _filter_hotels_only(self, results: list[dict], options: CollectorOptions, log_callback: LogCallback | None) -> list[dict]:
        raw_count = len(results)
        options.stats["raw_records_extracted"] = int(options.stats.get("raw_records_extracted", 0)) + raw_count
        if not options.hotels_only:
            options.stats["records_after_hotels_only_filter"] = int(options.stats.get("records_after_hotels_only_filter", 0)) + raw_count
            options.stats["hotels_only_removed"] = int(options.stats.get("hotels_only_removed", 0))
            return results
        kept: list[dict] = []
        removed: list[dict] = []
        for row in results:
            name = row.get("hotel_name")
            card_text = row.get("raw_hotel_name") or name
            if row.get("collection_status") != "success" or not name:
                row["excluded_by_hotels_only_filter"] = True
                removed.append(row)
                continue
            if is_probable_hotel(name, card_text):
                kept.append(row)
            else:
                row["excluded_by_hotels_only_filter"] = True
                removed.append(row)
        options.stats["records_after_hotels_only_filter"] = int(options.stats.get("records_after_hotels_only_filter", 0)) + len(kept)
        options.stats["hotels_only_removed"] = int(options.stats.get("hotels_only_removed", 0)) + len(removed)
        self.log(log_callback, f"raw records extracted: {raw_count}")
        self.log(log_callback, f"records kept after hotels only filter: {len(kept)}")
        self.log(log_callback, f"records removed as vacation rental or private accommodation: {len(removed)}")
        self.log(log_callback, f"first 10 removed names: {[row.get('hotel_name') for row in removed[:10]]}")
        self.log(log_callback, f"first 10 kept names: {[row.get('hotel_name') for row in kept[:10]]}")
        return kept

    def _detect_access_restriction(self, page, log_callback: LogCallback | None) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=3000).lower()
        except PlaywrightError:
            text = ""
        matched = [pattern for pattern in self.ACCESS_RESTRICTION_PATTERNS if pattern in text]
        if matched:
            self.log(log_callback, f"Access restriction or CAPTCHA wording detected: {', '.join(matched)}")
            return True
        self.log(log_callback, "Access restriction/CAPTCHA check: not detected.")
        return False

    def _log_page_verification(self, page, city_or_region: str, log_callback: LogCallback | None) -> None:
        try:
            text = page.locator("body").inner_text(timeout=5000)
        except PlaywrightError:
            text = ""
        lowered = text.lower()
        checks = {
            "contains city name": city_or_region.lower() in lowered,
            "contains property/hotel words": bool(re.search(r"\b(property|properties|hotel|hotels|stays?)\b", lowered)),
            "contains price symbols": bool(re.search(r"[$€£]|us\\$|mxn|eur|gbp", text, re.I)),
            "contains review score patterns": bool(re.search(r"\b\d[.,]\d\b|review score|reviews?", lowered)),
            "contains access restriction wording": any(pattern in lowered for pattern in self.ACCESS_RESTRICTION_PATTERNS),
        }
        for label, passed in checks.items():
            self.log(log_callback, f"Booking.com verification: {label}: {'yes' if passed else 'no'}")

    def _write_card_debug(self, cards, debug_dir: Path, options: CollectorOptions, log_callback: LogCallback | None) -> None:
        if options.performance_mode != "debug":
            self.log(log_callback, f"{options.performance_mode.title()} mode: skipped first-card raw HTML export.")
            return
        path = debug_dir / "booking_first_10_cards_debug.txt"
        sections: list[str] = []
        for index, card in enumerate(cards, start=1):
            try:
                text = card.inner_text(timeout=2000)
            except PlaywrightError as exc:
                text = f"<text unavailable: {exc}>"
            try:
                raw_name = self._text(card, "a[data-testid='title-link']") or self._text(card, "[data-testid='title'], h3")
                name = clean_hotel_name(raw_name)
            except PlaywrightError:
                name = ""
            star_details = extract_booking_star_rating_details(card)
            try:
                rating_html = card.evaluate(
                    r"""
                    (card) => {
                        const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
                        const title = card.querySelector("[data-testid='title'], a[data-testid='title-link'], h3");
                        const explicit = card.querySelector("[data-testid='rating-stars'], [data-testid*='rating-stars'], [aria-label*='star' i], [aria-label*='etoile' i], [aria-label*='\u00e9toile' i], [role='img'][aria-label], [title*='star' i], [title*='etoile' i], [title*='\u00e9toile' i]");
                        const roots = [];
                        if (title) {
                            roots.push({ label: "title", html: title.outerHTML });
                            if (title.parentElement) roots.push({ label: "title parent", html: title.parentElement.outerHTML });
                            if (title.parentElement?.parentElement) roots.push({ label: "title grandparent", html: title.parentElement.parentElement.outerHTML });
                        }
                        if (explicit) {
                            roots.push({ label: "explicit star/rating element", html: explicit.outerHTML });
                            if (explicit.parentElement) roots.push({ label: "explicit star/rating parent", html: explicit.parentElement.outerHTML });
                        }
                        return roots.map((item) => `--- ${item.label} ---\n${clean(item.html).slice(0, 5000)}`).join("\n\n");
                    }
                    """
                )
            except PlaywrightError as exc:
                rating_html = f"<rating html unavailable: {exc}>"
            try:
                html = card.evaluate("el => el.outerHTML")
            except PlaywrightError as exc:
                html = f"<html unavailable: {exc}>"
            sections.append(
                "\n".join(
                    [
                        f"===== CARD {index} STAR DEBUG =====",
                        f"Hotel name: {name}",
                        f"Raw star signal: {star_details.get('raw_star_signal')}",
                        f"Aria label found: {star_details.get('aria_label')}",
                        f"Star icon count: {star_details.get('star_icon_count')}",
                        f"Parsed star rating: {star_details.get('rating')}",
                        f"Missing reason: {star_details.get('missing_reason')}",
                        f"Candidate signals: {json.dumps(star_details.get('candidates') or [], ensure_ascii=False)}",
                        "",
                        f"===== CARD {index} TEXT =====",
                        text,
                        "",
                        f"===== CARD {index} RATING/TITLE HTML SNIPPET =====",
                        rating_html[:12000],
                        "",
                        f"===== CARD {index} FULL HTML =====",
                        html[:16000],
                        "",
                    ]
                )
            )
        path.write_text("\n".join(sections), encoding="utf-8")
        self.log(log_callback, f"Booking.com first 10 card debug file saved: {path.resolve()}")

    def _force_screenshot(
        self,
        page,
        screenshot_dir: Path,
        prefix: str,
        screenshot_paths: list[Path],
        options: CollectorOptions | None,
        log_callback: LogCallback | None,
    ) -> None:
        prefix_lower = prefix.lower()
        if options is not None and options.performance_mode != "debug" and not any(token in prefix_lower for token in ["error", "failed", "stuck", "zero", "access"]):
            return
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = screenshot_dir / f"{prefix}_{format_timestamp_for_filename()}.png"
        try:
            if not safe_screenshot(page, path, prefix, log_callback):
                return
            screenshot_paths.append(path.resolve())
            if options is not None:
                options.stats["screenshot_paths"] = [str(item) for item in screenshot_paths]
                options.stats["latest_screenshot_path"] = str(path.resolve())
                self._notify_status(options, f"Screenshot saved: {prefix}", latest_screenshot_path=str(path.resolve()))
            self.log(log_callback, f"Screenshot saved: {path.resolve()}")
        except Exception as exc:
            self.log(log_callback, f"Screenshot failed at {prefix}: {exc}")

    def _write_load_more_stuck_debug(self, page, log_callback: LogCallback | None, options: CollectorOptions | None = None) -> None:
        debug_dir = options.debug_dir if options and options.debug_dir else Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"load_more_stuck_page_text_{format_timestamp_for_filename()}.txt"
        try:
            text = self._safe_body_text(page)
            lower_text = text.lower()
            found = any(
                phrase in lower_text
                for phrase in [
                    "load more results",
                    "show more results",
                    "load more",
                    "see more results",
                    "afficher plus",
                    "charger plus",
                    "voir plus",
                ]
            )
            bottom_text = page.evaluate(
                """
                () => {
                    const nodes = Array.from(document.body.querySelectorAll("button, a, div, span"))
                        .filter((node) => {
                            const rect = node.getBoundingClientRect();
                            return rect.top > window.innerHeight * 0.55 && rect.top < window.innerHeight + 500;
                        })
                        .map((node) => node.innerText || node.textContent || "")
                        .filter(Boolean)
                        .slice(0, 80);
                    return nodes.join("\\n---\\n");
                }
                """
            )
            path.write_text(
                f"Load more visible text found: {'yes' if found else 'no'}\n\n===== BODY TEXT TAIL =====\n{text[-5000:]}\n\n===== BOTTOM AREA TEXT =====\n{bottom_text}",
                encoding="utf-8",
            )
            self.log(log_callback, f"Load more stuck page text saved: {path.resolve()}")
        except (OSError, PlaywrightError) as exc:
            self.log(log_callback, f"Could not write Load more stuck debug text: {exc}")

    def _screenshot(self, page, screenshot_dir: Path, prefix: str, options: CollectorOptions, screenshot_paths: list[Path], log_callback: LogCallback | None) -> None:
        prefix_lower = prefix.lower()
        diagnostic = any(token in prefix_lower for token in ["error", "zero", "access", "captcha", "stuck", "failed"])
        if not options.screenshots_enabled and not diagnostic:
            return
        if options.performance_mode != "debug" and not diagnostic:
            return
        limit = max(1, int(options.stats.get("screenshot_limit", 12)))
        if len(screenshot_paths) >= limit:
            if not options.stats.get("screenshot_limit_logged"):
                options.stats["screenshot_limit_logged"] = True
                self.log(log_callback, f"Screenshot limit reached ({limit}) for this date; further screenshots skipped.")
            return
        path = screenshot_dir / f"{prefix}_{format_timestamp_for_filename()}.png"
        try:
            if not safe_screenshot(page, path, prefix, log_callback):
                return
            screenshot_paths.append(path.resolve())
            options.stats["screenshot_paths"] = [str(item) for item in screenshot_paths]
            options.stats["latest_screenshot_path"] = str(path.resolve())
            self._notify_status(options, f"Screenshot saved: {prefix}", latest_screenshot_path=str(path.resolve()))
            self.log(log_callback, f"Screenshot saved: {path.resolve()}")
        except Exception as exc:
            self.log(log_callback, f"Screenshot failed at {prefix}: {exc}")

    def _write_debug_json(
        self,
        page,
        results_url: str | None,
        screenshot_paths: list[Path],
        options: CollectorOptions,
        debug_dir: Path,
        error_message: str | None,
        log_callback: LogCallback | None,
    ) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            current_url = page.url
            page_title = safe_page_title(page)
        except PlaywrightError:
            current_url = None
            page_title = None
        payload = {
            "current_url": current_url,
            "results_url": results_url,
            "page_title": page_title,
            "cookie_banner_detected": bool(options.stats.get("cookie_banner_detected")),
            "cookie_banner_declined": bool(options.stats.get("cookie_banner_declined")),
            "genius_popup_closed": bool(options.stats.get("genius_popup_closed")),
            "load_more_button_found": bool(options.stats.get("load_more_button_found")),
            "load_more_click_count": int(options.stats.get("load_more_clicks", 0)),
            "load_more_button_seen_count": int(options.stats.get("load_more_button_seen_count", 0)),
            "load_more_click_fail_count": int(options.stats.get("load_more_click_fail_count", 0)),
            "last_load_more_error": options.stats.get("last_load_more_error"),
            "load_more_last_button_text": options.stats.get("load_more_last_button_text"),
            "load_more_last_button_strategy": options.stats.get("load_more_last_button_strategy"),
            "load_more_last_button_box": options.stats.get("load_more_last_button_box"),
            "load_more_last_button_visible": bool(options.stats.get("load_more_last_button_visible")),
            "load_more_last_button_enabled": bool(options.stats.get("load_more_last_button_enabled")),
            "load_more_cookie_banner_visible_before_click": bool(options.stats.get("load_more_cookie_banner_visible_before_click")),
            "load_more_bottom_scan_attempts": int(options.stats.get("load_more_bottom_scan_attempts", 0)),
            "hotel_cards_before_load_more": int(options.stats.get("hotel_cards_before_load_more", 0)),
            "hotel_cards_after_load_more": int(options.stats.get("hotel_cards_after_load_more", 0)),
            "hotel_cards_before_last_click": int(options.stats.get("hotel_cards_before_last_click", 0)),
            "hotel_cards_after_last_click": int(options.stats.get("hotel_cards_after_last_click", 0)),
            "new_cards_after_last_click": int(options.stats.get("new_cards_after_last_click", 0)),
            "consecutive_no_new_card_attempts": int(options.stats.get("consecutive_no_new_card_attempts", 0)),
            "consecutive_no_button_attempts": int(options.stats.get("consecutive_no_button_attempts", 0)),
            "unexpected_new_pages_closed": int(options.stats.get("unexpected_new_pages_closed", 0)),
            "unexpected_property_navigation_count": int(options.stats.get("unexpected_property_navigation_count", 0)),
            "final_url": options.stats.get("final_url") or current_url,
            "final_page_title": options.stats.get("final_page_title") or page_title,
            "visible_result_count_text": options.stats.get("visible_result_count_text"),
            "target_result_count": options.stats.get("target_result_count"),
            "visible_result_count_is_lower_bound": bool(options.stats.get("visible_result_count_is_lower_bound")),
            "completion_status": options.stats.get("completion_status"),
            "booking_visible_result_count": options.stats.get("booking_visible_result_count"),
            "visible_hotel_cards_count": int(options.stats.get("visible_hotel_cards_count", 0)),
            "unique_results_loaded": int(options.stats.get("unique_results_loaded", 0)),
            "estimated_missing_results": options.stats.get("estimated_missing_results"),
            "unique_hotels_collected": int(options.stats.get("unique_hotels_collected", 0)),
            "new_hotels_added_last_cycle": int(options.stats.get("new_hotels_added_last_cycle", 0)),
            "lowest_price_collected": options.stats.get("lowest_price_collected"),
            "highest_price_collected": options.stats.get("highest_price_collected"),
            "hotels_with_valid_price": int(options.stats.get("hotels_with_valid_price", 0)),
            "hotels_missing_price": int(options.stats.get("hotels_missing_price", 0)),
            "total_scroll_attempts": int(options.stats.get("total_scroll_attempts", 0)),
            "final_page_height": int(options.stats.get("final_page_height", 0)),
            "final_stop_reason": options.stats.get("final_stop_reason"),
            "final_bottom_screenshot_path": options.stats.get("final_bottom_screenshot_path"),
            "booking_result_count_difference": options.stats.get("booking_result_count_difference"),
            "collection_completeness_warning": options.stats.get("collection_completeness_warning"),
            "load_more_success_count": int(options.stats.get("load_more_success_count", 0)),
            "cycle_count": int(options.stats.get("cycle_count", 0)),
            "consecutive_no_new_unique_results": int(options.stats.get("consecutive_no_new_unique_results", 0)),
            "consecutive_no_load_more_seen": int(options.stats.get("consecutive_no_load_more_seen", 0)),
            "consecutive_failed_load_more_clicks": int(options.stats.get("consecutive_failed_load_more_clicks", 0)),
            "full_loading_debug_json_path": options.stats.get("full_loading_debug_json_path"),
            "unique_results_loaded_path": options.stats.get("unique_results_loaded_path"),
            "bottom_visible_text_path": options.stats.get("bottom_visible_text_path"),
            "end_of_results_text": options.stats.get("end_of_results_text"),
            "final_hotel_card_count": int(options.stats.get("final_cards_found", 0)),
            "hotel_records_extracted": int(options.stats.get("hotel_records_extracted", 0)),
            "final_extracted_hotel_count": int(options.stats.get("final_extracted_hotel_count", 0)),
            "maximum_scroll_time_reached": bool(options.stats.get("maximum_scroll_time_reached")),
            "hotels_kept_after_filters": int(options.stats.get("records_after_hotels_only_filter", 0)),
            "hotels_removed_by_hotels_only_filter": int(options.stats.get("hotels_only_removed", 0)),
            "hotels_with_known_star_rating": int(options.stats.get("hotels_with_known_star_rating", 0)),
            "hotels_with_unknown_star_rating": int(options.stats.get("hotels_with_unknown_star_rating", 0)),
            "hotels_removed_by_star_filter": int(options.stats.get("hotels_removed_by_star_filter", 0)),
            "hotels_kept_after_star_filter": int(options.stats.get("hotels_kept_after_star_filter", 0)),
            "error_message": error_message or options.stats.get("error_message"),
            "screenshot_paths": [str(path) for path in screenshot_paths],
        }
        path = debug_dir / "booking_debug_latest.json"
        try:
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            self.log(log_callback, f"Booking.com debug JSON saved: {path.resolve()}")
        except OSError as exc:
            self.log(log_callback, f"Could not write Booking.com debug JSON: {exc}")

    def _log_zero_cards_help(self, page, screenshot_path: Path | None, log_callback: LogCallback | None, debug_dir: Path | None = None) -> None:
        self.log(log_callback, "Booking.com search completed, but no hotel cards were detected.")
        self.log(log_callback, f"Final URL: {safe_page_url(page, log_callback)}")
        self.log(log_callback, f"Page title: {safe_page_title(page, log_callback)}")
        if screenshot_path:
            self.log(log_callback, f"Screenshot path: {screenshot_path}")
        visible_text = self._safe_body_text(page)[:3000]
        debug_dir = debug_dir or Path("data/debug")
        debug_path = debug_dir / "booking_zero_cards_page_text.txt"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(visible_text, encoding="utf-8")
        self.log(log_callback, f"First 3000 characters of visible page text saved: {debug_path.resolve()}")
        self.log(log_callback, "Possible reasons: page layout changed; cookie modal blocked the page; date or city has no availability; Booking.com returned a different page; access restriction was shown; selectors need updating.")

    def _text(self, locator, selector: str) -> str | None:
        child = locator.locator(selector).first
        if child.count() == 0:
            return None
        text = child.inner_text(timeout=3000).strip()
        return text or None

    def _attr(self, locator, selector: str, attr: str) -> str | None:
        child = locator.locator(selector).first
        if child.count() == 0:
            return None
        return child.get_attribute(attr, timeout=3000)

    def _failed_result(self, exc, city_or_region, search_url, checkin_date, checkout_date, number_of_nights, adults, currency, position, screenshot_path: Path | None = None):
        return {
            "source": self.source_name,
            "city_or_region": city_or_region,
            "search_url": search_url,
            "hotel_name": None,
            "ota_hotel_id": None,
            "star_rating": None,
            "review_score": None,
            "review_count": None,
            "room_name": None,
            "cheapest_room_name": None,
            "raw_price_text": None,
            "parsed_price": None,
            "cheapest_price_total": None,
            "currency": currency,
            "taxes_and_fees_text": None,
            "provider_name": self.provider_name,
            "raw_source_payload": None,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "number_of_nights": number_of_nights,
            "adults": adults,
            "hotel_url": None,
            "ranking_position_on_page": position,
            "screenshot_path": str(screenshot_path) if screenshot_path else None,
            "collection_status": "failed",
            "error_message": str(exc),
            "collected_at": datetime.now(),
        }
