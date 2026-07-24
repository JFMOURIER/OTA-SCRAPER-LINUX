from __future__ import annotations

import csv
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from services.scheduled_instances import (
    SCHEDULED_INSTANCES,
    ScheduledInstanceDefinition,
)


COMPLETE_RUN_STATUSES = ("completed", "completed_all_dates")
MERGED_METADATA_COLUMNS = [
    "collection_instance",
    "source_run_id",
    "collection_timestamp",
    "date_bucket",
]


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    path = unquote(parsed.path).lower().rstrip("/")
    path = re.sub(r"\.[a-z]{2}(?:-[a-z]{2})?(?=\.html$)", "", path)
    return f"{parsed.netloc.lower()}{path}"


def _hotel_identity(row: dict[str, Any]) -> str:
    ota_id = str(row.get("ota_hotel_id") or "").strip().lower()
    if ota_id:
        return f"ota:{ota_id}"
    hotel_url = str(row.get("hotel_url") or "").strip()
    if hotel_url:
        return f"url:{_canonical_url(hotel_url)}"
    name = " ".join(
        str(row.get("hotel_name") or "").strip().lower().split()
    )
    return f"name:{name}"


def _dedupe_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(
            row.get("source_website")
            or row.get("source")
            or ""
        ).strip().lower(),
        _hotel_identity(row),
        str(row.get("checkin_date") or ""),
        str(row.get("checkout_date") or ""),
        str(row.get("adults") or ""),
        str(row.get("currency") or "").strip().upper(),
    )


def _observation_recency(row: dict[str, Any]) -> tuple[str, int]:
    timestamp = str(
        row.get("collection_timestamp")
        or row.get("timestamp")
        or ""
    )
    run_text = str(
        row.get("source_run_id")
        or row.get("collection_run_id")
        or row.get("scrape_session_id")
        or "0"
    )
    try:
        run_id = int(run_text)
    except ValueError:
        run_id = 0
    return timestamp, run_id


def _latest_successful_export(
    definition: ScheduledInstanceDefinition,
) -> tuple[int, Path]:
    database_path = definition.data_dir / "hotel_price_collector.sqlite"
    if not database_path.is_file():
        raise FileNotFoundError(
            f"Instance database is missing: {database_path}"
        )
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT id, csv_file_path
            FROM collection_runs
            WHERE status IN (?, ?)
              AND csv_export_status = 'succeeded'
              AND csv_file_path IS NOT NULL
            ORDER BY coalesce(csv_exported_at, completed_at) DESC, id DESC
            LIMIT 1
            """,
            COMPLETE_RUN_STATUSES,
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(
            f"No successful complete CSV export exists for "
            f"{definition.instance_id}"
        )
    export_path = Path(str(row[1])).resolve()
    expected_root = (definition.data_dir / "exports").resolve()
    try:
        export_path.relative_to(expected_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Instance {definition.instance_id} export points outside its "
            f"own exports directory: {export_path}"
        ) from exc
    if not export_path.is_file():
        raise FileNotFoundError(
            f"Recorded export is missing: {export_path}"
        )
    return int(row[0]), export_path


def consolidate_latest_full_year(
    *,
    definitions: Iterable[ScheduledInstanceDefinition] | None = None,
    output_dir: str | Path = "/home/jf/Downloads",
    city: str = "Orlando",
    now: datetime | None = None,
) -> Path:
    selected = list(definitions or SCHEDULED_INSTANCES.values())
    if len(selected) != 3:
        raise ValueError("Consolidation requires exactly three date buckets")

    deduped: dict[tuple[str, ...], dict[str, Any]] = {}
    source_columns: list[str] = []
    for definition in selected:
        run_id, csv_path = _latest_successful_export(definition)
        with csv_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise RuntimeError(f"CSV has no header: {csv_path}")
            for column in reader.fieldnames:
                if column not in source_columns:
                    source_columns.append(column)
            for source_row in reader:
                row = dict(source_row)
                row.update(
                    {
                        "collection_instance": definition.instance_id,
                        "source_run_id": (
                            row.get("collection_run_id")
                            or row.get("scrape_session_id")
                            or run_id
                        ),
                        "collection_timestamp": row.get("timestamp") or "",
                        "date_bucket": definition.date_bucket,
                    }
                )
                key = _dedupe_key(row)
                existing = deduped.get(key)
                if existing is None or _observation_recency(
                    row
                ) >= _observation_recency(existing):
                    deduped[key] = row

    rows = sorted(
        deduped.values(),
        key=lambda row: (
            str(row.get("checkin_date") or ""),
            str(row.get("hotel_name") or "").lower(),
            str(row.get("collection_instance") or ""),
        ),
    )
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    output_path = (
        output_root
        / f"{city}_latest_full_year_{timestamp}.csv"
    )
    if output_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite consolidated output: {output_path}"
        )
    temporary = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    fieldnames = MERGED_METADATA_COLUMNS + [
        column
        for column in source_columns
        if column not in MERGED_METADATA_COLUMNS
    ]
    try:
        with temporary.open(
            "x",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path.resolve()
