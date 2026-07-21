from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only integrity check and streamed recovery export.")
    parser.add_argument("database", nargs="?", default="data/instances/instance_1/hotel_price_collector.sqlite")
    parser.add_argument("--output")
    args = parser.parse_args()
    database = Path(args.database)
    if not database.is_absolute():
        database = BASE_DIR / database
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}")
    output = Path(args.output) if args.output else BASE_DIR / "data" / "recovery_exports" / f"recovered_{database.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    if not output.is_absolute():
        output = BASE_DIR / output
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing recovery export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{uuid4().hex}.tmp")
    uri = f"file:{database.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = str(conn.execute("pragma integrity_check").fetchone()[0])
        cursor = conn.execute("select * from hotel_price_results order by id")
        columns = [item[0] for item in cursor.description]
        rows_written = 0
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                writer.writerows(rows)
                rows_written += len(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
    finally:
        conn.close()
    print(f"database={database.resolve()}")
    print(f"integrity={integrity}")
    print(f"rows_exported={rows_written}")
    print(f"output={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
