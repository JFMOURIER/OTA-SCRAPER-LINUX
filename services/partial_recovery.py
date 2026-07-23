from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from database.db import (
    insert_hotel_results,
    result_date_counts_by_run_id,
)
from services.job_runner import update_checkpoint_date
from services.normalizer import normalize_hotel_result


REQUIRED_CORE_FIELDS = (
    "source",
    "city_or_region",
    "hotel_name",
    "checkin_date",
    "checkout_date",
)


@dataclass(frozen=True, slots=True)
class PartialInspection:
    stay_date: str
    json_path: str
    csv_path: str
    json_record_count: int
    csv_record_count: int
    required_core_fields_present: bool
    records_with_hotel_name: int
    records_with_raw_price: int
    records_with_parsed_price: int
    malformed_records: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PartialRecoveryResult:
    inspection: PartialInspection
    normalized_records: tuple[dict[str, Any], ...]
    database_rows_before: int
    database_rows_after: int
    dry_run: bool

    @property
    def recovered_records(self) -> int:
        return len(self.normalized_records)

    def to_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        payload = {
            "inspection": self.inspection.to_dict(),
            "recovered_records": self.recovered_records,
            "database_rows_before": self.database_rows_before,
            "database_rows_after": self.database_rows_after,
            "dry_run": self.dry_run,
        }
        if include_records:
            payload["normalized_records"] = list(self.normalized_records)
        return payload


def partial_paths(partial_dir: Path, stay_date: date) -> tuple[Path, Path]:
    stem = f"{stay_date.isoformat()}_partial_hotels"
    return partial_dir / f"{stem}.json", partial_dir / f"{stem}.csv"


