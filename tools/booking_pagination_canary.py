#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collectors.base import CollectorOptions
from collectors.booking_playwright import BookingPlaywrightCollector
from services.booking_pagination import hotel_identity
from services.job_runner import atomic_write_json, save_partial_records
from services.normalizer import normalize_hotel_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one isolated visible Booking offset-pagination canary."
    )
    parser.add_argument("--checkin", default="2026-09-10")
    parser.add_argument("--max-hotels", type=int, default=150)
    parser.add_argument("--city", default="Orlando")
    return parser.parse_args()


def owned_profile_processes(profile: Path) -> list[psutil.Process]:
    marker = str(profile.resolve())
    matches: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
            if marker in command:
                matches.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def wait_for_profile_processes(profile: Path, timeout: float = 8.0) -> list[psutil.Process]:
    deadline = time.monotonic() + timeout
    remaining = owned_profile_processes(profile)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining = owned_profile_processes(profile)
    return remaining


def main() -> int:
    args = parse_args()
    checkin = date.fromisoformat(args.checkin)
    if checkin <= date.today():
        raise SystemExit("--checkin must be a future date")
    checkout = checkin + timedelta(days=1)
    root = Path(
        tempfile.mkdtemp(
            prefix=f"ota-booking-pagination-canary-{datetime.now():%Y%m%d-%H%M%S}-",
            dir="/tmp",
        )
    )
    partial_dir = root / "partial"
    profile = root / "browser-profile"
    log_path = root / "canary.log"
    report_path = root / "canary-report.json"
    logs: list[str] = []

    def log(message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        logs.append(line)
        print(line, flush=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def save_partial(stay_date: date, records: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        saved = save_partial_records(partial_dir, stay_date, records)
        atomic_write_json(
            partial_dir / f"{stay_date.isoformat()}_partial_metadata.json",
            {**metadata, **saved},
        )

    options = CollectorOptions(
        collect_all_available=False,
        selected_star_ratings=(1, 2, 3, 4, 5),
        include_unknown_star_rating=True,
        debug_mode=False,
        screenshots_enabled=False,
        headless=False,
        fast_mode=False,
        performance_mode="balanced",
        block_images_and_fonts=False,
        hotels_only=True,
        screenshot_dir=root / "screenshots",
        screenshots_dir=root / "screenshots",
        debug_dir=root / "debug",
        checkpoint_dir=root / "checkpoints",
        status_dir=root / "status",
        exports_dir=root / "exports",
        log_dir=root / "logs",
        instance_data_dir=root,
        browser_profile_dir=profile,
        partial_dir=partial_dir,
        partial_results_callback=save_partial,
    )
    collector = BookingPlaywrightCollector()
    report: dict[str, Any] = {
        "root": str(root),
        "production_data_used": False,
        "city": args.city,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "adults": 2,
        "currency": "USD",
        "sort": "price",
        "max_hotels": args.max_hotels,
        "headless": False,
        "started_at": datetime.now().isoformat(),
    }
    exit_code = 1
    try:
        rows = collector.collect(
            args.city,
            checkin,
            checkout,
            2,
            "USD",
            args.max_hotels,
            False,
            options,
            lambda value, message: log(f"progress={value:.3f} {message}"),
            log,
        )
        identities = [hotel_identity(row) for row in rows]
        duplicates = len(identities) - len(set(identities))

        import database.db as database

        database_path = root / "canary.sqlite"
        original_database_path = database.SQLITE_DB_PATH
        try:
            database.SQLITE_DB_PATH = database_path
            database.init_db("sqlite")
            normalized = [
                normalize_hotel_result({**row, "collection_run_id": 1})
                for row in rows
            ]
            database.insert_hotel_results(normalized, backend="sqlite")
            database.insert_hotel_results(normalized, backend="sqlite")
        finally:
            database.SQLITE_DB_PATH = original_database_path
        with sqlite3.connect(database_path) as connection:
            database_rows = int(
                connection.execute(
                    "select count(*) from hotel_price_results where collection_run_id = 1"
                ).fetchone()[0]
            )
            unique_database_rows = int(
                connection.execute(
                    """
                    select count(*) from (
                        select checkin_date, lower(coalesce(hotel_url, hotel_name))
                        from hotel_price_results
                        where collection_run_id = 1
                        group by checkin_date, lower(coalesce(hotel_url, hotel_name))
                    )
                    """
                ).fetchone()[0]
            )

        offsets = list(options.stats.get("pagination_offsets_visited") or [])
        additions = list(options.stats.get("pagination_page_unique_additions") or [])
        page_counts = list(options.stats.get("pagination_page_card_counts") or [])
        completion = str(options.stats.get("completion_status") or "")
        passed = (
            len(offsets) >= 3
            and offsets[:3] == [0, 25, 50]
            and sum(1 for value in additions if int(value) > 0) >= 3
            and max(page_counts or [0]) <= 50
            and len(rows) >= 100
            and completion
            in {"completed_target_reached", "completed_results_exhausted"}
            and duplicates == 0
            and database_rows == unique_database_rows == len(rows)
        )
        report.update(
            {
                "status": "passed" if passed else "failed",
                "completion_status": completion,
                "offsets_visited": offsets,
                "page_card_counts": page_counts,
                "page_unique_additions": additions,
                "unique_records": len(rows),
                "duplicates": duplicates,
                "database_path": str(database_path),
                "database_rows": database_rows,
                "unique_database_rows": unique_database_rows,
                "peak_browser_rss_mb": options.stats.get("peak_browser_rss_mb"),
                "peak_browser_pss_mb": options.stats.get("peak_browser_pss_mb"),
                "minimum_available_ram_mb": options.stats.get(
                    "minimum_available_ram_mb"
                ),
                "maximum_live_dom_cards": max(page_counts or [0]),
                "dom_replaced_between_pages": bool(
                    options.stats.get("pagination_dom_replaced")
                ),
                "partials_path": str(partial_dir),
            }
        )
        exit_code = 0 if passed else 1
    except Exception as exc:
        report.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "stats": options.stats,
            }
        )
        log(f"CANARY FAILED: {type(exc).__name__}: {exc}")
    finally:
        remaining = wait_for_profile_processes(profile)
        report["owned_browser_processes_detected_after_collector_cleanup"] = [
            process.pid for process in remaining
        ]
        if remaining:
            for process in remaining:
                try:
                    process.terminate()
                except psutil.Error:
                    pass
            _gone, alive = psutil.wait_procs(remaining, timeout=4)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            exit_code = 1
        final_remaining = wait_for_profile_processes(profile, timeout=3)
        report["owned_browser_processes_after_cleanup"] = [
            process.pid for process in final_remaining
        ]
        report["orphan_browser_process_count"] = len(final_remaining)
        if final_remaining:
            exit_code = 1
        report["completed_at"] = datetime.now().isoformat()
        atomic_write_json(report_path, report)
        print(f"CANARY_ROOT={root}", flush=True)
        print(f"CANARY_REPORT={report_path}", flush=True)
        print(json.dumps(report, indent=2, default=str), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
