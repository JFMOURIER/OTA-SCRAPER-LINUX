#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.consolidation import consolidate_latest_full_year


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read the latest successful CSV from each isolated instance and "
            "produce a deduplicated annual CSV."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="/home/jf/Downloads",
    )
    args = parser.parse_args()
    print(
        consolidate_latest_full_year(output_dir=args.output_dir)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
