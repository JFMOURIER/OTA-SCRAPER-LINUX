#!/usr/bin/env python3
"""Deterministic 365-date endurance simulation with bounded fake browser batches."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.endurance import (
    IncrementalResultBuffer,
    cleanup_ephemeral_profile,
    prepare_ephemeral_profile,
)
from services.job_runner import atomic_write_json


class FakeBrowser:
    def __init__(self, profile: Path) -> None:
        self.profile = profile
        self.closed = False
        prepare_ephemeral_profile(self.profile)
        (self.profile / "cache.bin").write_bytes(b"x" * 1024)

    def close(self) -> None:
        self.closed = True
        if not cleanup_ephemeral_profile(self.profile):
            raise RuntimeError(f"fake ephemeral profile was not removed: {self.profile}")


class FakeCollector:
    def collect(self, day_index: int, buffer: IncrementalResultBuffer) -> None:
        for batch in range(4):
            start = batch * 25
            rows = [
                {
                    "hotel_url": f"https://booking.test/hotel-{hotel}.html?day={day_index}",
                    "hotel_name": f"Hotel {hotel}",
                    "price": 100 + hotel + day_index,
                }
                for hotel in range(start, start + 25)
            ]
            if batch:
                rows.append(
                    {
                        "hotel_url": f"https://booking.test/hotel-{start - 1}.html?duplicate=1",
                        "hotel_name": f"Hotel {start - 1}",
                        "price": 100 + start - 1 + day_index,
                    }
                )
            buffer.add(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    started = time.time()
    process = psutil.Process()
    baseline_children = {child.pid for child in process.children(recursive=True)}
    milestones = {1, 10, 25, 50, 75, 100, 200, 365}
    memory: dict[str, float] = {}
    maximum_buffer = 0
    maximum_profile_bytes = 0
    all_profiles_bounded = True

    with tempfile.TemporaryDirectory(prefix="ota-endurance-365-") as directory:
        root = Path(directory)
        database = root / "endurance.sqlite"
        checkpoint = root / "checkpoint.json"
        export = root / "final.csv"
        conn = sqlite3.connect(database)
        conn.execute(
            """
            create table results (
                scrape_date text not null,
                hotel_url text not null,
                hotel_name text,
                price real,
                primary key(scrape_date, hotel_url)
            )
            """
        )
        collector = FakeCollector()
        completed: list[str] = []
        first = date(2026, 1, 1)
        for index in range(1, 366):
            scrape_date = first + timedelta(days=index - 1)
            profile = root / "profiles" / scrape_date.isoformat() / "attempt_1"
            browser = FakeBrowser(profile)
            buffer = IncrementalResultBuffer(100)
            collector.collect(index, buffer)
            maximum_buffer = max(maximum_buffer, len(buffer))
            rows = buffer.values()
            with conn:
                conn.executemany(
                    """
                    insert into results(scrape_date, hotel_url, hotel_name, price)
                    values(?,?,?,?)
                    on conflict(scrape_date, hotel_url) do update set
                        hotel_name=excluded.hotel_name,
                        price=excluded.price
                    """,
                    [
                        (
                            scrape_date.isoformat(),
                            str(row["hotel_url"]).split("?", 1)[0],
                            row["hotel_name"],
                            row["price"],
                        )
                        for row in rows
                    ],
                )
            completed.append(scrape_date.isoformat())
            atomic_write_json(
                checkpoint,
                {
                    "status": "running" if index < 365 else "completed",
                    "completed_dates": completed,
                    "last_completed_date": scrape_date.isoformat(),
                },
            )
            profile_bytes = sum(path.stat().st_size for path in profile.rglob("*") if path.is_file())
            maximum_profile_bytes = max(maximum_profile_bytes, profile_bytes)
            all_profiles_bounded = all_profiles_bounded and profile_bytes <= 2048
            browser.close()
            del rows, buffer, browser
            if index in milestones:
                memory[str(index)] = round(process.memory_info().rss / (1024 * 1024), 1)

        row_count = conn.execute("select count(*) from results").fetchone()[0]
        distinct_dates = conn.execute("select count(distinct scrape_date) from results").fetchone()[0]
        duplicate_count = conn.execute(
            "select count(*) from (select scrape_date,hotel_url,count(*) c from results group by 1,2 having c>1)"
        ).fetchone()[0]
        with export.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["scrape_date", "hotel_url", "hotel_name", "price"])
            for row in conn.execute(
                "select scrape_date,hotel_url,hotel_name,price from results order by scrape_date,hotel_url"
            ):
                writer.writerow(row)
        with export.open("r", encoding="utf-8", newline="") as handle:
            csv_rows = max(0, sum(1 for _ in csv.reader(handle)) - 1)
        final_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
        remaining_profile_files = sum(
            1 for path in (root / "profiles").rglob("*") if path.is_file()
        )
        remaining_profile_entries = sum(1 for _ in (root / "profiles").rglob("*"))
        conn.close()
        after_children = {child.pid for child in process.children(recursive=True)}
        rss_values = list(memory.values())
        report = {
            "status": "passed",
            "dates_completed": distinct_dates,
            "expected_rows": 36500,
            "sqlite_rows": row_count,
            "csv_rows": csv_rows,
            "duplicate_rows": duplicate_count,
            "maximum_date_buffer_records": maximum_buffer,
            "rss_mb_by_date": memory,
            "rss_growth_mb": round(max(rss_values) - min(rss_values), 1),
            "baseline_child_pids": sorted(baseline_children),
            "final_child_pids": sorted(after_children),
            "stable_process_count": after_children == baseline_children,
            "checkpoint_status": final_checkpoint.get("status"),
            "checkpoint_completed_dates": len(final_checkpoint.get("completed_dates", [])),
            "maximum_profile_bytes": maximum_profile_bytes,
            "all_profiles_bounded": all_profiles_bounded,
            "remaining_profile_files": remaining_profile_files,
            "remaining_profile_entries": remaining_profile_entries,
            "elapsed_seconds": round(time.time() - started, 2),
        }
        passed = (
            row_count == 36500
            and csv_rows == row_count
            and duplicate_count == 0
            and distinct_dates == 365
            and maximum_buffer == 100
            and report["stable_process_count"]
            and report["checkpoint_status"] == "completed"
            and report["checkpoint_completed_dates"] == 365
            and all_profiles_bounded
            and remaining_profile_files == 0
            and remaining_profile_entries == 0
            and report["rss_growth_mb"] < 40
        )
        report["status"] = "passed" if passed else "failed"
    output = args.output or Path("/tmp") / f"ota_endurance_365_{time.strftime('%Y%m%d_%H%M%S')}.json"
    atomic_write_json(output, report)
    print(json.dumps({**report, "report_path": str(output)}, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
