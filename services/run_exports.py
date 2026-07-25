from __future__ import annotations

import csv
import fcntl
import hashlib
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import database.db as db
import openpyxl

from services.exporter import (
    export_sqlite_run_to_csv,
    export_sqlite_run_to_excel,
    safe_filename,
)
from services.instance_config import INSTANCE_CONFIG


TERMINAL_RUN_STATUSES = {
    "completed",
    "completed_all_dates",
    "completed_with_failed_dates",
    "completed_with_blocked_dates",
    "completed_with_partial_results",
    "completed_with_zero_results",
    "completed_incomplete",
    "blocked_or_access_restricted",
    "stopped",
    "stopped_by_user",
    "stopped_by_user_with_partial_results",
    "stopped_resource_limit",
    "browser_crash",
    "application_exception",
    "fatal_error_with_partial_results",
    "failed",
    "fatal_startup_error",
    "fatal_config_error",
}

COMPLETE_RUN_STATUSES = {
    "completed",
    "completed_all_dates",
}


def _publish_local_export_status(
    instance_id: str,
    database_path: Path,
    *,
    status: str,
    path: str | Path | None,
    rows: int,
) -> None:
    try:
        from services.operational_status import update_operational_status

        resolved_path = Path(path).resolve() if path else None
        update_operational_status(
            instance_id,
            data_dir=database_path.parent,
            local_export_status=status,
            local_export_path=str(resolved_path) if resolved_path else None,
            local_export_rows=int(rows),
            local_export_bytes=(
                resolved_path.stat().st_size
                if resolved_path and resolved_path.is_file()
                else 0
            ),
        )
    except Exception:
        pass


