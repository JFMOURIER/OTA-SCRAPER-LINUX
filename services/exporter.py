from __future__ import annotations

import csv
import re
import os
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import xlsxwriter

from services.date_utils import format_date_for_filename, format_timestamp_for_filename
from services.normalizer import RESULT_FIELDS


CSV_EXPORT_COLUMNS = [
    "source_website",
    "hotel_name",
    "star_rating",
    "review_score",
    "number_of_reviews",
    "cheapest_visible_price",
    "currency",
    "hotel_url",
    "destination_city",
    "checkin_date",
    "checkout_date",
    "timestamp",
    "scrape_session_id",
    "instance_id",
    "error_message",
]


def safe_filename(text: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text))
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return cleaned or "file"


def build_excel_filename(source: str, city_or_region: str, checkin_date: Any, number_of_nights: int) -> str:
    source_part = safe_filename(source.lower().replace(".com", ""))
    city_part = safe_filename(city_or_region)
    raw_instance_id = str(os.getenv("INSTANCE_ID", "")).strip().lower()
    instance_id = safe_filename(raw_instance_id) if raw_instance_id else ""
    start_date = os.getenv("INSTANCE_START_DATE")
    end_date = os.getenv("INSTANCE_END_DATE")
    date_part = format_date_for_filename(checkin_date)
    timestamp = format_timestamp_for_filename()
    if instance_id and start_date and end_date:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{source_part}_{city_part.lower()}_{instance_id}_{start_date}_to_{end_date}_{timestamp}.xlsx"
    return f"{source_part}_{city_part}_{date_part}_{number_of_nights}_nights_{timestamp}.xlsx"


def downloads_dir() -> Path:
    return Path.home() / "Downloads"


