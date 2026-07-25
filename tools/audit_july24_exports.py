#!/usr/bin/env python3
"""Read-safe July 24 run audit with exact-run CSV recovery.

The source SQLite file is never copied directly. Every inspection uses SQLite's
online backup API. Recovery is opt-in and writes only new, timestamped files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")
TARGET_DATE = "2026-07-24"
DOWNLOADS = Path("/home/jf/Downloads")
DOWNLOAD_EXPORT_ROOT = DOWNLOADS / "OTA-SCRAPER-EXPORTS"
INSTANCE_DEFINITIONS = {
    "instance_1": {
        "port": 8501,
        "aliases": ("instance_1", "period_1", "near_30_days"),
        "drive_folder_id": "18kkxujFodzEfoOmhfpBuZI8xDzaoUk8b",
        "drive_destination": (
            "OTA - SCRAPED PRICES/OTA DATA/ORLANDO/ONE MONTH DATA"
        ),
    },
    "instance_2": {
        "port": 8502,
        "aliases": ("instance_2", "period_2", "medium_31_120_days"),
        "drive_folder_id": "1ATZztmP0pS9c0oprzF3LzQKYcpsXtT7S",
        "drive_destination": (
            "OTA - SCRAPED PRICES/OTA DATA/ORLANDO/"
            "NEXT THREE MONTHS DATA"
        ),
    },
    "instance_3": {
        "port": 8503,
        "aliases": ("instance_3", "period_3", "long_121_365_days"),
        "drive_folder_id": "19TNxbw6-ymHmUE2dkrSLyvKwYtjP4H5c",
        "drive_destination": (
            "OTA - SCRAPED PRICES/OTA DATA/ORLANDO/"
            "REST OF THE YEAR DATA"
        ),
    },
}
COMPLETE_STATUSES = {"completed", "completed_all_dates"}


@dataclass(frozen=True)
class CsvInspection:
    path: Path
    rows: int
    size: int
    sha256: str
    min_checkin: str | None
    max_checkin: str | None
    sources: tuple[str, ...]
    cities: tuple[str, ...]
    instances: tuple[str, ...]


def _slug(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return text.strip("._-").lower() or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _online_snapshot(source: Path, destination: Path) -> None:
    uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as source_connection:
        with sqlite3.connect(destination) as target_connection:
            source_connection.backup(target_connection, pages=2048)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f'pragma table_info("{table}")')
    ]


def _july_runs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = _tables(connection)
    if "collection_runs" not in tables:
        return []
    run_columns = set(_columns(connection, "collection_runs"))
    clauses = []
    parameters: list[Any] = []
    for column in ("started_at", "completed_at"):
        if column in run_columns:
            clauses.append(f"date({column}) = ?")
            parameters.append(TARGET_DATE)
    if (
        "hotel_price_results" in tables
        and "collection_run_id"
        in _columns(connection, "hotel_price_results")
    ):
        result_columns = set(_columns(connection, "hotel_price_results"))
        if "collected_at" in result_columns:
            clauses.append(
                "id in (select distinct collection_run_id "
                "from hotel_price_results where date(collected_at) = ?)"
            )
            parameters.append(TARGET_DATE)
    if not clauses:
        return []
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "select * from collection_runs where "
        + " or ".join(f"({clause})" for clause in clauses)
        + " order by id",
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def _result_summary(
    connection: sqlite3.Connection,
    run_id: int,
) -> dict[str, Any]:
    tables = _tables(connection)
    if "hotel_price_results" not in tables:
        return {"rows": 0}
    columns = set(_columns(connection, "hotel_price_results"))
    if "collection_run_id" not in columns:
        return {"rows": 0}
    selections = ["count(*) as rows"]
    for column, alias in (
        ("checkin_date", "min_checkin"),
        ("checkin_date", "max_checkin"),
        ("source", "result_source"),
        ("city_or_region", "result_city"),
    ):
        if alias == "min_checkin":
            selections.append(f"min({column}) as {alias}")
        elif alias == "max_checkin":
            selections.append(f"max({column}) as {alias}")
        else:
            selections.append(f"min({column}) as {alias}")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        f"select {', '.join(selections)} "
        "from hotel_price_results where collection_run_id = ?",
        (int(run_id),),
    ).fetchone()
    return dict(row) if row is not None else {"rows": 0}


def _csv_values(row: dict[str, str], names: Iterable[str]) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def inspect_csv(
    path: Path,
    cache: dict[Path, CsvInspection],
) -> CsvInspection:
    resolved = path.resolve()
    if resolved in cache:
        return cache[resolved]
    rows = 0
    checkins: list[str] = []
    sources: set[str] = set()
    cities: set[str] = set()
    instances: set[str] = set()
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            checkin = _csv_values(
                row,
                (
                    "checkin_date",
                    "requested_checkin_date",
                    "effective_checkin_date",
                ),
            )
            if checkin:
                checkins.append(checkin[:10])
            source = _csv_values(row, ("source",))
            city = _csv_values(row, ("city_or_region", "city"))
            instance = _csv_values(
                row,
                ("instance_id", "instance", "instance_name"),
            )
            if source:
                sources.add(source)
            if city:
                cities.add(city)
            if instance:
                instances.add(instance)
    inspection = CsvInspection(
        path=resolved,
        rows=rows,
        size=resolved.stat().st_size,
        sha256=_sha256(resolved),
        min_checkin=min(checkins) if checkins else None,
        max_checkin=max(checkins) if checkins else None,
        sources=tuple(sorted(sources)),
        cities=tuple(sorted(cities)),
        instances=tuple(sorted(instances)),
    )
    cache[resolved] = inspection
    return inspection


def _candidate_paths(
    run: dict[str, Any],
    *,
    data_dir: Path,
    logical_instance: str,
    aliases: tuple[str, ...],
    downloads_csvs: list[Path],
) -> list[Path]:
    rows: list[Path] = []
    for field in ("csv_file_path", "csv_downloads_path"):
        value = str(run.get(field) or "").strip()
        if value:
            rows.append(Path(value))
    run_id = int(run["id"])
    token = f"_run_{run_id}_"
    for root in (
        data_dir / "exports",
        ROOT / "data" / "merged_exports",
        DOWNLOAD_EXPORT_ROOT / logical_instance,
    ):
        if root.is_dir():
            rows.extend(
                path
                for path in root.rglob("*.csv")
                if token in path.name
            )
    for path in downloads_csvs:
        lower_path = str(path).casefold()
        if token in path.name and any(
            alias.casefold() in lower_path for alias in aliases
        ):
            rows.append(path)
    unique: dict[Path, None] = {}
    for path in rows:
        try:
            if path.is_file():
                unique[path.resolve()] = None
        except OSError:
            continue
    return list(unique)


def _best_csv(
    candidates: list[Path],
    *,
    expected_rows: int,
    source: str,
    city: str,
    cache: dict[Path, CsvInspection],
) -> tuple[CsvInspection | None, list[CsvInspection]]:
    inspections: list[CsvInspection] = []
    for path in candidates:
        try:
            inspections.append(inspect_csv(path, cache))
        except (OSError, UnicodeError, csv.Error):
            continue
    matching = [row for row in inspections if row.rows == expected_rows]
    if not matching:
        return None, inspections

    def score(row: CsvInspection) -> tuple[int, int, int]:
        source_match = not row.sources or source in row.sources
        city_match = not row.cities or city in row.cities
        local = str(ROOT / "data" / "instances") in str(row.path)
        return int(source_match), int(city_match), int(local)

    return sorted(matching, key=score, reverse=True)[0], inspections


def _atomic_copy_unique(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if (
            destination.stat().st_size == source.stat().st_size
            and _sha256(destination) == _sha256(source)
        ):
            return destination.resolve()
        raise FileExistsError(f"Refusing to overwrite {destination}")
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    with source.open("rb") as source_handle, temporary.open("xb") as target:
        shutil.copyfileobj(source_handle, target, 8 * 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, destination)
    return destination.resolve()


def _filename(
    run: dict[str, Any],
    *,
    logical_instance: str,
    status: str,
    timestamp: datetime,
) -> str:
    nights = max(1, int(run.get("number_of_nights") or 1))
    start = str(run.get("resolved_start_date") or run.get("checkin_date"))
    end = str(run.get("resolved_end_date") or "")
    if not end:
        checkout = date.fromisoformat(str(run["checkout_date"])[:10])
        end = (checkout - timedelta(days=nights)).isoformat()
    completeness = "final" if status in COMPLETE_STATUSES else "partial"
    return (
        f"ota_results_{_slug(run.get('source'))}_"
        f"{_slug(run.get('city_or_region'))}_{logical_instance}_"
        f"run_{int(run['id'])}_{start}_to_{end}_{completeness}_"
        f"{timestamp.strftime('%Y%m%d_%H%M%S')}.csv"
    )


def _allocate_paths(
    *,
    data_dir: Path,
    logical_instance: str,
    run: dict[str, Any],
    timestamp: datetime,
) -> tuple[Path, Path]:
    for offset in range(120):
        candidate_time = timestamp.replace(microsecond=0)
        if offset:
            candidate_time = datetime.fromtimestamp(
                candidate_time.timestamp() + offset,
                tz=PARIS,
            )
        name = _filename(
            run,
            logical_instance=logical_instance,
            status=str(run.get("status") or ""),
            timestamp=candidate_time,
        )
        local = data_dir / "exports" / name
        download = DOWNLOAD_EXPORT_ROOT / logical_instance / name
        if not local.exists() and not download.exists():
            return local, download
    raise FileExistsError("Could not allocate a unique audit export name")


def _export_snapshot_run(
    snapshot: Path,
    run: dict[str, Any],
    *,
    logical_instance: str,
    data_dir: Path,
) -> tuple[Path, Path]:
    timestamp = datetime.now(PARIS)
    local, download = _allocate_paths(
        data_dir=data_dir,
        logical_instance=logical_instance,
        run=run,
        timestamp=timestamp,
    )
    local.parent.mkdir(parents=True, exist_ok=True)
    temporary = local.with_name(
        f".{local.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    with sqlite3.connect(snapshot) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "select * from hotel_price_results "
            "where collection_run_id = ? order by id",
            (int(run["id"]),),
        )
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            columns = [str(item[0]) for item in cursor.description]
            writer.writerow(columns)
            while True:
                batch = cursor.fetchmany(2000)
                if not batch:
                    break
                writer.writerows(tuple(row[column] for column in columns) for row in batch)
            handle.flush()
            os.fsync(handle.fileno())
    os.replace(temporary, local)
    download = _atomic_copy_unique(local, download)
    return local.resolve(), download


def _read_supporting_state(
    data_dir: Path,
    run_id: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, path in (
        ("status_json", data_dir / "status" / "current_job_status.json"),
        ("checkpoint", data_dir / "checkpoints" / "current_run_resume.json"),
        (
            "drive_state",
            data_dir / "status" / "drive_uploads" / f"run_{run_id}.json",
        ),
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result[label] = {
                "path": str(path.resolve()),
                "payload": payload if isinstance(payload, dict) else {},
            }
        except (OSError, json.JSONDecodeError, TypeError):
            result[label] = {"path": None, "payload": {}}
    logs = sorted(
        (data_dir / "logs").glob("*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    result["run_log"] = str(logs[0].resolve()) if logs else None
    return result


def run_audit(*, recover: bool) -> tuple[Path, list[dict[str, Any]]]:
    downloads_csvs = (
        list(DOWNLOADS.rglob("*.csv")) if DOWNLOADS.is_dir() else []
    )
    csv_cache: dict[Path, CsvInspection] = {}
    report_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ota-july24-audit-") as temporary:
        snapshot_root = Path(temporary)
        for logical_instance, definition in INSTANCE_DEFINITIONS.items():
            for alias in definition["aliases"]:
                data_dir = ROOT / "data" / "instances" / alias
                database_candidates = [
                    data_dir / "hotel_price_collector.sqlite",
                    data_dir / "hotel_price_collector.db",
                ]
                for database in database_candidates:
                    if not database.is_file():
                        continue
                    snapshot = (
                        snapshot_root
                        / f"{logical_instance}_{alias}_{database.name}.sqlite"
                    )
                    _online_snapshot(database, snapshot)
                    with sqlite3.connect(snapshot) as connection:
                        runs = _july_runs(connection)
                        for run in runs:
                            summary = _result_summary(
                                connection,
                                int(run["id"]),
                            )
                            db_rows = int(summary.get("rows") or 0)
                            source = str(
                                run.get("source")
                                or summary.get("result_source")
                                or ""
                            )
                            city = str(
                                run.get("city_or_region")
                                or summary.get("result_city")
                                or ""
                            )
                            candidates = _candidate_paths(
                                run,
                                data_dir=data_dir,
                                logical_instance=logical_instance,
                                aliases=definition["aliases"],
                                downloads_csvs=downloads_csvs,
                            )
                            best, inspected = _best_csv(
                                candidates,
                                expected_rows=db_rows,
                                source=source,
                                city=city,
                                cache=csv_cache,
                            )
                            actions: list[str] = []
                            local_path: Path | None = (
                                best.path if best is not None else None
                            )
                            download_path: Path | None = None
                            if db_rows == 0:
                                actions.append("empty_export")
                            elif best is None and recover:
                                local_path, download_path = _export_snapshot_run(
                                    snapshot,
                                    run,
                                    logical_instance=logical_instance,
                                    data_dir=data_dir,
                                )
                                best = inspect_csv(local_path, csv_cache)
                                actions.append(
                                    "regenerated_from_sqlite_explicit_run_id"
                                )
                            elif best is not None and recover:
                                desired_dir = (
                                    DOWNLOAD_EXPORT_ROOT / logical_instance
                                )
                                existing_download = next(
                                    (
                                        item.path
                                        for item in inspected
                                        if item.path.parent.resolve()
                                        == desired_dir.resolve()
                                        and item.rows == db_rows
                                    ),
                                    None,
                                )
                                if existing_download is None:
                                    _, desired = _allocate_paths(
                                        data_dir=data_dir,
                                        logical_instance=logical_instance,
                                        run=run,
                                        timestamp=datetime.now(PARIS),
                                    )
                                    download_path = _atomic_copy_unique(
                                        best.path,
                                        desired,
                                    )
                                    actions.append(
                                        "copied_verified_export_to_required_downloads"
                                    )
                                else:
                                    download_path = existing_download
                            supporting = _read_supporting_state(
                                data_dir,
                                int(run["id"]),
                            )
                            drive = supporting["drive_state"]["payload"]
                            run_status = str(run.get("status") or "")
                            csv_rows = best.rows if best else 0
                            csv_status = (
                                "empty_export"
                                if db_rows == 0
                                else "verified"
                                if best and csv_rows == db_rows
                                else "missing_or_mismatched"
                            )
                            report_rows.append(
                                {
                                    "Instance": logical_instance,
                                    "Instance directory": alias,
                                    "Port": definition["port"],
                                    "Database path": str(database.resolve()),
                                    "Run ID": int(run["id"]),
                                    "Run status": run_status,
                                    "Run started": run.get("started_at"),
                                    "Run completed": run.get("completed_at"),
                                    "Start date": (
                                        run.get("resolved_start_date")
                                        or run.get("checkin_date")
                                    ),
                                    "End date": (
                                        run.get("resolved_end_date")
                                        or summary.get("max_checkin")
                                    ),
                                    "Database rows": db_rows,
                                    "CSV status": csv_status,
                                    "CSV rows": csv_rows,
                                    "CSV bytes": best.size if best else 0,
                                    "CSV SHA-256": (
                                        best.sha256 if best else None
                                    ),
                                    "CSV min check-in": (
                                        best.min_checkin if best else None
                                    ),
                                    "CSV max check-in": (
                                        best.max_checkin if best else None
                                    ),
                                    "Database min check-in": summary.get(
                                        "min_checkin"
                                    ),
                                    "Database max check-in": summary.get(
                                        "max_checkin"
                                    ),
                                    "Source": source,
                                    "City": city,
                                    "Local CSV path": (
                                        str(local_path) if local_path else None
                                    ),
                                    "Downloads CSV path": (
                                        str(download_path)
                                        if download_path
                                        else None
                                    ),
                                    "Associated CSVs": [
                                        str(item.path) for item in inspected
                                    ],
                                    "Recovery performed": (
                                        ", ".join(actions) if actions else "none"
                                    ),
                                    "Status JSON": supporting["status_json"][
                                        "path"
                                    ],
                                    "Checkpoint": supporting["checkpoint"][
                                        "path"
                                    ],
                                    "Run log": supporting["run_log"],
                                    "Export status": (
                                        run.get("csv_export_status")
                                        or csv_status
                                    ),
                                    "Google Drive destination": (
                                        definition["drive_destination"]
                                        + " ["
                                        + definition["drive_folder_id"]
                                        + "]"
                                    ),
                                    "Google Drive status": (
                                        drive.get("drive_upload_status")
                                        or run.get("drive_upload_status")
                                        or "not_recorded"
                                    ),
                                }
                            )

    generated_at = datetime.now(PARIS)
    for offset in range(120):
        timestamp = datetime.fromtimestamp(
            generated_at.timestamp() + offset,
            tz=PARIS,
        ).strftime("%H%M%S")
        report_path = (
            DOWNLOADS / f"ota_export_audit_20260725_{timestamp}.txt"
        )
        if not report_path.exists():
            break
    else:
        raise FileExistsError("Could not allocate a unique audit report name")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = report_path.with_name(
        f".{report_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    with temporary_report.open("x", encoding="utf-8") as handle:
        handle.write(
            "OTA export audit — July 24, 2026 — Europe/Paris\n"
            f"Generated: {datetime.now(PARIS).isoformat(timespec='seconds')}\n"
            f"Recovery enabled: {recover}\n"
            "SQLite inspection method: online backup API\n\n"
        )
        for row in report_rows:
            handle.write("-" * 88 + "\n")
            for key, value in row.items():
                handle.write(
                    f"{key}: "
                    + (
                        json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (list, dict))
                        else str(value)
                    )
                    + "\n"
                )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_report, report_path)
    return report_path.resolve(), report_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Create only new atomic CSV copies/recoveries when needed.",
    )
    args = parser.parse_args()
    report, rows = run_audit(recover=args.recover)
    mismatches = [
        row
        for row in rows
        if row["Database rows"] > 0
        and row["Database rows"] != row["CSV rows"]
    ]
    print(
        json.dumps(
            {
                "report": str(report),
                "runs": len(rows),
                "mismatches": len(mismatches),
                "rows": rows,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