def load_partial_json(path: Path) -> list[Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Partial JSON must contain a list: {path}")
    return payload


def inspect_partial_pair(partial_dir: Path, stay_date: date) -> PartialInspection:
    json_path, csv_path = partial_paths(partial_dir, stay_date)
    if not json_path.is_file():
        raise FileNotFoundError(f"Partial JSON not found: {json_path}")
    if not csv_path.is_file():
        raise FileNotFoundError(f"Partial CSV not found: {csv_path}")

    records = load_partial_json(json_path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        csv_count = sum(1 for _ in csv.DictReader(handle))

    malformed = 0
    names = 0
    raw_prices = 0
    parsed_prices = 0
    expected_date = stay_date.isoformat()
    for record in records:
        if not isinstance(record, dict):
            malformed += 1
            continue
        names += int(bool(str(record.get("hotel_name") or "").strip()))
        raw_prices += int(
            bool(str(record.get("raw_price_text") or record.get("price") or "").strip())
        )
        parsed_prices += int(record.get("parsed_price") not in (None, ""))
        missing_core = any(record.get(field) in (None, "") for field in REQUIRED_CORE_FIELDS)
        wrong_date = str(record.get("checkin_date") or "") != expected_date
        missing_identity = not (
            str(record.get("hotel_url") or "").strip()
            or str(record.get("hotel_name") or "").strip()
        )
        if missing_core or wrong_date or missing_identity:
            malformed += 1

    if csv_count != len(records):
        malformed += abs(csv_count - len(records))

    return PartialInspection(
        stay_date=expected_date,
        json_path=str(json_path.resolve()),
        csv_path=str(csv_path.resolve()),
        json_record_count=len(records),
        csv_record_count=csv_count,
        required_core_fields_present=malformed == 0,
        records_with_hotel_name=names,
        records_with_raw_price=raw_prices,
        records_with_parsed_price=parsed_prices,
        malformed_records=malformed,
    )


def is_duplicate_resource_status_failure(error: Any) -> bool:
    text = str(error or "")
    return (
        "update_status_file() got multiple values for keyword argument"
        in text
        and "available_ram_mb" in text
    )


def normalize_partial_records(
    records: list[Any],
    *,
    run_id: int,
    source: str,
    city_or_region: str,
    stay_date: date,
    checkout_date: date,
    number_of_nights: int,
    adults: int,
    currency: str,
) -> list[dict[str, Any]]:
    normalized_by_identity: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Partial record for {stay_date.isoformat()} is not an object")
        identity = str(record.get("hotel_url") or record.get("hotel_name") or "").strip().lower()
        if not identity:
            raise ValueError(f"Partial record for {stay_date.isoformat()} has no hotel identity")
        authoritative = dict(record)
        authoritative.update(
            {
                "collection_run_id": run_id,
                "source": source,
                "city_or_region": city_or_region,
                "checkin_date": stay_date,
                "checkout_date": checkout_date,
                "number_of_nights": number_of_nights,
                "adults": adults,
                "currency": currency,
                "collection_status": "success",
                "error_message": None,
            }
        )
        normalized_by_identity[identity] = normalize_hotel_result(authoritative)
    return list(normalized_by_identity.values())


def recover_partial_date(
    partial_dir: Path,
    *,
    stay_date: date,
    run_id: int,
    source: str,
    city_or_region: str,
    number_of_nights: int,
    adults: int,
    currency: str,
    backend: str = "sqlite",
    dry_run: bool = False,
) -> PartialRecoveryResult:
    inspection = inspect_partial_pair(partial_dir, stay_date)
    if inspection.malformed_records:
        raise ValueError(
            f"Refusing to recover {stay_date.isoformat()}: "
            f"{inspection.malformed_records} malformed or mismatched records"
        )
    if inspection.json_record_count == 0:
        raise ValueError(f"Refusing to recover empty partial data for {stay_date.isoformat()}")

    json_path, _ = partial_paths(partial_dir, stay_date)
    normalized = normalize_partial_records(
        load_partial_json(json_path),
        run_id=run_id,
        source=source,
        city_or_region=city_or_region,
        stay_date=stay_date,
        checkout_date=stay_date + timedelta(days=number_of_nights),
        number_of_nights=number_of_nights,
        adults=adults,
        currency=currency,
    )
    before = result_date_counts_by_run_id(run_id, backend=backend).get(
        stay_date.isoformat(), 0
    )
    if not dry_run:
        insert_hotel_results(normalized, backend=backend)
    after = result_date_counts_by_run_id(run_id, backend=backend).get(
        stay_date.isoformat(), 0
    )
    return PartialRecoveryResult(
        inspection=inspection,
        normalized_records=tuple(normalized),
        database_rows_before=before,
        database_rows_after=after,
        dry_run=dry_run,
    )


def recover_checkpoint_status_failures(
    checkpoint: dict[str, Any],
    *,
    planned_dates: list[date],
    partial_dir: Path,
    run_id: int,
    source: str,
    city_or_region: str,
    number_of_nights: int,
    adults: int,
    currency: str,
    backend: str = "sqlite",
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
    strict: bool = True,
) -> list[PartialRecoveryResult]:
    recovered: list[PartialRecoveryResult] = []
    date_statuses = checkpoint.get("date_statuses") or {}
    for stay_date in planned_dates:
        date_key = stay_date.isoformat()
        date_row = date_statuses.get(date_key)
        if not isinstance(date_row, dict):
            continue
        previously_recovered = (
            bool(date_row.get("recovered_from_partial"))
            and date_row.get("recovery_reason")
            == "nonessential_status_reporting_failure"
        )
        if not (
            is_duplicate_resource_status_failure(date_row.get("last_error"))
            or previously_recovered
        ):
            continue
        try:
            result = recover_partial_date(
                partial_dir,
                stay_date=stay_date,
                run_id=run_id,
                source=source,
                city_or_region=city_or_region,
                number_of_nights=number_of_nights,
                adults=adults,
                currency=currency,
                backend=backend,
                dry_run=dry_run,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if log:
                disposition = (
                    "recovery refused"
                    if strict
                    else "normal resume will continue with collection"
                )
                log(f"Partial recovery was not usable for {date_key}; {disposition}: {exc}")
            if strict:
                raise
            continue
        recovered.append(result)
        if log:
            action = "Validated" if dry_run else "Recovered"
            log(
                f"{action} {result.recovered_records} partial records for {date_key}; "
                f"SQLite date rows: {result.database_rows_before} -> "
                f"{result.database_rows_after}."
            )
        if dry_run:
            continue
        checkpoint = update_checkpoint_date(
            checkpoint,
            stay_date=stay_date,
            checkout_date=stay_date + timedelta(days=number_of_nights),
            status="completed",
            attempts=int(date_row.get("attempts") or 0),
            records_collected=result.database_rows_after,
            error=None,
            output_files={
                "partial_json": result.inspection.json_path,
                "partial_csv": result.inspection.csv_path,
                "saved_to_sqlite": True,
            },
        )
        recovered_row = checkpoint["date_statuses"][date_key]
        recovered_row["recovered_from_partial"] = True
        recovered_row["recovery_reason"] = "nonessential_status_reporting_failure"
    return recovered
