from __future__ import annotations

import re
import os
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from services.date_utils import format_date_for_filename, format_timestamp_for_filename
from services.normalizer import RESULT_FIELDS


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

    with pd.ExcelWriter(file_path, engine="xlsxwriter") as writer:
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

    with pd.ExcelWriter(file_path, engine="xlsxwriter") as writer:
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

    return file_path.resolve()