def build_csv_filename(source: str, city_or_region: str, instance_id: str, exported_at: datetime | None = None) -> str:
    timestamp = (exported_at or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    source_part = safe_filename(str(source or "source").lower().replace(".com", ""))
    city_part = safe_filename(str(city_or_region or "city").lower())
    instance_part = safe_filename(str(instance_id or "default").lower())
    return f"ota_results_{source_part}_{city_part}_{instance_part}_{timestamp}.csv"


def _numeric_values(results: list[dict[str, Any]], field: str) -> list[float]:
    values: list[float] = []
    for row in results:
        value = row.get(field)
        if value is None or value == "":
            continue
        values.append(float(value))
    return values


def create_summary(results: list[dict[str, Any]], run_metadata: dict[str, Any]) -> dict[str, Any]:
    prices = _numeric_values(results, "cheapest_price_total")
    review_scores = _numeric_values(results, "review_score")
    success_count = sum(1 for row in results if row.get("collection_status") == "success")
    failed_count = sum(1 for row in results if row.get("collection_status") != "success")

    summary = {
        "source": run_metadata.get("source"),
        "city_or_region": run_metadata.get("city_or_region"),
        "checkin_date": run_metadata.get("checkin_date"),
        "checkout_date": run_metadata.get("checkout_date"),
        "number_of_nights": run_metadata.get("number_of_nights"),
        "adults": run_metadata.get("adults"),
        "currency": run_metadata.get("currency"),
        "total hotels collected": len(results),
        "lowest price": min(prices) if prices else None,
        "highest price": max(prices) if prices else None,
        "average price": round(sum(prices) / len(prices), 2) if prices else None,
        "average review score": round(sum(review_scores) / len(review_scores), 2) if review_scores else None,
        "number of successful records": success_count,
        "number of failed records": failed_count,
        "collection started at": run_metadata.get("started_at"),
        "collection completed at": run_metadata.get("completed_at") or datetime.now(),
    }

    for key in [
        "performance mode",
        "request blocking enabled",
        "hotels only filter enabled",
        "records removed by hotels only filter",
        "total raw records extracted",
        "total records after hotels only filter",
        "load more button clicks",
        "load more button seen count",
        "load more click failures",
        "last load more error",
        "last load more button text",
        "last load more locator",
        "last load more button box",
        "last load more visible",
        "last load more enabled",
        "cookie banner visible before load more",
        "final number of hotel cards found",
        "final extracted hotel count",
        "maximum scroll time reached",
        "total runtime seconds",
        "browser startup time seconds",
        "total page load time seconds",
        "total scrolling and load more time seconds",
        "total extraction time seconds",
        "total database save time seconds",
        "total excel export time seconds",
        "ultra reliable loading mode",
        "completion status",
        "visible result count text",
        "target result count",
        "visible result count is lower bound",
        "unique results loaded",
        "estimated missing results",
        "load more success count",
        "consecutive no new unique results",
        "consecutive no load more seen",
        "consecutive failed load more clicks",
        "bottom visible text path",
        "full loading debug JSON path",
        "unique results loaded path",
        "resume checkpoint path",
        "final stop reason",
        "booking visible result count",
        "booking result count difference",
        "visible hotel cards count",
        "unique hotels collected",
        "new hotels added last cycle",
        "lowest price collected during loading",
        "highest price collected during loading",
        "hotels with valid price during loading",
        "hotels missing price during loading",
        "final URL",
        "final page title",
        "final bottom screenshot path",
        "collection completeness warning",
        "average seconds per hotel",
        "hotels with known star rating",
        "hotels with unknown star rating",
        "hotels removed by star filter",
        "hotels kept after star filter",
    ]:
        if key in run_metadata:
            summary[key] = run_metadata.get(key)

    star_counts: dict[str, int] = {}
    for row in results:
        star = row.get("star_rating")
        label = "Unknown" if star is None else str(star)
        star_counts[label] = star_counts.get(label, 0) + 1
    summary["hotels by star rating"] = star_counts
    return summary


def _serialize_for_excel(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, default=str)
    return value


def _serialize_for_csv(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, default=str)
    return "" if value is None else value


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _summary_or_first_row(summary: dict[str, Any], results: list[dict[str, Any]], key: str, default: Any = None) -> Any:
    value = summary.get(key)
    if value is not None and value != "":
        return value
    for row in results:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _result_csv_row(row: dict[str, Any], *, session_id: Any, instance_id: str, exported_at: datetime) -> dict[str, Any]:
    return {
        "source_website": _serialize_for_csv(row.get("source")),
        "hotel_name": _serialize_for_csv(row.get("hotel_name")),
        "star_rating": _serialize_for_csv(row.get("star_rating")),
        "review_score": _serialize_for_csv(row.get("review_score")),
        "number_of_reviews": _serialize_for_csv(row.get("review_count")),
        "cheapest_visible_price": _serialize_for_csv(_first_present(row, "cheapest_price_total", "parsed_price", "raw_price_text")),
        "currency": _serialize_for_csv(row.get("currency")),
        "hotel_url": _serialize_for_csv(row.get("hotel_url")),
        "destination_city": _serialize_for_csv(row.get("city_or_region")),
        "checkin_date": _serialize_for_csv(row.get("checkin_date")),
        "checkout_date": _serialize_for_csv(row.get("checkout_date")),
        "timestamp": _serialize_for_csv(row.get("collected_at") or exported_at),
        "scrape_session_id": _serialize_for_csv(session_id or row.get("collection_run_id")),
        "instance_id": _serialize_for_csv(instance_id),
        "error_message": _serialize_for_csv(row.get("error_message")),
    }


def export_results_to_csv(
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    output_dir: str | Path | None = None,
    filename: str | None = None,
    *,
    instance_id: str | None = None,
    session_id: Any = None,
) -> Path:
    exported_at = datetime.now()
    output_path = Path(output_dir) if output_dir is not None else downloads_dir()
    output_path.mkdir(parents=True, exist_ok=True)
    source = _summary_or_first_row(summary, results, "source", "source")
    city = _summary_or_first_row(summary, results, "city_or_region", "city")
    resolved_instance_id = instance_id or str(summary.get("instance_id") or os.getenv("INSTANCE_ID") or "default")
    resolved_session_id = session_id or summary.get("scrape_session_id") or summary.get("run_id")
    csv_path = output_path / (filename or build_csv_filename(str(source), str(city), str(resolved_instance_id), exported_at))

    tmp_path = csv_path.with_name(f".{csv_path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_EXPORT_COLUMNS)
        writer.writeheader()
        for row in results:
            writer.writerow(_result_csv_row(row, session_id=resolved_session_id, instance_id=str(resolved_instance_id), exported_at=exported_at))
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(csv_path)
    return csv_path.resolve()


def _results_dataframe(results: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame([{key: _serialize_for_excel(value) for key, value in row.items()} for row in results])
    if df.empty:
        df = pd.DataFrame(columns=RESULT_FIELDS)
    return df


def _by_date_summary(results_df: pd.DataFrame) -> pd.DataFrame:
    if results_df.empty or "checkin_date" not in results_df.columns:
        return pd.DataFrame(columns=["checkin_date", "checkout_date", "hotels_collected", "errors", "lowest_price", "average_price"])
    df = results_df.copy()
    if "collection_status" in df.columns:
        df["is_error"] = df["collection_status"].astype(str).ne("success")
    else:
        df["is_error"] = False
    if "cheapest_price_total" not in df.columns:
        df["cheapest_price_total"] = None
    if "hotel_name" not in df.columns:
        df["hotel_name"] = None
    if "checkout_date" not in df.columns:
        df["checkout_date"] = None
    return (
        df.groupby(["checkin_date", "checkout_date"], dropna=False)
        .agg(
            hotels_collected=("hotel_name", "count"),
            errors=("is_error", "sum"),
            lowest_price=("cheapest_price_total", "min"),
            average_price=("cheapest_price_total", "mean"),
        )
        .reset_index()
    )


def _by_source_summary(results_df: pd.DataFrame) -> pd.DataFrame:
    if results_df.empty or "source" not in results_df.columns:
        return pd.DataFrame(columns=["source", "hotels_collected", "errors", "lowest_price", "average_price"])
    df = results_df.copy()
    if "collection_status" in df.columns:
        df["is_error"] = df["collection_status"].astype(str).ne("success")
    else:
        df["is_error"] = False
    if "cheapest_price_total" not in df.columns:
        df["cheapest_price_total"] = None
    if "hotel_name" not in df.columns:
        df["hotel_name"] = None
    return (
        df.groupby("source", dropna=False)
        .agg(
            hotels_collected=("hotel_name", "count"),
            errors=("is_error", "sum"),
            lowest_price=("cheapest_price_total", "min"),
            average_price=("cheapest_price_total", "mean"),
        )
        .reset_index()
    )


def _errors_dataframe(results_df: pd.DataFrame) -> pd.DataFrame:
    if results_df.empty or "collection_status" not in results_df.columns:
        return pd.DataFrame(columns=results_df.columns)
    return results_df[results_df["collection_status"].astype(str).ne("success")].copy()


def _raw_api_debug_dataframe(results_df: pd.DataFrame) -> pd.DataFrame:
    columns = [column for column in ["source", "provider_name", "hotel_name", "checkin_date", "raw_source_payload", "error_message"] if column in results_df.columns]
    if not columns or "raw_source_payload" not in columns:
        return pd.DataFrame()
    debug_df = results_df[results_df["raw_source_payload"].fillna("").astype(str).ne("")][columns].copy()
    return debug_df


def build_block_excel_filename(source: str, city_or_region: str, block_start_date: Any, block_end_date: Any, timestamp: str) -> str:
    source_part = safe_filename(source.lower().replace(".com", ""))
    city_part = safe_filename(str(city_or_region).lower())
    return f"{source_part}_{city_part}_{block_start_date}_to_{block_end_date}_{timestamp}.xlsx"


def build_master_excel_filename(source: str, city_or_region: str, start_date: Any, end_date: Any, timestamp: str) -> str:
    source_part = safe_filename(source.lower().replace(".com", ""))
    city_part = safe_filename(str(city_or_region).lower())
    return f"{source_part}_{city_part}_FULL_{start_date}_to_{end_date}_{timestamp}.xlsx"


def export_results_to_excel(results: list[dict[str, Any]], summary: dict[str, Any], output_dir: str | Path, filename: str | None = None) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    filename = filename or build_excel_filename(
        str(summary.get("source") or "source"),
        str(summary.get("city_or_region") or "city"),
        summary.get("checkin_date"),
        int(summary.get("number_of_nights") or 1),
    )
    file_path = output_path / filename
    tmp_path = file_path.with_name(f".{file_path.stem}.{os.getpid()}.{uuid4().hex}.tmp{file_path.suffix}")

    results_df = _results_dataframe(results)
    date_status_rows = summary.get("__date_status_rows") if isinstance(summary.get("__date_status_rows"), list) else []
    public_summary = {key: value for key, value in summary.items() if not str(key).startswith("__")}
    summary_rows = []
    for key, value in public_summary.items():
        if isinstance(value, dict):
            value = ", ".join(f"{item_key}: {item_value}" for item_key, item_value in value.items())
        summary_rows.append({"Metric": key, "Value": _serialize_for_excel(value)})
    summary_df = pd.DataFrame(summary_rows)
    date_status_df = pd.DataFrame(date_status_rows)
    by_date_df = _by_date_summary(results_df)
    by_source_df = _by_source_summary(results_df)
    errors_df = _errors_dataframe(results_df)
    raw_debug_df = _raw_api_debug_dataframe(results_df)

    with pd.ExcelWriter(tmp_path, engine="xlsxwriter") as writer:
        results_df.to_excel(writer, index=False, sheet_name="All Results")
        by_date_df.to_excel(writer, index=False, sheet_name="By Date Summary")
        by_source_df.to_excel(writer, index=False, sheet_name="By Source Summary")
        errors_df.to_excel(writer, index=False, sheet_name="Errors")
        if not raw_debug_df.empty:
            raw_debug_df.to_excel(writer, index=False, sheet_name="Raw API Debug")
        if not date_status_df.empty:
            date_status_df.to_excel(writer, index=False, sheet_name="Date Status")
        summary_df.to_excel(writer, index=False, sheet_name="Run Summary")

        workbook = writer.book
        results_sheet = writer.sheets["All Results"]
        summary_sheet = writer.sheets["Run Summary"]
        money_format = workbook.add_format({"num_format": "$#,##0.00"})
        number_format = workbook.add_format({"num_format": "0.00"})
        date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})

        for sheet_name, df in {
            "All Results": results_df,
            "By Date Summary": by_date_df,
            "By Source Summary": by_source_df,
            "Errors": errors_df,
            **({"Raw API Debug": raw_debug_df} if not raw_debug_df.empty else {}),
            **({"Date Status": date_status_df} if not date_status_df.empty else {}),
        }.items():
            sheet = writer.sheets[sheet_name]
            sheet.freeze_panes(1, 0)
            if not df.empty:
                sheet.autofilter(0, 0, len(df), max(len(df.columns) - 1, 0))
            for idx, column in enumerate(df.columns):
                width = min(max(len(str(column)) + 2, 14), 42)
                fmt = None
                if "price" in column:
                    fmt = money_format
                elif column in {"review_score", "star_rating"}:
                    fmt = number_format
                elif "date" in column or column.endswith("_at"):
                    fmt = date_format
                sheet.set_column(idx, idx, width, fmt)

        summary_sheet.freeze_panes(1, 0)
        summary_sheet.autofilter(0, 0, len(summary_df), 1)
        summary_sheet.set_column(0, 0, 32)
        summary_sheet.set_column(1, 1, 48)

    tmp_path.replace(file_path)
    return file_path.resolve()


def export_batch_master_excel(
    results: list[dict[str, Any]],
    block_rows: list[dict[str, Any]],
    run_summary: dict[str, Any],
    incomplete_rows: list[dict[str, Any]],
    output_dir: str | Path,
    filename: str,
) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    file_path = output_path / filename
    tmp_path = file_path.with_name(f".{file_path.stem}.{os.getpid()}.{uuid4().hex}.tmp{file_path.suffix}")

    results_df = _results_dataframe(results)
    block_df = pd.DataFrame(block_rows)
    summary_df = pd.DataFrame(
        [{"Metric": key, "Value": _serialize_for_excel(value)} for key, value in run_summary.items()]
    )
    by_date_df = _by_date_summary(results_df)
    by_source_df = _by_source_summary(results_df)
    errors_df = _errors_dataframe(results_df)
    incomplete_df = pd.DataFrame(incomplete_rows)
    if not incomplete_df.empty:
        errors_df = pd.concat([errors_df, incomplete_df], ignore_index=True, sort=False)
    raw_debug_df = _raw_api_debug_dataframe(results_df)

    with pd.ExcelWriter(tmp_path, engine="xlsxwriter") as writer:
        results_df.to_excel(writer, index=False, sheet_name="All Results")
        by_date_df.to_excel(writer, index=False, sheet_name="By Date Summary")
        by_source_df.to_excel(writer, index=False, sheet_name="By Source Summary")
        errors_df.to_excel(writer, index=False, sheet_name="Errors")
        if not raw_debug_df.empty:
            raw_debug_df.to_excel(writer, index=False, sheet_name="Raw API Debug")
        block_df.to_excel(writer, index=False, sheet_name="Block Summary")
        summary_df.to_excel(writer, index=False, sheet_name="Run Summary")

        workbook = writer.book
        money_format = workbook.add_format({"num_format": "$#,##0.00"})
        number_format = workbook.add_format({"num_format": "0.00"})
        date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
        for sheet_name, df in {
            "All Results": results_df,
            "By Date Summary": by_date_df,
            "By Source Summary": by_source_df,
            "Errors": errors_df,
            **({"Raw API Debug": raw_debug_df} if not raw_debug_df.empty else {}),
            "Block Summary": block_df,
            "Run Summary": summary_df,
        }.items():
            sheet = writer.sheets[sheet_name]
            sheet.freeze_panes(1, 0)
            if not df.empty:
                sheet.autofilter(0, 0, len(df), max(len(df.columns) - 1, 0))
            for idx, column in enumerate(df.columns):
                width = min(max(len(str(column)) + 2, 14), 48)
                fmt = None
                if "price" in str(column):
                    fmt = money_format
                elif column in {"review_score", "star_rating", "completion_percentage"}:
                    fmt = number_format
                elif "date" in str(column) or str(column).endswith("_at"):
                    fmt = date_format
                sheet.set_column(idx, idx, width, fmt)

    tmp_path.replace(file_path)
    return file_path.resolve()


def _stream_cell(value: Any) -> Any:
    value = _serialize_for_excel(value)
    if isinstance(value, str) and len(value) > 32767:
        return value[:32764] + "..."
    return value


def export_sqlite_run_to_excel(
    run_id: int,
    summary: dict[str, Any],
    output_dir: str | Path,
    filename: str | None = None,
    *,
    batch_size: int = 500,
) -> Path:
    """Export a run with bounded memory and an atomic final rename."""

    from database.db import iter_sqlite_results_by_run_id

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    filename = filename or build_excel_filename(
        str(summary.get("source") or "source"),
        str(summary.get("city_or_region") or "city"),
        summary.get("checkin_date"),
        int(summary.get("number_of_nights") or 1),
    )
    file_path = output_path / filename
    tmp_path = file_path.with_name(f".{file_path.stem}.{os.getpid()}.{uuid4().hex}.tmp{file_path.suffix}")
    columns = list(RESULT_FIELDS)
    raw_columns = ["source", "provider_name", "hotel_name", "checkin_date", "raw_source_payload", "error_message"]
    by_date: dict[tuple[str, str], dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}

    workbook = xlsxwriter.Workbook(str(tmp_path), {"constant_memory": True})
    try:
        sheets = {
            "All Results": workbook.add_worksheet("All Results"),
            "Errors": workbook.add_worksheet("Errors"),
            "Raw API Debug": workbook.add_worksheet("Raw API Debug"),
        }
        for col, name in enumerate(columns):
            sheets["All Results"].write(0, col, name)
            sheets["Errors"].write(0, col, name)
        for col, name in enumerate(raw_columns):
            sheets["Raw API Debug"].write(0, col, name)
        result_row = error_row = raw_row = 1
        for batch in iter_sqlite_results_by_run_id(run_id, batch_size=batch_size):
            for result in batch:
                for col, name in enumerate(columns):
                    sheets["All Results"].write(result_row, col, _stream_cell(result.get(name)))
                result_row += 1
                is_error = str(result.get("collection_status") or "success") != "success"
                if is_error:
                    for col, name in enumerate(columns):
                        sheets["Errors"].write(error_row, col, _stream_cell(result.get(name)))
                    error_row += 1
                if result.get("raw_source_payload"):
                    for col, name in enumerate(raw_columns):
                        sheets["Raw API Debug"].write(raw_row, col, _stream_cell(result.get(name)))
                    raw_row += 1
                checkin = str(result.get("checkin_date") or "")
                checkout = str(result.get("checkout_date") or "")
                date_bucket = by_date.setdefault((checkin, checkout), {"hotels": 0, "errors": 0, "price_count": 0, "price_sum": 0.0, "price_min": None})
                source_bucket = by_source.setdefault(str(result.get("source") or ""), {"hotels": 0, "errors": 0, "price_count": 0, "price_sum": 0.0, "price_min": None})
                if result.get("hotel_name"):
                    date_bucket["hotels"] += 1
                    source_bucket["hotels"] += 1
                date_bucket["errors"] += int(is_error)
                source_bucket["errors"] += int(is_error)
                try:
                    price = float(result.get("cheapest_price_total"))
                except (TypeError, ValueError):
                    price = None
                if price is not None:
                    for bucket in (date_bucket, source_bucket):
                        bucket["price_count"] += 1
                        bucket["price_sum"] += price
                        bucket["price_min"] = price if bucket["price_min"] is None else min(bucket["price_min"], price)
            del batch

        by_date_sheet = workbook.add_worksheet("By Date Summary")
        by_date_headers = ["checkin_date", "checkout_date", "hotels_collected", "errors", "lowest_price", "average_price"]
        for col, name in enumerate(by_date_headers):
            by_date_sheet.write(0, col, name)
        for row_index, ((checkin, checkout), values) in enumerate(sorted(by_date.items()), start=1):
            count = values["price_count"]
            row = [checkin, checkout, values["hotels"], values["errors"], values["price_min"], values["price_sum"] / count if count else None]
            for col, value in enumerate(row):
                by_date_sheet.write(row_index, col, value)

        by_source_sheet = workbook.add_worksheet("By Source Summary")
        source_headers = ["source", "hotels_collected", "errors", "lowest_price", "average_price"]
        for col, name in enumerate(source_headers):
            by_source_sheet.write(0, col, name)
        for row_index, (source, values) in enumerate(sorted(by_source.items()), start=1):
            count = values["price_count"]
            row = [source, values["hotels"], values["errors"], values["price_min"], values["price_sum"] / count if count else None]
            for col, value in enumerate(row):
                by_source_sheet.write(row_index, col, value)

        date_status_sheet = workbook.add_worksheet("Date Status")
        date_status_rows = summary.get("__date_status_rows") if isinstance(summary.get("__date_status_rows"), list) else []
        date_status_columns = sorted({key for row in date_status_rows for key in row})
        for col, name in enumerate(date_status_columns):
            date_status_sheet.write(0, col, name)
        for row_index, row in enumerate(date_status_rows, start=1):
            for col, name in enumerate(date_status_columns):
                date_status_sheet.write(row_index, col, _stream_cell(row.get(name)))

        summary_sheet = workbook.add_worksheet("Run Summary")
        summary_sheet.write_row(0, 0, ["Metric", "Value"])
        public_summary = {key: value for key, value in summary.items() if not str(key).startswith("__")}
        public_summary["rows exported"] = result_row - 1
        for row_index, (key, value) in enumerate(public_summary.items(), start=1):
            summary_sheet.write(row_index, 0, str(key))
            summary_sheet.write(row_index, 1, _stream_cell(value))

        for sheet in [*sheets.values(), by_date_sheet, by_source_sheet, date_status_sheet, summary_sheet]:
            sheet.freeze_panes(1, 0)
        sheets["All Results"].autofilter(0, 0, max(0, result_row - 1), len(columns) - 1)
        for sheet in sheets.values():
            sheet.set_column(0, max(len(columns) - 1, 0), 18)
    except Exception:
        workbook.close()
        raise
    else:
        workbook.close()
    tmp_path.replace(file_path)
    return file_path.resolve()


def export_sqlite_run_to_csv(
    run_id: int,
    summary: dict[str, Any],
    output_dir: str | Path,
    filename: str | None = None,
    *,
    instance_id: str = "default",
    batch_size: int = 500,
) -> Path:
    from database.db import iter_sqlite_results_by_run_id

    exported_at = datetime.now()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    filename = filename or build_csv_filename(
        str(summary.get("source") or "source"),
        str(summary.get("city_or_region") or "city"),
        instance_id,
        exported_at,
    )
    csv_path = output_path / filename
    tmp_path = csv_path.with_name(f".{csv_path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_EXPORT_COLUMNS)
        writer.writeheader()
        for batch in iter_sqlite_results_by_run_id(run_id, batch_size=batch_size):
            for row in batch:
                writer.writerow(_result_csv_row(row, session_id=run_id, instance_id=instance_id, exported_at=exported_at))
            del batch
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(csv_path)
    return csv_path.resolve()