def _truthy_environment(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _run_date_range(run: dict[str, Any]) -> tuple[str, str]:
    start = date.fromisoformat(
        str(run.get("resolved_start_date") or run["checkin_date"])
    )
    if run.get("resolved_end_date"):
        return start.isoformat(), date.fromisoformat(
            str(run["resolved_end_date"])
        ).isoformat()
    checkout_boundary = date.fromisoformat(str(run["checkout_date"]))
    nights = max(1, int(run.get("number_of_nights") or 1))
    end = checkout_boundary - timedelta(days=nights)
    return start.isoformat(), end.isoformat()


def build_automatic_csv_filename(
    run: dict[str, Any],
    *,
    instance_id: str,
    exported_at: datetime,
) -> str:
    start, end = _run_date_range(run)
    source = safe_filename(
        str(run.get("source") or "source").lower().replace(".com", "")
    )
    city = safe_filename(str(run.get("city_or_region") or "city").lower())
    instance = safe_filename(str(instance_id or "default").lower())
    completeness = (
        "final" if str(run.get("status") or "") in COMPLETE_RUN_STATUSES else "partial"
    )
    return (
        f"ota_results_{source}_{city}_{instance}_run_{int(run['id'])}_"
        f"{start}_to_{end}_{completeness}_{exported_at.strftime('%Y%m%d_%H%M%S')}.csv"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _csv_data_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def _excel_data_rows(path: Path) -> int:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook["All Hotel Results"]
        return max(
            0,
            sum(1 for _ in worksheet.iter_rows(values_only=True)) - 1,
        )
    finally:
        workbook.close()


def _atomic_copy_no_overwrite(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if (
            destination.stat().st_size == source.stat().st_size
            and _sha256(destination) == _sha256(source)
        ):
            return destination.resolve()
        raise FileExistsError(
            f"Refusing to overwrite different existing file: {destination}"
        )
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target:
            shutil.copyfileobj(source_handle, target, 8 * 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


def _resolved_instance_id(database_path: Path, instance_id: str | None) -> str:
    if instance_id:
        return instance_id
    try:
        if database_path.parent.resolve() == INSTANCE_CONFIG.data_dir.resolve():
            return INSTANCE_CONFIG.instance_id
    except OSError:
        pass
    return database_path.parent.name or INSTANCE_CONFIG.instance_id


def _copy_to_downloads_by_default(database_path: Path) -> bool:
    if not _truthy_environment("OTA_AUTO_EXPORT_DOWNLOADS", True):
        return False
    if not INSTANCE_CONFIG.active:
        return False
    try:
        return database_path.parent.resolve() == INSTANCE_CONFIG.data_dir.resolve()
    except OSError:
        return False


def _payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "csv_export_status": run.get("csv_export_status"),
        "csv_file_path": run.get("csv_file_path"),
        "csv_downloads_path": run.get("csv_downloads_path"),
        "csv_rows_exported": int(run.get("csv_rows_exported") or 0),
        "csv_exported_at": run.get("csv_exported_at"),
        "csv_export_error": run.get("csv_export_error"),
        "excel_export_status": run.get("excel_export_status"),
        "excel_file_path": run.get("excel_file_path"),
        "excel_downloads_path": run.get("excel_downloads_path"),
        "excel_rows_exported": int(run.get("excel_rows_exported") or 0),
        "excel_exported_at": run.get("excel_exported_at"),
        "excel_export_error": run.get("excel_export_error"),
        "drive_upload_status": run.get("drive_upload_status"),
        "drive_upload_error": run.get("drive_upload_error"),
    }


def _successful_export_is_reusable(
    run: dict[str, Any],
    *,
    expected_rows: int,
    require_downloads: bool,
    downloads_dir: Path | None = None,
) -> bool:
    if run.get("csv_export_status") != "succeeded":
        return False
    if int(run.get("csv_rows_exported") or -1) != expected_rows:
        return False
    instance_path = Path(str(run.get("csv_file_path") or ""))
    if not instance_path.is_file():
        return False
    if require_downloads:
        downloads_path = Path(str(run.get("csv_downloads_path") or ""))
        if not downloads_path.is_file():
            return False
        if (
            downloads_dir is not None
            and downloads_path.parent.resolve() != downloads_dir.resolve()
        ):
            return False
    return True


def _production_downloads_dir(instance_id: str) -> Path:
    aliases = {
        "near_30_days": "instance_1",
        "period_1": "instance_1",
        "instance_1": "instance_1",
        "medium_31_120_days": "instance_2",
        "period_2": "instance_2",
        "instance_2": "instance_2",
        "long_121_365_days": "instance_3",
        "period_3": "instance_3",
        "instance_3": "instance_3",
    }
    root = Path(os.getenv("OTA_DOWNLOADS_DIR", "/home/jf/Downloads"))
    return (
        root
        / "OTA-SCRAPER-EXPORTS"
        / aliases.get(instance_id, safe_filename(instance_id))
    )


def _next_available_paths(
    *,
    run: dict[str, Any],
    instance_id: str,
    export_dir: Path,
    downloads_dir: Path | None,
) -> tuple[datetime, Path, Path | None]:
    candidate_time = datetime.now()
    for offset in range(0, 120):
        exported_at = candidate_time + timedelta(seconds=offset)
        filename = build_automatic_csv_filename(
            run,
            instance_id=instance_id,
            exported_at=exported_at,
        )
        instance_path = export_dir / filename
        downloads_path = downloads_dir / filename if downloads_dir else None
        if instance_path.exists():
            continue
        if downloads_path is not None and downloads_path.exists():
            continue
        return exported_at, instance_path, downloads_path
    raise FileExistsError("Could not allocate a unique timestamped CSV filename")


def automatic_export_run_csv(
    run_id: int,
    *,
    database_path: str | Path | None = None,
    instance_id: str | None = None,
    export_dir: str | Path | None = None,
    downloads_dir: str | Path | None = None,
    copy_to_downloads: bool | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Export finalized SQLite rows exactly once for a terminal run."""

    database_path = Path(database_path or db.SQLITE_DB_PATH).resolve()
    resolved_instance = _resolved_instance_id(database_path, instance_id)
    resolved_export_dir = Path(export_dir or database_path.parent / "exports")
    if copy_to_downloads is None:
        copy_to_downloads = _copy_to_downloads_by_default(database_path)
    if downloads_dir is None and copy_to_downloads:
        downloads_dir = _production_downloads_dir(resolved_instance)
    resolved_downloads_dir = Path(downloads_dir) if downloads_dir else None
    lock_path = database_path.parent / "status" / f"csv_export_run_{run_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            run = db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
            payload = _payload(run)
            payload["csv_export_status"] = (
                payload.get("csv_export_status") or "in_progress"
            )
            return payload

        run = db.fetch_collection_run_by_id(run_id, backend="sqlite")
        if run is None:
            raise ValueError(f"SQLite run {run_id} does not exist")
        status = str(run.get("status") or "")
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                f"Run {run_id} is not terminal; current status is {status!r}"
            )
        expected_rows = db.count_results_by_run_id(run_id, backend="sqlite")
        if _successful_export_is_reusable(
            run,
            expected_rows=expected_rows,
            require_downloads=bool(copy_to_downloads),
            downloads_dir=resolved_downloads_dir,
        ):
            if log:
                log(
                    f"Reusing successful automatic CSV export for run {run_id}: "
                    f"{run.get('csv_file_path')}"
                )
            payload = _payload(run)
            _publish_local_export_status(
                resolved_instance,
                database_path,
                status="succeeded",
                path=payload.get("csv_file_path"),
                rows=expected_rows,
            )
            return payload

        if expected_rows == 0:
            db.update_collection_run_csv_export(
                run_id,
                status="empty_export",
                csv_file_path=None,
                csv_downloads_path=None,
                rows_exported=0,
                exported_at=datetime.now(),
                error=None,
                backend="sqlite",
            )
            payload = _payload(
                db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
            )
            _publish_local_export_status(
                resolved_instance,
                database_path,
                status="empty_export",
                path=None,
                rows=0,
            )
            return payload

        prior_instance_path = Path(str(run.get("csv_file_path") or ""))
        can_reuse_prior = (
            run.get("csv_export_status") in {"failed", "succeeded"}
            and prior_instance_path.is_file()
            and _csv_data_rows(prior_instance_path) == expected_rows
        )
        exported_at: datetime
        instance_path: Path
        downloads_path: Path | None
        if can_reuse_prior:
            instance_path = prior_instance_path
            exported_at = datetime.fromisoformat(
                str(run.get("csv_exported_at") or datetime.now().isoformat())
            )
            downloads_path = (
                resolved_downloads_dir / instance_path.name
                if resolved_downloads_dir
                else None
            )
        else:
            exported_at, instance_path, downloads_path = _next_available_paths(
                run=run,
                instance_id=resolved_instance,
                export_dir=resolved_export_dir,
                downloads_dir=resolved_downloads_dir,
            )

        db.update_collection_run_csv_export(
            run_id,
            status="in_progress",
            csv_file_path=str(instance_path.resolve())
            if instance_path.exists()
            else str(instance_path),
            csv_downloads_path=str(downloads_path)
            if downloads_path is not None
            else None,
            rows_exported=expected_rows,
            exported_at=exported_at,
            error=None,
            backend="sqlite",
        )
        try:
            if not can_reuse_prior:
                summary = {
                    "source": run.get("source"),
                    "city_or_region": run.get("city_or_region"),
                    "checkin_date": run.get("checkin_date"),
                    "checkout_date": run.get("checkout_date"),
                    "number_of_nights": run.get("number_of_nights"),
                    "run_id": run_id,
                    "instance_id": resolved_instance,
                }
                instance_path = export_sqlite_run_to_csv(
                    run_id,
                    summary,
                    resolved_export_dir,
                    filename=instance_path.name,
                    instance_id=resolved_instance,
                )
            if downloads_path is not None:
                downloads_path = _atomic_copy_no_overwrite(
                    instance_path,
                    downloads_path,
                )
            db.update_collection_run_csv_export(
                run_id,
                status="succeeded",
                csv_file_path=str(instance_path.resolve()),
                csv_downloads_path=str(downloads_path)
                if downloads_path is not None
                else None,
                rows_exported=expected_rows,
                exported_at=exported_at,
                error=None,
                backend="sqlite",
            )
            if log:
                log(
                    f"Automatic run {run_id} CSV exported atomically with "
                    f"{expected_rows} rows: {instance_path}"
                )
            _publish_local_export_status(
                resolved_instance,
                database_path,
                status="succeeded",
                path=instance_path,
                rows=expected_rows,
            )
        except Exception as exc:
            db.update_collection_run_csv_export(
                run_id,
                status="failed",
                csv_file_path=str(instance_path.resolve())
                if instance_path.exists()
                else None,
                csv_downloads_path=str(downloads_path)
                if downloads_path is not None and downloads_path.exists()
                else None,
                rows_exported=expected_rows,
                exported_at=exported_at,
                error=str(exc),
                backend="sqlite",
            )
            if log:
                log(f"Automatic run {run_id} CSV export failed: {exc}")
            _publish_local_export_status(
                resolved_instance,
                database_path,
                status="failed",
                path=instance_path if instance_path.exists() else None,
                rows=expected_rows,
            )
        return _payload(
            db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
        )


def automatic_export_run_excel(
    run_id: int,
    *,
    database_path: str | Path | None = None,
    instance_id: str | None = None,
    export_dir: str | Path | None = None,
    downloads_dir: str | Path | None = None,
    copy_to_downloads: bool | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    database_path = Path(database_path or db.SQLITE_DB_PATH).resolve()
    resolved_instance = _resolved_instance_id(database_path, instance_id)
    resolved_export_dir = Path(export_dir or database_path.parent / "exports")
    if copy_to_downloads is None:
        copy_to_downloads = _copy_to_downloads_by_default(database_path)
    if downloads_dir is None and copy_to_downloads:
        downloads_dir = _production_downloads_dir(resolved_instance)
    resolved_downloads_dir = Path(downloads_dir) if downloads_dir else None
    lock_path = (
        database_path.parent / "status" / f"excel_export_run_{run_id}.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            run = db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
            payload = _payload(run)
            payload["excel_export_status"] = (
                payload.get("excel_export_status") or "in_progress"
            )
            return payload
        run = db.fetch_collection_run_by_id(run_id, backend="sqlite")
        if run is None:
            raise ValueError(f"SQLite run {run_id} does not exist")
        status = str(run.get("status") or "")
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                f"Run {run_id} is not terminal; current status is {status!r}"
            )
        expected_rows = db.count_results_by_run_id(run_id, backend="sqlite")
        prior_path = Path(str(run.get("excel_file_path") or ""))
        prior_download = Path(str(run.get("excel_downloads_path") or ""))
        reusable = (
            run.get("excel_export_status") == "succeeded"
            and int(run.get("excel_rows_exported") or -1) == expected_rows
            and prior_path.is_file()
            and _excel_data_rows(prior_path) == expected_rows
            and (
                not copy_to_downloads
                or (
                    prior_download.is_file()
                    and _sha256(prior_download) == _sha256(prior_path)
                )
            )
        )
        if reusable:
            return _payload(run)
        if expected_rows == 0:
            db.update_collection_run_excel_export(
                run_id,
                status="skipped_no_rows",
                excel_file_path=None,
                excel_downloads_path=None,
                rows_exported=0,
                exported_at=datetime.now(),
                error=None,
                backend="sqlite",
            )
            return _payload(
                db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
            )
        csv_path = Path(str(run.get("csv_file_path") or ""))
        if csv_path.name.endswith(".csv"):
            filename = csv_path.with_suffix(".xlsx").name
            exported_at = datetime.fromisoformat(
                str(run.get("csv_exported_at") or datetime.now().isoformat())
            )
        else:
            exported_at = datetime.now()
            filename = build_automatic_csv_filename(
                run,
                instance_id=resolved_instance,
                exported_at=exported_at,
            ).replace(".csv", ".xlsx")
        instance_path = resolved_export_dir / filename
        downloads_path = (
            resolved_downloads_dir / filename
            if resolved_downloads_dir is not None
            else None
        )
        can_reuse_existing = (
            instance_path.is_file()
            and _excel_data_rows(instance_path) == expected_rows
        )
        db.update_collection_run_excel_export(
            run_id,
            status="in_progress",
            excel_file_path=str(instance_path),
            excel_downloads_path=(
                str(downloads_path) if downloads_path else None
            ),
            rows_exported=expected_rows,
            exported_at=exported_at,
            error=None,
            backend="sqlite",
        )
        try:
            if not can_reuse_existing:
                start, end = _run_date_range(run)
                summary = {
                    "run ID": run_id,
                    "instance ID": resolved_instance,
                    "run status": status,
                    "source": run.get("source"),
                    "city_or_region": run.get("city_or_region"),
                    "resolved start date": start,
                    "resolved end date": end,
                    "date mode": run.get("date_mode") or "manual",
                    "schedule slot": run.get("schedule_slot"),
                    "number_of_nights": run.get("number_of_nights"),
                    "adults": run.get("adults"),
                    "currency": run.get("currency"),
                    "collection started at": run.get("started_at"),
                    "collection completed at": run.get("completed_at"),
                    "SQLite row count": expected_rows,
                }
                instance_path = export_sqlite_run_to_excel(
                    run_id,
                    summary,
                    resolved_export_dir,
                    filename=filename,
                    overwrite_existing=False,
                )
            actual_rows = _excel_data_rows(instance_path)
            if actual_rows != expected_rows:
                raise RuntimeError(
                    f"Excel rows ({actual_rows}) do not match SQLite rows "
                    f"({expected_rows}) for run {run_id}"
                )
            if downloads_path is not None:
                downloads_path = _atomic_copy_no_overwrite(
                    instance_path,
                    downloads_path,
                )
            db.update_collection_run_excel_export(
                run_id,
                status="succeeded",
                excel_file_path=str(instance_path.resolve()),
                excel_downloads_path=(
                    str(downloads_path) if downloads_path else None
                ),
                rows_exported=expected_rows,
                exported_at=exported_at,
                error=None,
                backend="sqlite",
            )
            if log:
                log(
                    f"Automatic run {run_id} Excel exported atomically with "
                    f"{expected_rows} rows: {instance_path}"
                )
        except Exception as exc:
            db.update_collection_run_excel_export(
                run_id,
                status="failed",
                excel_file_path=(
                    str(instance_path.resolve())
                    if instance_path.exists()
                    else None
                ),
                excel_downloads_path=(
                    str(downloads_path)
                    if downloads_path is not None and downloads_path.exists()
                    else None
                ),
                rows_exported=expected_rows,
                exported_at=exported_at,
                error=str(exc),
                backend="sqlite",
            )
            if log:
                log(f"Automatic run {run_id} Excel export failed: {exc}")
        return _payload(
            db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
        )


def automatic_export_run_bundle(
    run_id: int,
    *,
    database_path: str | Path | None = None,
    instance_id: str | None = None,
    export_dir: str | Path | None = None,
    downloads_dir: str | Path | None = None,
    copy_to_downloads: bool | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Publish complete local CSV/Excel artifacts before optional Drive upload."""

    database_path = Path(database_path or db.SQLITE_DB_PATH).resolve()
    resolved_instance = _resolved_instance_id(database_path, instance_id)
    resolved_export_dir = Path(export_dir or database_path.parent / "exports")
    lock_path = (
        database_path.parent / "status" / f"bundle_export_run_{run_id}.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            run = db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
            return _payload(run)
        csv_payload = automatic_export_run_csv(
            run_id,
            database_path=database_path,
            instance_id=resolved_instance,
            export_dir=resolved_export_dir,
            downloads_dir=downloads_dir,
            copy_to_downloads=copy_to_downloads,
            log=log,
        )
        excel_payload = automatic_export_run_excel(
            run_id,
            database_path=database_path,
            instance_id=resolved_instance,
            export_dir=resolved_export_dir,
            downloads_dir=downloads_dir,
            copy_to_downloads=copy_to_downloads,
            log=log,
        )
        run = db.fetch_collection_run_by_id(run_id, backend="sqlite") or {}
        expected_rows = db.count_results_by_run_id(run_id, backend="sqlite")
        run["authoritative_row_count"] = expected_rows
        local_success = (
            csv_payload.get("csv_export_status") == "succeeded"
            and int(csv_payload.get("csv_rows_exported") or -1)
            == expected_rows
        )
        try:
            from services.schedule_config import (
                load_schedule,
                record_schedule_event,
            )

            schedule = load_schedule(
                resolved_instance,
                data_dir=database_path.parent,
            )
            record_schedule_event(
                resolved_instance,
                (
                    "local_export_succeeded"
                    if local_success
                    else "local_export_failed"
                ),
                data_dir=database_path.parent,
                run_id=run_id,
                local_export_status=(
                    "succeeded" if local_success else "failed"
                ),
            )
            if local_success and schedule["drive_upload_enabled"]:
                from services.google_drive_sync import upload_run_bundle

                drive = upload_run_bundle(
                    resolved_instance,
                    run,
                    data_dir=database_path.parent,
                    csv_path=str(csv_payload["csv_file_path"]),
                    excel_path=(
                        str(excel_payload["excel_file_path"])
                        if excel_payload.get("excel_export_status")
                        == "succeeded"
                        else None
                    ),
                    folder_id=schedule["drive_folder_id"],
                    upload_csv=schedule["upload_csv"],
                    upload_excel=schedule["upload_excel"],
                )
                run.update(drive)
            elif local_success:
                run["drive_upload_status"] = "not_configured"
        except Exception as exc:
            # Delivery is an independent concern.  Never reclassify or erase a
            # successfully collected SQLite run because scheduling/Drive failed.
            run["drive_upload_status"] = run.get("drive_upload_status") or "failed"
            run["drive_upload_error"] = str(exc)
            if log:
                log(f"Drive delivery for run {run_id} failed independently: {exc}")
        run.update(csv_payload)
        run.update(excel_payload)
        return _payload(run)
