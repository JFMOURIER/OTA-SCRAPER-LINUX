from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue as queue_module
import time
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the managed worker without a live browser.")
    parser.add_argument("--cancel-after", type=float)
    parser.add_argument("--max-hotels", type=int, default=2)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.max_hotels < 1 or args.max_hotels > 500:
        parser.error("--max-hotels must be between 1 and 500")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    data_dir = args.data_dir.resolve() if args.data_dir else BASE_DIR / "data" / "diagnostics" / f"worker_smoke_{stamp}"
    os.environ["INSTANCE_ID"] = f"worker_smoke_{stamp}"
    os.environ["INSTANCE_NAME"] = "Worker smoke"
    os.environ["INSTANCE_DATA_DIR"] = str(data_dir)
    os.environ["INSTANCE_PORT"] = "8598"
    os.environ["DB_BACKEND"] = "sqlite"

    import app
    from services.job_runner import CancellationSignal

    data_dir.mkdir(parents=True, exist_ok=bool(args.data_dir))
    app.INSTANCE_CONFIG.ensure_directories()
    config = app.CollectionConfig(
        source="Demo",
        city_or_region="Test City",
        checkin_start=date(2026, 8, 1),
        checkin_end=date(2026, 8, 1),
        nights=1,
        adults=2,
        currency="USD",
        max_hotels=args.max_hotels,
        headless=True,
        collect_all_available=False,
        max_scroll_minutes=1,
        selected_star_ratings=(1, 2, 3, 4, 5),
        include_unknown_star_rating=True,
        debug_mode=False,
        screenshots_enabled=False,
        fast_mode=False,
        performance_mode="balanced",
        block_images_and_fonts=False,
        test_mode=True,
        db_backend="sqlite",
        hotels_only=True,
        disable_filters_during_complete_collection=False,
        ultra_reliable_loading_mode=False,
        resume_previous_run=args.resume,
        auto_export_partial_excel=False,
        partial_export_frequency="every_25_dates",
    )
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    job_id = str(uuid4())
    signal = CancellationSignal(context.Event(), app.CANCEL_FILE, job_id)
    signal.reset()
    log_file = data_dir / "logs" / "worker_smoke.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)
    process = context.Process(
        target=app.run_background_job_with_fatal_guard,
        args=(app.run_resilient_collection_job, config, signal, output, job_id, str(log_file)),
        daemon=False,
    )
    process.start()
    if args.cancel_after is not None:
        time.sleep(max(0.0, args.cancel_after))
        signal.set("user")
    messages = []
    deadline = time.monotonic() + 30
    while process.is_alive() and time.monotonic() < deadline:
        try:
            messages.append(output.get(timeout=0.2))
        except queue_module.Empty:
            pass
    process.join(timeout=2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
        raise RuntimeError("Worker smoke timed out")
    while True:
        try:
            messages.append(output.get_nowait())
        except queue_module.Empty:
            break
    status_path = data_dir / "status" / "current_job_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    database_path = data_dir / "hotel_price_collector.sqlite"
    payload = {
        "exit_code": process.exitcode,
        "status": status.get("status"),
        "database": str(database_path),
        "database_exists": database_path.exists(),
        "messages": len(messages),
        "completion_messages": [
            {
                "kind": item[0],
                "status": (item[1] or {}).get("status") if isinstance(item[1], dict) else None,
                "result_count": len((item[1] or {}).get("results") or []) if isinstance(item[1], dict) else None,
                "error": (item[1] or {}).get("error") if isinstance(item[1], dict) else None,
            }
            for item in messages
            if item and item[0] in {"complete", "failed"}
        ],
    }
    print(json.dumps(payload, indent=2, default=str))
    expected = {"stopped_by_user_with_partial_results", "stopped_by_user"} if args.cancel_after is not None else {"completed_all_dates"}
    return 0 if process.exitcode == 0 and payload["status"] in expected and database_path.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
