from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
INSTANCES_DIR = BASE_DIR / "data" / "instances"
MERGED_DIR = BASE_DIR / "data" / "merged_exports"


def _read_results(path: Path) -> pd.DataFrame:
    try:
        xl = pd.ExcelFile(path)
    except Exception:
        return pd.DataFrame()
    for sheet_name in ["Hotel Results", "All Results"]:
        if sheet_name in xl.sheet_names:
            try:
                df = pd.read_excel(path, sheet_name=sheet_name)
                df["source_file"] = str(path.resolve())
                df["instance_id"] = path.parents[1].name if len(path.parents) > 1 else path.parent.name
                return df
            except Exception:
                return pd.DataFrame()
    return pd.DataFrame()


def _date_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        return str(value)


def _dedupe_key(row: pd.Series) -> str:
    parts = [
        str(row.get("source") or "").strip().lower(),
        str(row.get("city_or_region") or "").strip().lower(),
        _date_text(row.get("checkin_date")),
        _date_text(row.get("checkout_date")),
    ]
    hotel_url = str(row.get("hotel_url") or "").strip().lower()
    if hotel_url and hotel_url != "nan":
        parts.append(hotel_url)
    else:
        parts.append(str(row.get("hotel_name") or "").strip().lower())
    return "|".join(parts)


def _coverage_rows(results_df: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if results_df.empty or "checkin_date" not in results_df.columns:
        return [], []
    coverage: list[dict[str, Any]] = []
    for instance_id, group in results_df.groupby("instance_id", dropna=False):
        dates = sorted({_date_text(value) for value in group["checkin_date"].dropna() if _date_text(value)})
        coverage.append(
            {
                "instance_id": instance_id,
                "first_checkin_date": dates[0] if dates else "",
                "last_checkin_date": dates[-1] if dates else "",
                "unique_checkin_dates": len(dates),
                "source_files": group["source_file"].nunique() if "source_file" in group.columns else 0,
                "rows": len(group),
            }
        )
    all_dates = sorted({_date_text(value) for value in results_df["checkin_date"].dropna() if _date_text(value)})
    issues: list[dict[str, Any]] = []
    if all_dates:
        start = datetime.fromisoformat(all_dates[0]).date()
        end = datetime.fromisoformat(all_dates[-1]).date()
        expected = {(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)}
        present = set(all_dates)
        for missing in sorted(expected - present):
            issues.append({"issue": "missing check in date", "checkin_date": missing, "detail": ""})
        counts = results_df.assign(_checkin_text=results_df["checkin_date"].map(_date_text)).groupby("_checkin_text").size()
        for date_text, count in counts.items():
            if count > 0 and results_df[results_df["checkin_date"].map(_date_text) == date_text]["instance_id"].nunique() > 1:
                issues.append({"issue": "duplicate date across instances", "checkin_date": date_text, "detail": f"{count} rows"})
    return coverage, issues


def merge_instance_exports(instances_dir: Path | None = None, output_dir: Path | None = None) -> Path:
    instances_dir = instances_dir or INSTANCES_DIR
    output_dir = output_dir or MERGED_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(instances_dir.glob("*/exports/*.xlsx"))
    frames = [_read_results(path) for path in files]
    frames = [frame for frame in frames if not frame.empty]
    all_results = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    duplicates = pd.DataFrame()
    deduped = all_results.copy()
    if not all_results.empty:
        all_results["_dedupe_key"] = all_results.apply(_dedupe_key, axis=1)
        duplicates = all_results[all_results.duplicated("_dedupe_key", keep=False)].copy()
        deduped = all_results.drop_duplicates("_dedupe_key", keep="first").drop(columns=["_dedupe_key"], errors="ignore")
        duplicates = duplicates.drop(columns=["_dedupe_key"], errors="ignore")

    instance_summary_rows = []
    for path in files:
        instance_id = path.parents[1].name if len(path.parents) > 1 else path.parent.name
        instance_rows = all_results[all_results.get("instance_id") == instance_id] if not all_results.empty and "instance_id" in all_results.columns else pd.DataFrame()
        instance_summary_rows.append(
            {
                "instance_id": instance_id,
                "export_file": str(path.resolve()),
                "rows": len(instance_rows),
                "first_checkin_date": min((_date_text(value) for value in instance_rows.get("checkin_date", [])), default=""),
                "last_checkin_date": max((_date_text(value) for value in instance_rows.get("checkin_date", [])), default=""),
            }
        )
    coverage_rows, coverage_issue_rows = _coverage_rows(all_results)

    failed_rows = pd.DataFrame()
    if not all_results.empty and "collection_status" in all_results.columns:
        failed_rows = all_results[all_results["collection_status"].fillna("").astype(str).str.lower() != "success"].copy()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    city = "orlando"
    if not deduped.empty and "city_or_region" in deduped.columns:
        city_values = [str(value).strip().lower().replace(" ", "_") for value in deduped["city_or_region"].dropna().unique()]
        if city_values:
            city = city_values[0]
    output_path = output_dir / f"booking_{city}_MASTER_{timestamp}.xlsx"
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        deduped.to_excel(writer, index=False, sheet_name="All Results")
        pd.DataFrame(instance_summary_rows).to_excel(writer, index=False, sheet_name="Instance Summary")
        pd.DataFrame(coverage_rows + coverage_issue_rows).to_excel(writer, index=False, sheet_name="Date Coverage")
        duplicates.to_excel(writer, index=False, sheet_name="Duplicates Check")
        failed_rows.to_excel(writer, index=False, sheet_name="Failed or Incomplete Dates")
        for sheet_name, sheet in writer.sheets.items():
            sheet.freeze_panes(1, 0)
            sheet.set_column(0, 60, 18)
    return output_path.resolve()


if __name__ == "__main__":
    print(merge_instance_exports())
