#!/usr/bin/env python3
"""Persist one verified local CSV in its instance Drive retry queue."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import db
from services.google_drive_sync import upload_run_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--instance",
        required=True,
        choices={
            "near_30_days",
            "medium_31_120_days",
            "long_121_365_days",
        },
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    database_path = data_dir / "hotel_price_collector.sqlite"
    csv_path = args.csv.resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)

    previous_database = db.SQLITE_DB_PATH
    try:
        db.SQLITE_DB_PATH = database_path
        run = db.fetch_collection_run_by_id(args.run_id, backend="sqlite")
        if run is None:
            raise ValueError(f"Run {args.run_id} does not exist")
        authoritative_rows = db.count_results_by_run_id(
            args.run_id,
            backend="sqlite",
        )
        run["authoritative_row_count"] = authoritative_rows
        run["csv_rows_exported"] = authoritative_rows
        state = upload_run_bundle(
            args.instance,
            run,
            data_dir=data_dir,
            csv_path=csv_path,
            upload_csv=True,
            upload_excel=False,
        )
    finally:
        db.SQLITE_DB_PATH = previous_database
    print(json.dumps(state, indent=2, sort_keys=True, default=str))
    return 0 if state["drive_upload_status"] in {"pending", "succeeded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
