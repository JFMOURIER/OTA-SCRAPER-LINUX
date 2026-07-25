from __future__ import annotations

import json
import os
from datetime import date, time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st

from services.google_drive_sync import (
    drive_configuration_status,
    load_drive_state,
    pending_upload_states,
    retry_latest_failed_upload,
    test_drive_folder_access,
)
from services.hardware_preflight import three_worker_concurrency_enabled
from services.schedule_config import (
    DATE_MODE_AUTOMATIC,
    DATE_MODE_MANUAL,
    PARIS,
    default_daily_times,
    disable_schedule,
    frequency_description,
    interval_seconds_for_runs_per_day,
    load_schedule,
    next_scheduled_run,
    paris_now,
    read_schedule_history,
    read_schedule_state,
    resolved_dates_for_schedule,
    save_schedule,
)
from services.schedule_dispatcher import request_run_once
from services.scheduled_instances import (
    SCHEDULED_INSTANCES,
    get_scheduled_instance,
    resolve_automatic_windows,
)


COMPLETE_STATUSES = {"completed", "completed_all_dates"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_drive_state(data_dir: Path) -> dict[str, Any]:
    states = []
    root = data_dir / "status" / "drive_uploads"
    for path in root.glob("run_*.json"):
        payload = _read_json(path)
        if payload:
            states.append((path.stat().st_mtime, payload))
    return (
        sorted(states, key=lambda value: value[0], reverse=True)[0][1]
        if states
        else {}
    )


def _latest_complete_exports() -> dict[str, Any]:
    try:
        from database.db import fetch_collection_runs

        runs = fetch_collection_runs(limit=100, backend="sqlite")
    except Exception:
        return {}
    for run in runs:
        if str(run.get("status") or "") not in COMPLETE_STATUSES:
            continue
        csv_path = Path(str(run.get("csv_file_path") or ""))
        excel_path = Path(str(run.get("excel_file_path") or ""))
        if csv_path.is_file() or excel_path.is_file():
            return {
                "run_id": run.get("id"),
                "csv": csv_path if csv_path.is_file() else None,
                "excel": excel_path if excel_path.is_file() else None,
            }
    return {}


def _time_value(value: str) -> time:
    return time.fromisoformat(value)


def render_automatic_schedule(
    instance_config: Any,
    *,
    stop_callback: Callable[[str], dict[str, Any]] | None = None,
) -> None:
    instance_id = str(instance_config.instance_id)
    if instance_id not in SCHEDULED_INSTANCES:
        return
    definition = get_scheduled_instance(instance_id)
    data_dir = Path(instance_config.data_dir)
    now = paris_now()
    schedule = load_schedule(instance_id, data_dir=data_dir, now=now)
    state = read_schedule_state(instance_id, data_dir=data_dir)
    automatic_start, automatic_end = resolve_automatic_windows(now.date())[
        instance_id
    ]
    resolved_start, resolved_end = resolved_dates_for_schedule(
        schedule,
        now.date(),
    )
    drive_status = drive_configuration_status(
        instance_id,
        folder_id=definition.drive_folder_id,
    )
    latest_drive = _latest_drive_state(data_dir)
    pending_drive_count = len(
        pending_upload_states(instance_id, data_dir=data_dir)
    )
    worker_status = _read_json(
        data_dir / "status" / "current_job_status.json"
    )
    configured_workers = (
        3 if three_worker_concurrency_enabled() else 1
    )

    st.header("Scheduler")
    st.caption(
        "The dispatcher runs independently of Streamlit. Opening or refreshing "
        "this page never starts a collection."
    )
    summary_columns = st.columns(4)
    summary_columns[0].metric(
        "Schedule",
        "Enabled" if schedule["enabled"] else "Disabled",
    )
    summary_columns[1].metric(
        "Date mode",
        (
            "Automatic rolling dates"
            if schedule["date_mode"] == DATE_MODE_AUTOMATIC
            else "Manual fixed dates"
        ),
    )
    summary_columns[2].metric(
        "Current worker",
        str(worker_status.get("status") or state["current_worker_state"]),
    )
    summary_columns[3].metric(
        "Host concurrency",
        f"{configured_workers} worker(s)",
    )
    st.write(f"Current Europe/Paris anchor date: `{now.date().isoformat()}`")
    st.write(
        "Resolved automatic dates: "
        f"`{automatic_start.isoformat()}` through "
        f"`{automatic_end.isoformat()}`"
    )
    st.write(
        "Current configured dates: "
        f"`{resolved_start.isoformat()}` through "
        f"`{resolved_end.isoformat()}`"
    )
    if instance_id == "long_121_365_days":
        st.info(
            "This is the final approximately eight-month section of one rolling "
            "year, not twelve additional months after its start."
        )

    form_key = f"schedule_form_{instance_id}"
    template = dict(schedule["collection_template"])
    with st.form(form_key):
        enabled = st.toggle(
            "On / Off",
            value=bool(schedule["enabled"]),
        )
        manual_start = date.fromisoformat(schedule["manual_start_date"])
        manual_end = date.fromisoformat(schedule["manual_end_date"])
        over_year_confirmed = bool(
            schedule["manual_over_one_year_confirmed"]
        )
        rolling_dates_locked = (
            instance_id == "near_30_days" and bool(schedule["enabled"])
        )
        if rolling_dates_locked:
            date_mode = DATE_MODE_AUTOMATIC
            locked_dates = st.columns(2)
            locked_dates[0].date_input(
                "Start date",
                value=automatic_start,
                disabled=True,
            )
            locked_dates[1].date_input(
                "End date",
                value=automatic_end,
                disabled=True,
            )
            st.caption(
                "Dates are controlled by the rolling scheduler: Europe/Paris "
                "today through today + 30 calendar days. They are recalculated "
                "at the beginning of every cycle."
            )
        else:
            date_label = st.radio(
                "Date mode",
                ["Automatic rolling dates", "Manual fixed dates"],
                index=(
                    0
                    if schedule["date_mode"] == DATE_MODE_AUTOMATIC
                    else 1
                ),
                horizontal=False,
            )
            date_mode = (
                DATE_MODE_AUTOMATIC
                if date_label == "Automatic rolling dates"
                else DATE_MODE_MANUAL
            )
        if date_mode == DATE_MODE_MANUAL:
            manual_columns = st.columns(2)
            manual_start = manual_columns[0].date_input(
                "Manual start date",
                value=manual_start,
            )
            manual_end = manual_columns[1].date_input(
                "Manual end date",
                value=manual_end,
            )
            st.warning(
                "Manual dates are fixed and will be reused for every scheduled "
                "run until automatic rolling dates are restored."
            )
            if manual_end >= manual_start and (
                manual_end - manual_start
            ).days + 1 > 365:
                st.warning("This manual range exceeds 365 days.")
                over_year_confirmed = st.checkbox(
                    "I explicitly confirm this manual range exceeds 365 days",
                    value=over_year_confirmed,
                )
        if instance_id == "near_30_days":
            runs_per_day = int(
                st.selectbox(
                    "Runs per day",
                    list(range(1, 25)),
                    index=max(0, int(schedule["runs_per_day"] or 24) - 1),
                    format_func=lambda value: (
                        f"{value} run per day"
                        if value == 1
                        else f"{value} runs per day"
                    ),
                )
            )
            interval_seconds = interval_seconds_for_runs_per_day(runs_per_day)
            if interval_seconds.is_integer() and int(interval_seconds) % 3600 == 0:
                interval_text = (
                    f"{int(interval_seconds) // 3600} hour(s)"
                )
            elif interval_seconds.is_integer() and int(interval_seconds) % 60 == 0:
                interval_text = (
                    f"{int(interval_seconds) // 60} minute(s)"
                )
            else:
                interval_text = f"{interval_seconds:.3f} seconds"
            st.caption(
                f"{runs_per_day} run(s)/day = every {interval_text} over a "
                "rolling 24-hour period."
            )
            interval_minutes = interval_seconds / 60.0
            daily_run_times: list[str] = []
        else:
            maximum = 4 if instance_id == "medium_31_120_days" else 2
            runs_per_day = int(
                st.selectbox(
                    "Runs per day",
                    list(range(1, maximum + 1)),
                    index=max(
                        0,
                        int(schedule["runs_per_day"] or 1) - 1,
                    ),
                )
            )
            existing_times = list(schedule["daily_run_times"])
            if len(existing_times) != runs_per_day:
                existing_times = list(
                    default_daily_times(instance_id, runs_per_day)
                )
            daily_run_times = []
            time_columns = st.columns(runs_per_day)
            for index in range(runs_per_day):
                selected = time_columns[index].time_input(
                    f"Daily time {index + 1}",
                    value=_time_value(existing_times[index]),
                    step=60,
                )
                daily_run_times.append(selected.strftime("%H:%M"))
            daily_run_times = sorted(daily_run_times)
            interval_minutes = None

        with st.expander("Scheduled collection template", expanded=False):
            source = st.selectbox(
                "Source",
                ["Booking.com", "Demo", "Expedia"],
                index=["Booking.com", "Demo", "Expedia"].index(
                    str(template["source"])
                ),
            )
            city = st.text_input(
                "City or region",
                value=str(template["city_or_region"]),
            )
            basic = st.columns(4)
            nights = int(
                basic[0].number_input(
                    "Length of stay (nights)",
                    min_value=1,
                    max_value=60,
                    value=int(template["nights"]),
                )
            )
            adults = int(
                basic[1].number_input(
                    "Adults",
                    min_value=1,
                    max_value=12,
                    value=int(template["adults"]),
                )
            )
            rooms = int(
                basic[2].number_input(
                    "Rooms",
                    min_value=1,
                    max_value=12,
                    value=int(template["rooms"]),
                )
            )
            currency = basic[3].selectbox(
                "Currency",
                ["USD", "EUR", "GBP", "MXN"],
                index=["USD", "EUR", "GBP", "MXN"].index(
                    str(template["currency"])
                ),
            )
            limits = st.columns(3)
            max_hotels = int(
                limits[0].number_input(
                    "Maximum hotels",
                    min_value=1,
                    max_value=10000,
                    value=int(template["max_hotels"]),
                )
            )
            collect_all = limits[1].checkbox(
                "Collect all available",
                value=bool(template["collect_all_available"]),
            )
            max_scroll = int(
                limits[2].number_input(
                    "Maximum scroll time (minutes)",
                    min_value=1,
                    max_value=240,
                    value=int(template["max_scroll_minutes"]),
                )
            )
            star_ratings = st.multiselect(
                "Selected star ratings",
                [1, 2, 3, 4, 5],
                default=list(template["selected_star_ratings"]),
            )
            filters = st.columns(2)
            include_unknown = filters[0].checkbox(
                "Include unknown star rating",
                value=bool(template["include_unknown_star_rating"]),
            )
            hotels_only = filters[1].checkbox(
                "Hotels only",
                value=bool(template["hotels_only"]),
            )
            modes = st.columns(2)
            performance_mode = modes[0].selectbox(
                "Performance mode",
                ["balanced", "debug"],
                index=(
                    0
                    if template["performance_mode"] == "balanced"
                    else 1
                ),
            )
            browser_mode = modes[1].selectbox(
                "Browser mode",
                ["headless", "visible"],
                index=(
                    0 if template["browser_mode"] == "headless" else 1
                ),
            )
            retry = st.columns(3)
            retry_enabled = retry[0].checkbox(
                "Retry failed dates",
                value=bool(
                    template["retry_failed_dates_automatically"]
                ),
            )
            retries = int(
                retry[1].number_input(
                    "Maximum retries per date",
                    min_value=1,
                    max_value=20,
                    value=int(template["max_retries_per_date"]),
                )
            )
            continue_on_failure = retry[2].checkbox(
                "Continue on date failure",
                value=bool(template["continue_if_date_fails"]),
            )
            auto_partial = st.checkbox(
                "Automatic partial Excel snapshots",
                value=bool(template["auto_export_partial_excel"]),
            )
            partial_frequency = st.selectbox(
                "Partial snapshot frequency",
                [
                    "every_25_dates",
                    "every_5_dates",
                    "every_30_minutes",
                ],
                index=[
                    "every_25_dates",
                    "every_5_dates",
                    "every_30_minutes",
                ].index(str(template["partial_export_frequency"])),
                disabled=not auto_partial,
            )
            local_csv = st.checkbox(
                "Automatic complete CSV export",
                value=bool(template["local_csv_export"]),
                disabled=True,
            )
            local_excel = st.checkbox(
                "Automatic complete Excel export",
                value=bool(template["local_excel_export"]),
                disabled=True,
            )

        with st.expander("Google Drive delivery", expanded=False):
            st.write(f"Dedicated folder ID: `{definition.drive_folder_id}`")
            st.write(definition.drive_folder_url)
            drive_enabled = st.checkbox(
                "Upload completed local exports to Google Drive",
                value=bool(schedule["drive_upload_enabled"]),
            )
            upload_csv = st.checkbox(
                "Upload complete CSV",
                value=bool(schedule["upload_csv"]),
            )
            upload_excel = st.checkbox(
                "Upload complete Excel",
                value=bool(schedule["upload_excel"]),
            )
            st.write(
                "Current configuration state: "
                f"`{drive_status['status']}`"
            )

        submitted = st.form_submit_button(
            "Save Schedule",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        candidate = {
            **schedule,
            "enabled": enabled,
            "date_mode": (
                DATE_MODE_AUTOMATIC
                if instance_id == "near_30_days" and enabled
                else date_mode
            ),
            "manual_start_date": manual_start.isoformat(),
            "manual_end_date": manual_end.isoformat(),
            "manual_over_one_year_confirmed": over_year_confirmed,
            "frequency_mode": (
                "interval" if instance_id == "near_30_days" else "daily"
            ),
            "interval_minutes": interval_minutes,
            "runs_per_day": runs_per_day,
            "daily_run_times": daily_run_times,
            "collection_template": {
                "source": source,
                "city_or_region": city,
                "nights": nights,
                "adults": adults,
                "rooms": rooms,
                "currency": currency,
                "max_hotels": max_hotels,
                "collect_all_available": collect_all,
                "max_scroll_minutes": max_scroll,
                "selected_star_ratings": star_ratings,
                "include_unknown_star_rating": include_unknown,
                "hotels_only": hotels_only,
                "performance_mode": performance_mode,
                "browser_mode": browser_mode,
                "retry_failed_dates_automatically": retry_enabled,
                "max_retries_per_date": retries,
                "continue_if_date_fails": continue_on_failure,
                "auto_export_partial_excel": auto_partial,
                "partial_export_frequency": partial_frequency,
                "local_csv_export": local_csv,
                "local_excel_export": local_excel,
            },
            "drive_upload_enabled": drive_enabled,
            "drive_folder_id": definition.drive_folder_id,
            "upload_csv": upload_csv,
            "upload_excel": upload_excel,
        }
        try:
            saved = save_schedule(
                instance_id,
                candidate,
                data_dir=data_dir,
                modified_by="streamlit",
            )
            saved_start, saved_end = resolved_dates_for_schedule(
                saved,
                paris_now().date(),
            )
            next_run = next_scheduled_run(saved)
            st.success("Schedule saved atomically")
            st.json(
                {
                    "instance_id": instance_id,
                    "date_mode": saved["date_mode"],
                    "resolved_current_date_range": (
                        f"{saved_start} to {saved_end}"
                    ),
                    "frequency": frequency_description(saved),
                    "next_scheduled_run": (
                        next_run.isoformat(timespec="seconds")
                        if next_run
                        else None
                    ),
                    "drive_upload": (
                        "enabled"
                        if saved["drive_upload_enabled"]
                        else "disabled"
                    ),
                }
            )
        except Exception as exc:
            st.error(
                "Schedule was not saved; the previous valid schedule remains "
                f"unchanged: {exc}"
            )

    action_columns = st.columns(5)
    if action_columns[0].button(
        "Disable Schedule",
        key=f"disable_schedule_{instance_id}",
        use_container_width=True,
    ):
        disable_schedule(
            instance_id,
            data_dir=data_dir,
            modified_by="streamlit",
        )
        st.success("Future scheduled runs are disabled.")
        st.rerun()
    if action_columns[1].button(
        "Run Once Now",
        key=f"run_once_{instance_id}",
        use_container_width=True,
    ):
        try:
            result = request_run_once(instance_id, data_dir=data_dir)
            if result["status"] == "started":
                st.success(
                    "Run-once request started independently of Streamlit."
                )
            else:
                st.warning(
                    f"Run once did not start: {result.get('reason')}"
                )
        except Exception as exc:
            st.error(f"Run once failed to start: {exc}")
    if action_columns[2].button(
        "Recalculate Preview",
        key=f"preview_schedule_{instance_id}",
        use_container_width=True,
    ):
        st.rerun()
    if action_columns[3].button(
        "Test Google Drive Connection",
        key=f"drive_test_{instance_id}",
        use_container_width=True,
    ):
        result = test_drive_folder_access(instance_id)
        if result["status"] == "verified":
            st.success("Google Drive read/write verification succeeded.")
        else:
            st.warning(result)
    if action_columns[4].button(
        "Retry Latest Failed Upload",
        key=f"drive_retry_{instance_id}",
        use_container_width=True,
    ):
        result = retry_latest_failed_upload(
            instance_id,
            data_dir=data_dir,
        )
        if result.get("drive_upload_status") == "succeeded":
            st.success("Drive upload retry succeeded without rerunning collection.")
        else:
            st.warning(result)

    if st.button(
        "Stop",
        key=f"stop_scheduled_worker_{instance_id}",
        use_container_width=True,
    ):
        if stop_callback is not None:
            result = stop_callback("sidebar")
        else:
            from services.stop_control import request_instance_stop

            result = request_instance_stop(
                instance_id,
                data_dir=data_dir,
                source="sidebar",
            )
        if result["status"] == "stop_requested":
            st.warning("Stop requested…")
        else:
            st.info("Scheduler is off; no active worker was detected.")

    schedule = load_schedule(instance_id, data_dir=data_dir)
    state = read_schedule_state(instance_id, data_dir=data_dir)
    next_run = next_scheduled_run(schedule)
    latest_drive = _latest_drive_state(data_dir)
    status_rows = [
        {"Item": "Last scheduled run", "Value": state["last_scheduled_run"]},
        {"Item": "Last completed run", "Value": state["last_completed_run"]},
        {"Item": "Last successful run", "Value": state["last_successful_run"]},
        {"Item": "Last failed run", "Value": state["last_failed_run"]},
        {
            "Item": "Next scheduled run",
            "Value": (
                next_run.isoformat(timespec="seconds") if next_run else None
            ),
        },
        {
            "Item": "Current schedule state",
            "Value": state["current_schedule_state"],
        },
        {
            "Item": "Current worker state",
            "Value": worker_status.get("status")
            or state["current_worker_state"],
        },
        {"Item": "Last skip reason", "Value": state["last_skip_reason"]},
        {"Item": "Last defer reason", "Value": state["last_defer_reason"]},
        {
            "Item": "Current host concurrency state",
            "Value": f"OTA_MAX_CONCURRENT_WORKERS={configured_workers}",
        },
        {
            "Item": "Google Drive configuration state",
            "Value": drive_status["status"],
        },
        {
            "Item": "Drive configured",
            "Value": drive_status["status"] == "configured",
        },
        {
            "Item": "Target folder",
            "Value": definition.drive_folder_id,
        },
        {
            "Item": "Last successful upload",
            "Value": latest_drive.get("drive_uploaded_at"),
        },
        {
            "Item": "Last uploaded filename",
            "Value": latest_drive.get("google_drive_remote_filename"),
        },
        {
            "Item": "Local / remote size",
            "Value": (
                f"{latest_drive.get('local_csv_bytes')} / "
                f"{latest_drive.get('google_drive_remote_bytes')}"
            ),
        },
        {
            "Item": "Pending upload count",
            "Value": pending_drive_count,
        },
        {
            "Item": "Last Drive error",
            "Value": latest_drive.get("drive_upload_error"),
        },
        {
            "Item": "Last upload status",
            "Value": latest_drive.get("drive_upload_status")
            or "not_configured",
        },
        {
            "Item": "Last Drive CSV path",
            "Value": latest_drive.get("drive_csv_path"),
        },
        {
            "Item": "Last Drive Excel path",
            "Value": latest_drive.get("drive_excel_path"),
        },
    ]
    st.dataframe(
        pd.DataFrame(status_rows),
        hide_index=True,
        use_container_width=True,
    )

    latest_exports = _latest_complete_exports()
    download_columns = st.columns(2)
    if latest_exports.get("csv"):
        csv_path = latest_exports["csv"]
        download_columns[0].download_button(
            "Download Latest Complete CSV",
            data=csv_path.read_bytes(),
            file_name=csv_path.name,
            mime="text/csv",
            key=f"download_complete_csv_{instance_id}",
        )
    else:
        download_columns[0].button(
            "Download Latest Complete CSV",
            disabled=True,
            key=f"download_complete_csv_disabled_{instance_id}",
        )
    if latest_exports.get("excel"):
        excel_path = latest_exports["excel"]
        download_columns[1].download_button(
            "Download Latest Complete Excel",
            data=excel_path.read_bytes(),
            file_name=excel_path.name,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            key=f"download_complete_excel_{instance_id}",
        )
    else:
        download_columns[1].button(
            "Download Latest Complete Excel",
            disabled=True,
            key=f"download_complete_excel_disabled_{instance_id}",
        )

    history = read_schedule_history(
        instance_id,
        data_dir=data_dir,
        limit=50,
    )
    with st.expander("Recent schedule history", expanded=False):
        if history:
            st.dataframe(
                pd.DataFrame(history),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("No scheduler events have been recorded yet.")
