from __future__ import annotations

from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from collectors.base import CollectorOptions


def main() -> int:
    base_dir = BASE_DIR
    CollectorOptions(
        collect_all_available=False,
        max_scroll_minutes=5,
        selected_star_ratings=(1, 2, 3, 4, 5),
        include_unknown_star_rating=True,
        debug_mode=True,
        screenshots_enabled=True,
        headless=True,
        fast_mode=False,
        performance_mode="balanced",
        block_images_and_fonts=False,
        stop_event=None,
        hotels_only=True,
        disable_filters_during_complete_collection=False,
        ultra_reliable_loading_mode=False,
        screenshot_dir=base_dir / "data" / "screenshots",
        screenshots_dir=base_dir / "data" / "screenshots",
        debug_dir=base_dir / "data" / "debug",
        checkpoint_dir=base_dir / "data" / "checkpoints",
        status_dir=base_dir / "data" / "status",
        exports_dir=base_dir / "data" / "exports",
        log_dir=base_dir / "data" / "logs",
        instance_data_dir=base_dir / "data",
        browser_profile_dir=base_dir / "data" / "browser_profile",
        partial_dir=base_dir / "data" / "partial",
        current_attempt=1,
    )
    print("CollectorOptions test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
