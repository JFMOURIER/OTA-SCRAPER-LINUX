from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one small visible/headless Booking.com comparison.")
    parser.add_argument("--destination", default="Orlando")
    parser.add_argument("--date", default=(date.today() + timedelta(days=14)).isoformat())
    parser.add_argument("--max-properties", type=int, default=3)
    parser.add_argument("--mode", choices=("visible", "headless", "both"), default="both")
    parser.add_argument("--cancel-after-seconds", type=float)
    parser.add_argument("--data-dir", default="data/diagnostics")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_properties < 1 or args.max_properties > 10:
        raise SystemExit("--max-properties must be between 1 and 10")
    stay_date = date.fromisoformat(args.date)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (BASE_DIR / args.data_dir / stamp).resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["INSTANCE_ID"] = f"diagnostic_{stamp}"
    os.environ["INSTANCE_NAME"] = "Controlled scraper diagnostic"
    os.environ["INSTANCE_DATA_DIR"] = str(root)
    os.environ["INSTANCE_PORT"] = "8599"
    os.environ["DB_BACKEND"] = "sqlite"

    from collectors.base import CollectorOptions
    from collectors.booking_playwright import BookingPlaywrightCollector
    from database.db import (
        create_collection_run,
        init_db,
        insert_hotel_results,
        sqlite_integrity_check,
        update_collection_run_status,
    )
    from services.normalizer import normalize_hotel_result
    from services.resource_guard import ResourceGuard, SingleScraperLock, cleanup_owned_browser_processes

    init_db("sqlite")
    log_path = root / "comparison.log"
    comparison_path = root / "comparison.json"
    modes = ["visible", "headless"] if args.mode == "both" else [args.mode]
    results_by_mode: dict[str, dict] = {}
    lock = SingleScraperLock(BASE_DIR / "data" / "active_scraper.lock")
    lock.acquire(f"diagnostic_{stamp}")

    def log(message: str) -> None:
        line = f"{datetime.now().isoformat(sep=' ', timespec='seconds')} {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    try:
        for mode in modes:
            headless = mode == "headless"
            stop_event = threading.Event()
            timer = None
            if args.cancel_after_seconds is not None:
                timer = threading.Timer(args.cancel_after_seconds, stop_event.set)
                timer.start()
            guard = ResourceGuard(root, owner_pid=os.getpid())
            before = guard.check(starting=True, force=True)
            run_id = create_collection_run(
                "Booking.com",
                args.destination,
                stay_date,
                stay_date + timedelta(days=1),
                1,
                2,
                "USD",
                args.max_properties,
                backend="sqlite",
            )
            options = CollectorOptions(
                collect_all_available=False,
                max_scroll_minutes=1,
                selected_star_ratings=(1, 2, 3, 4, 5),
                include_unknown_star_rating=True,
                debug_mode=False,
                screenshots_enabled=False,
                headless=headless,
                fast_mode=False,
                performance_mode="balanced",
                block_images_and_fonts=False,
                stop_event=stop_event,
                hotels_only=True,
                screenshot_dir=root / "screenshots" / mode,
                debug_dir=root / "debug" / mode,
                browser_profile_dir=root / "browser_profiles" / f"{mode}_{uuid4().hex}",
                status_dir=root / "status",
                partial_dir=root / "partial",
                log_dir=root,
                instance_data_dir=root,
            )
            started = time.perf_counter()
            mode_payload: dict[str, object] = {"mode": mode, "headless": headless, "run_id": run_id}
            try:
                raw = BookingPlaywrightCollector().collect(
                    args.destination,
                    stay_date,
                    stay_date + timedelta(days=1),
                    2,
                    "USD",
                    args.max_properties,
                    False,
                    options,
                    lambda value, message: (guard.check(), log(f"{mode} progress={value:.2f} {message}")),
                    lambda message: log(f"{mode} {message}"),
                )
                normalized = [normalize_hotel_result({**row, "collection_run_id": run_id}) for row in raw]
                inserted = insert_hotel_results(normalized, backend="sqlite")
                status = "stopped_by_user" if stop_event.is_set() else "completed"
                update_collection_run_status(run_id, status, backend="sqlite")
                mode_payload.update(
                    {
                        "status": status,
                        "rows_inserted": inserted,
                        "hotel_cards_detected": options.stats.get("final_cards_found"),
                        "parsed_fields": sorted({key for row in normalized for key, value in row.items() if value not in (None, "")}),
                        "browser_implementation": options.stats.get("browser_implementation"),
                        "page_loaded": options.stats.get("page_loaded"),
                        "search_verified": options.stats.get("search_verified"),
                        "final_url": options.stats.get("final_url"),
                        "sort_applied": options.stats.get("sort_applied"),
                        "cookie_handled": options.stats.get("cookie_banner_declined"),
                    }
                )
                del raw, normalized
            except Exception as exc:
                if stop_event.is_set():
                    update_collection_run_status(run_id, "stopped_by_user", "Controlled cancellation test", backend="sqlite")
                    mode_payload.update(
                        {
                            "status": "stopped_by_user",
                            "stop_latency_target_seconds": args.cancel_after_seconds,
                            "page_loaded": options.stats.get("page_loaded"),
                            "search_verified": options.stats.get("search_verified"),
                            "sort_applied": options.stats.get("sort_applied"),
                            "hotel_cards_detected": options.stats.get("hotel_cards_detected"),
                            "browser_implementation": options.stats.get("browser_implementation"),
                        }
                    )
                else:
                    update_collection_run_status(run_id, "browser_crash" if "closed" in str(exc).lower() else "application_exception", str(exc), backend="sqlite")
                    mode_payload.update({"status": "failed", "error": str(exc), "error_type": type(exc).__name__})
            finally:
                if timer:
                    timer.cancel()
                cleanup = cleanup_owned_browser_processes(os.getpid(), log)
                after = guard.check(force=True)
                mode_payload.update(
                    {
                        "elapsed_seconds": round(time.perf_counter() - started, 2),
                        "memory_before": before.as_dict(),
                        "memory_after": after.as_dict(),
                        "resource_peaks": guard.peaks(),
                        "browser_cleanup": cleanup,
                    }
                )
            results_by_mode[mode] = mode_payload
            comparison_path.write_text(json.dumps(results_by_mode, indent=2, default=str), encoding="utf-8")
    finally:
        cleanup_owned_browser_processes(os.getpid(), log)
        lock.release()

    payload = {
        "destination": args.destination,
        "date": stay_date.isoformat(),
        "max_properties": args.max_properties,
        "sqlite_integrity": sqlite_integrity_check(),
        "modes": results_by_mode,
        "log": str(log_path),
    }
    comparison_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(comparison_path)
    return 0 if all(row.get("status") in {"completed", "stopped_by_user"} for row in results_by_mode.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
