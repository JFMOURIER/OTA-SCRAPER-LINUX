from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import gc
import json
import multiprocessing
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from shutil import copyfile
from threading import Event, Lock
from typing import Any
from uuid import uuid4

import os
import pandas as pd
import streamlit as st
from dotenv import dotenv_values, load_dotenv
from playwright.sync_api import sync_playwright

from collectors.base import CollectorOptions
from collectors.amadeus_hotels import AmadeusHotelsCollector
from collectors.booking_playwright import BookingPlaywrightCollector
from collectors.custom_api import CustomApiCollector
from collectors.dataforseo_google_hotels import DataForSeoGoogleHotelsCollector
from collectors.demo_collector import DemoCollector
from collectors.expedia_rapid import ExpediaRapidCollector
from collectors.expedia_playwright import ExpediaPlaywrightCollector
from collectors.google_hotels_api_placeholder import GoogleHotelsApiPlaceholderCollector
from collectors.searchapi_google_hotels import SearchApiGoogleHotelsCollector
from collectors.serpapi_google_hotels import SerpApiGoogleHotelsCollector
from database.db import (
    SQLITE_DB_PATH,
    create_collection_run,
    count_results_by_run_id,
    fetch_collection_runs,
    fetch_latest_results,
    fetch_results_by_run_id,
    fetch_results_by_run_id_limited,
    get_connection,
    init_db,
    insert_hotel_results,
    result_date_counts_by_run_id,
    reset_sqlite_database,
    sqlite_wal_checkpoint,
    update_collection_run_excel_path,
    update_collection_run_status,
)
from services.date_utils import calculate_checkout_date, split_date_range_into_blocks
from services.exporter import (
    build_block_excel_filename,
    build_master_excel_filename,
    create_summary,
    export_batch_master_excel,
    export_results_to_excel,
    export_results_to_csv,
    export_sqlite_run_to_csv,
    export_sqlite_run_to_excel,
    safe_filename,
)
from services.instance_config import INSTANCE_CONFIG
from services.job_runner import (
    CancellationSignal,
    RetrySettings,
    atomic_write_json,
    build_checkpoint,
    final_run_status,
    heartbeat_is_stale,
    load_checkpoint,
    save_partial_records,
    should_export_snapshot,
    update_checkpoint_date,
    update_heartbeat,
)
from services.normalizer import normalize_hotel_result
from services.playwright_safe import launch_managed_chromium_context, safe_close_browser, safe_screenshot
from services.resource_guard import (
    ResourceGuard,
    ResourceLimitExceeded,
    ScraperAlreadyRunning,
    SingleScraperLock,
    cleanup_owned_browser_processes,
)
from services.scraper_errors import (
    AccessRestrictionError,
    BrowserClosedError,
    FatalScraperConfigError,
    classify_scraper_error,
    is_browser_closed_error,
)
from tools.merge_instance_exports import merge_instance_exports


BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = INSTANCE_CONFIG.export_dir
SCREENSHOT_DIR = INSTANCE_CONFIG.screenshot_dir
DEBUG_DIR = INSTANCE_CONFIG.debug_dir
LOG_DIR = INSTANCE_CONFIG.log_dir
BROWSER_PROFILE_DIR = INSTANCE_CONFIG.browser_profile_dir
CHECKPOINT_DIR = INSTANCE_CONFIG.checkpoint_dir
STATUS_DIR = INSTANCE_CONFIG.status_dir
STATUS_FILE = INSTANCE_CONFIG.status_file
PARTIAL_DIR = INSTANCE_CONFIG.partial_dir
HEARTBEAT_FILE = STATUS_DIR / "heartbeat.json"
ENV_PATH = BASE_DIR / ".env"
SQLITE_WRITE_LOCK = Lock()
ACTIVE_STATUS_STALE_AFTER_SECONDS = 120
# Lock is per instance; the legacy global lock is intentionally left untouched.
SCRAPER_LOCK_FILE = INSTANCE_CONFIG.data_dir / "status" / "active_scraper.lock"
CANCEL_FILE = STATUS_DIR / "cancel_request.json"
WORKER_STOP_GRACE_SECONDS = 8.0
MAX_UI_RESULTS = 500


st.set_page_config(page_title="Hotel Price Collector", page_icon=":hotel:", layout="wide")


BOOKING_SLOW_SOURCE = "Booking.com Playwright slow validation mode"
BROWSER_AUTOMATION_SOURCES = ["Demo", BOOKING_SLOW_SOURCE, "Expedia"]
API_PROVIDER_SOURCES = [
    "SerpApi Google Hotels",
    "SearchAPI Google Hotels",
    "DataForSEO Google Hotels",
    "Amadeus Hotel API",
    "Expedia Rapid API",
    "Custom API",
]
ALL_SOURCE_OPTIONS = BROWSER_AUTOMATION_SOURCES + ["Google Hotels API provider placeholder"] + API_PROVIDER_SOURCES
API_SOURCES = set(API_PROVIDER_SOURCES + ["Google Hotels API provider placeholder"])


@dataclass(slots=True)
class CollectionConfig:
    source: str
    city_or_region: str
    checkin_start: date
    checkin_end: date
    nights: int
    adults: int
    currency: str
    max_hotels: int
    headless: bool
    collect_all_available: bool
    max_scroll_minutes: int
    selected_star_ratings: tuple[int, ...]
    include_unknown_star_rating: bool
    debug_mode: bool
    screenshots_enabled: bool
    fast_mode: bool
    performance_mode: str
    block_images_and_fonts: bool
    test_mode: bool
    db_backend: str
    hotels_only: bool
    disable_filters_during_complete_collection: bool
    ultra_reliable_loading_mode: bool
    resume_previous_run: bool
    batch_mode: str = "single"
    custom_block_days: int = 1
    max_parallel_workers: int = 1
    rerun_batch_scope: str = "resume_incomplete"
    selected_batch_blocks: tuple[int, ...] = ()
    retry_failed_dates_automatically: bool = True
    max_retries_per_date: int = 3
    continue_if_date_fails: bool = True
    auto_export_partial_excel: bool = False
    partial_export_frequency: str = "every_25_dates"


@dataclass(slots=True)
class BatchBlock:
    block_id: int
    start_date: date
    end_date: date

    @property
    def stay_dates(self) -> list[date]:
        return date_range(self.start_date, self.end_date)


def ensure_session_state() -> None:
    defaults = {
        "results": [],
        "summary": {},
        "logs": [],
        "status_message": "Ready",
        "status": "ready",
        "progress": 0.0,
        "run_id": None,
        "excel_file_path": None,
        "csv_file_path": None,
        "csv_export_status": None,
        "csv_export_error": None,
        "csv_rows_exported": 0,
        "records_saved": 0,
        "db_ready": False,
        "db_error": None,
        "current_job": None,
        "job_started_at": None,
        "metrics": {},
        "batch_blocks": [],
        "batch_summary": {},
        "date_status_rows": [],
        "active_log_file_path": None,
        "last_preflight": {},
        "resume_preview": {},
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def initialize_database(db_backend: str) -> None:
    if st.session_state.db_ready and st.session_state.get("active_db_backend") == db_backend:
        return
    st.session_state.active_db_backend = db_backend
    if db_backend == "sqlite":
        try:
            init_db("sqlite")
            st.session_state.db_ready = True
            st.session_state.db_error = None
        except Exception as exc:
            st.session_state.db_ready = False
            st.session_state.db_error = str(exc)
        return

    if not ENV_PATH.exists():
        st.session_state.db_ready = False
        st.session_state.db_error = f"Missing .env file at {ENV_PATH.resolve()}"
        return
    missing_values = missing_env_values()
    if missing_values:
        st.session_state.db_ready = False
        st.session_state.db_error = f"Missing required .env values: {', '.join(missing_values)}"
        return
    try:
        init_db("postgres")
        st.session_state.db_ready = True
        st.session_state.db_error = None
    except Exception as exc:
        st.session_state.db_ready = False
        st.session_state.db_error = format_postgres_error(exc)


def format_postgres_error(exc: Exception) -> str:
    message = str(exc)
    if is_postgres_reachability_error(message):
        return (
            "PostgreSQL is not reachable on localhost port 5432.\n"
            "Please check that PostgreSQL is installed, running, and that the .env credentials are correct.\n\n"
            f"Exact error:\n{message}"
        )
    return message


def is_postgres_reachability_error(message: str) -> bool:
    lowered = message.lower()
    timeout_or_refused = any(token in lowered for token in ["timeout", "connection refused", "could not connect", "connection failed"])
    localhost_5432 = "localhost" in lowered or "127.0.0.1" in lowered or "::1" in lowered or "5432" in lowered
    return timeout_or_refused and localhost_5432


def missing_env_values() -> list[str]:
    if not ENV_PATH.exists():
        return ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    values = dotenv_values(ENV_PATH)
    missing: list[str] = []
    for key in ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"]:
        if not str(values.get(key) or "").strip():
            missing.append(key)
    password = str(values.get("POSTGRES_PASSWORD") or "").strip()
    if not password or password == "your_password_here":
        missing.append("POSTGRES_PASSWORD")
    return missing


def build_setup_diagnostics() -> dict[str, Any]:
    env_exists = ENV_PATH.exists()
    if env_exists:
        load_dotenv(dotenv_path=ENV_PATH, override=True)
    env_values = dotenv_values(ENV_PATH) if env_exists else {}
    diagnostics: list[dict[str, str]] = []

    def add(label: str, ok: bool, detail: str = "") -> None:
        diagnostics.append({"Item": label, "Status": "Yes" if ok else "No", "Detail": detail})

    def env_value(key: str) -> str | None:
        value = os.getenv(key) or env_values.get(key)
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip()

    host = env_value("POSTGRES_HOST")
    port = env_value("POSTGRES_PORT")
    db_name = env_value("POSTGRES_DB")
    user = env_value("POSTGRES_USER")
    password = env_value("POSTGRES_PASSWORD")
    password_ready = bool(password and password != "your_password_here")
    required_loaded = bool(host and port and db_name and user and password_ready)
    connection_ok = False
    database_exists = False
    tables_ok = False
    connection_detail = ""
    database_detail = ""
    table_detail = ""

    add("Project folder", BASE_DIR.exists(), str(BASE_DIR.resolve()))
    add("Expected .env path", True, str(ENV_PATH.resolve()))
    add(".env file exists", env_exists, str(ENV_PATH.resolve()))
    add(".env.example exists", (BASE_DIR / ".env.example").exists(), str((BASE_DIR / ".env.example").resolve()))
    add("POSTGRES_HOST loaded", bool(host), host or "Set POSTGRES_HOST in .env")
    add("POSTGRES_PORT loaded", bool(port), port or "Set POSTGRES_PORT in .env")
    add("POSTGRES_DB loaded", bool(db_name), db_name or "Set POSTGRES_DB in .env")
    add("POSTGRES_USER loaded", bool(user), user or "Set POSTGRES_USER in .env")
    add("PostgreSQL password loaded", password_ready, "Loaded" if password_ready else "Set POSTGRES_PASSWORD in .env; replace your_password_here")

    if env_exists and required_loaded:
        try:
            with get_connection() as conn:
                connection_ok = True
                database_exists = True
                connection_detail = f"Connected to {host}:{port}/{db_name} as {user}"
                database_detail = f"Target database exists: {db_name}"
                with conn.cursor() as cur:
                    cur.execute("select to_regclass('public.collection_runs'), to_regclass('public.hotel_price_results')")
                    collection_runs, hotel_results = cur.fetchone()
                    tables_ok = bool(collection_runs and hotel_results)
                    table_detail = "collection_runs and hotel_price_results found" if tables_ok else "Run database initialization after connection succeeds"
        except Exception as exc:
            connection_detail = format_postgres_error(exc)
            if is_postgres_reachability_error(str(exc)):
                database_detail = "Could not check database existence because PostgreSQL is not reachable on localhost port 5432."
            else:
                try:
                    with get_connection(database_name="postgres") as conn:
                        with conn.cursor() as cur:
                            cur.execute("select exists(select 1 from pg_database where datname = %s)", (db_name,))
                            database_exists = bool(cur.fetchone()[0])
                            database_detail = f"Database {db_name!r} {'exists' if database_exists else 'does not exist'}"
                except Exception as database_exc:
                    database_detail = f"Could not check database existence: {format_postgres_error(database_exc)}"
            table_detail = "Skipped because target PostgreSQL connection failed"
    elif env_exists:
        connection_detail = "Skipped because one or more required PostgreSQL .env values are missing"
        database_detail = "Skipped because one or more required PostgreSQL .env values are missing"
        table_detail = "Skipped because PostgreSQL connection was not tested"
    else:
        connection_detail = f"Skipped because .env is missing at {ENV_PATH.resolve()}"
        database_detail = "Skipped because .env is missing"
        table_detail = "Skipped because .env is missing"

    add("PostgreSQL connection successful", connection_ok, connection_detail)
    add("PostgreSQL database exists", database_exists, database_detail)
    add("Required database tables exist", tables_ok, table_detail)
    add("exports folder exists", EXPORT_DIR.exists(), str(EXPORT_DIR.resolve()))
    add("screenshots folder exists", SCREENSHOT_DIR.exists(), str(SCREENSHOT_DIR.resolve()))
    add("debug folder exists", DEBUG_DIR.exists(), str(DEBUG_DIR.resolve()))

    return {
        "rows": diagnostics,
        "env_exists": env_exists,
        "connection_ok": connection_ok,
        "tables_ok": tables_ok,
        "database_exists": database_exists,
        "expected_env_path": ENV_PATH.resolve(),
        "project_folder": BASE_DIR.resolve(),
        "connection_detail": connection_detail,
    }


def render_setup_diagnostics(diagnostics: dict[str, Any]) -> None:
    st.subheader("Setup Diagnostics")
    st.caption("Expected .env path")
    st.code(str(diagnostics["expected_env_path"]), language="text")
    st.dataframe(text_rows_dataframe(diagnostics["rows"]), use_container_width=True, hide_index=True)

    if st.button("Test PostgreSQL connection"):
        try:
            if not ENV_PATH.exists():
                raise RuntimeError(f"Missing .env file at {ENV_PATH.resolve()}")
            missing_values = missing_env_values()
            if missing_values:
                raise RuntimeError(f"Missing required .env values: {', '.join(missing_values)}")
            init_db("postgres")
            st.session_state.db_ready = True
            st.session_state.active_db_backend = "postgres"
            st.session_state.db_error = None
            st.success("Connection successful")
            st.rerun()
        except Exception as exc:
            st.session_state.db_ready = False
            st.session_state.db_error = format_postgres_error(exc)
            st.error(
                "Connection failed. PostgreSQL may not be installed, not running, or the .env credentials may be incorrect.\n\n"
                f"{st.session_state.db_error}"
            )

    if not diagnostics["env_exists"]:
        st.warning("The scraper has not started yet because PostgreSQL setup is incomplete.")
        if st.button("Create .env from .env.example", type="primary"):
            if ENV_PATH.exists():
                st.info(f".env already exists at {ENV_PATH.resolve()}")
            elif not (BASE_DIR / ".env.example").exists():
                st.error(f".env.example is missing at {(BASE_DIR / '.env.example').resolve()}")
            else:
                copyfile(BASE_DIR / ".env.example", ENV_PATH)
                st.success(".env created. Please open it and fill in your PostgreSQL password.")
                st.rerun()
    elif not diagnostics["connection_ok"]:
        st.warning("The scraper has not started yet because PostgreSQL setup is incomplete.")

    with st.expander("Ubuntu/Linux setup commands", expanded=not diagnostics["connection_ok"]):
        st.code(
            f'cd "{BASE_DIR.resolve()}"\n'
            "cp .env.example .env\n"
            "${EDITOR:-nano} .env\n"
            "python3 -m venv .venv\n"
            "source .venv/bin/activate\n"
            "pip install -r requirements.txt\n"
            "python -m playwright install --with-deps chromium\n"
            "streamlit run app.py",
            language="bash",
        )


def get_collector(source: str):
    if source == "Booking.com":
        source = BOOKING_SLOW_SOURCE
    if source == "Demo":
        return DemoCollector()
    if source == BOOKING_SLOW_SOURCE:
        return BookingPlaywrightCollector()
    if source == "Expedia":
        return ExpediaPlaywrightCollector()
    if source == "Google Hotels API provider placeholder":
        return GoogleHotelsApiPlaceholderCollector()
    if source == "SerpApi Google Hotels":
        return SerpApiGoogleHotelsCollector()
    if source == "SearchAPI Google Hotels":
        return SearchApiGoogleHotelsCollector()
    if source == "DataForSEO Google Hotels":
        return DataForSeoGoogleHotelsCollector()
    if source == "Amadeus Hotel API":
        return AmadeusHotelsCollector()
    if source == "Expedia Rapid API":
        return ExpediaRapidCollector()
    return CustomApiCollector()


def is_booking_source(source: str) -> bool:
    return source in {"Booking.com", BOOKING_SLOW_SOURCE}


def is_browser_source(source: str) -> bool:
    return source in {"Demo", "Booking.com", BOOKING_SLOW_SOURCE, "Expedia"}


def is_api_source(source: str) -> bool:
    return source in API_SOURCES


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        return [start]
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _checkpoint_signature(config: CollectionConfig) -> dict[str, Any]:
    return {
        "source": config.source,
        "city_or_region": config.city_or_region.strip().lower(),
        "checkin_start": config.checkin_start.isoformat(),
        "checkin_end": config.checkin_end.isoformat(),
        "nights": config.nights,
        "adults": config.adults,
        "currency": config.currency,
        "collect_all_available": config.collect_all_available,
        "selected_star_ratings": list(config.selected_star_ratings),
        "include_unknown_star_rating": config.include_unknown_star_rating,
        "db_backend": config.db_backend,
        "hotels_only": config.hotels_only,
        "disable_filters_during_complete_collection": config.disable_filters_during_complete_collection,
    }


def _checkpoint_path(config: CollectionConfig) -> Path:
    safe_source = "".join(ch if ch.isalnum() else "_" for ch in config.source.lower()).strip("_")
    safe_city = "".join(ch if ch.isalnum() else "_" for ch in config.city_or_region.lower()).strip("_")[:60] or "city"
    return CHECKPOINT_DIR / f"{safe_source}_{safe_city}_{config.checkin_start.isoformat()}_{config.checkin_end.isoformat()}_{config.nights}n_resume.json"


def load_resume_checkpoint(config: CollectionConfig) -> dict[str, Any] | None:
    path = _checkpoint_path(config)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("signature") != _checkpoint_signature(config):
        return None
    return payload


def save_resume_checkpoint(config: CollectionConfig, payload: dict[str, Any]) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    payload["signature"] = _checkpoint_signature(config)
    payload["checkpoint_path"] = str(_checkpoint_path(config).resolve())
    payload["updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    path = _checkpoint_path(config)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def mark_checkpoint_date(config: CollectionConfig, payload: dict[str, Any], stay_date: date, status: str, records_saved: int = 0, error: str | None = None) -> Path:
    dates = payload.setdefault("dates", {})
    dates[stay_date.isoformat()] = {
        "status": status,
        "records_saved": records_saved,
        "error": error,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    return save_resume_checkpoint(config, payload)


def _db_write_lock_for(config: CollectionConfig):
    return SQLITE_WRITE_LOCK if config.db_backend == "sqlite" else _NullLock()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _batch_signature(config: CollectionConfig, blocks: list[BatchBlock]) -> dict[str, Any]:
    return {
        **_checkpoint_signature(config),
        "batch_mode": config.batch_mode,
        "custom_block_days": config.custom_block_days,
        "max_parallel_workers": config.max_parallel_workers,
        "blocks": [(block.start_date.isoformat(), block.end_date.isoformat()) for block in blocks],
    }


def _batch_checkpoint_path(config: CollectionConfig) -> Path:
    safe_source = safe_filename(config.source.lower().replace(".com", ""))
    safe_city = safe_filename(config.city_or_region.lower())[:60] or "city"
    return CHECKPOINT_DIR / f"{safe_source}_{safe_city}_{config.checkin_start.isoformat()}_{config.checkin_end.isoformat()}_{config.batch_mode}_{config.nights}n_batch_resume.json"


def load_batch_checkpoint(config: CollectionConfig, blocks: list[BatchBlock]) -> dict[str, Any] | None:
    path = _batch_checkpoint_path(config)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("signature") != _batch_signature(config, blocks):
        return None
    return payload


def save_batch_checkpoint(config: CollectionConfig, blocks: list[BatchBlock], payload: dict[str, Any]) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    payload["signature"] = _batch_signature(config, blocks)
    payload["checkpoint_path"] = str(_batch_checkpoint_path(config).resolve())
    payload["updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    path = _batch_checkpoint_path(config)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def block_status_row(block: BatchBlock, status: str = "pending", **overrides: Any) -> dict[str, Any]:
    dates = block.stay_dates
    row = {
        "block_id": block.block_id,
        "date range": f"{block.start_date.isoformat()} to {block.end_date.isoformat()}",
        "status": status,
        "current stay date": "-",
        "hotels collected": 0,
        "completion percentage": 0.0,
        "runtime": "-",
        "stop reason": "-",
        "Excel file path": "-",
        "stay_dates": len(dates),
    }
    row.update(overrides)
    return row


def write_block_debug_file(block_folder: Path, payload: dict[str, Any]) -> Path:
    path = block_folder / "block_debug.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path.resolve()


def append_block_log(log_file: Path, message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def build_startup_check() -> dict[str, Any]:
    return {
        "instance_id": INSTANCE_CONFIG.instance_id,
        "instance_name": INSTANCE_CONFIG.instance_name,
        "port": INSTANCE_CONFIG.instance_port,
        "data_dir": str(INSTANCE_CONFIG.data_dir.resolve()),
        "sqlite_path": str(SQLITE_DB_PATH.resolve()),
        "browser_profile_path": str(BROWSER_PROFILE_DIR.resolve()),
        "log_dir": str(LOG_DIR.resolve()),
        "export_dir": str(EXPORT_DIR.resolve()),
        "screenshot_dir": str(SCREENSHOT_DIR.resolve()),
        "debug_dir": str(DEBUG_DIR.resolve()),
        "status_dir": str(STATUS_DIR.resolve()),
        "partial_dir": str(PARTIAL_DIR.resolve()),
        "current_process_id": os.getpid(),
        "python_executable": sys.executable,
        "working_directory": str(Path.cwd().resolve()),
        "timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def write_startup_check() -> dict[str, Any]:
    payload = build_startup_check()
    write_json_file(DEBUG_DIR / "instance_startup_check.json", payload)
    return payload


def run_headless_smoke_test() -> dict[str, Any]:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now()
    screenshot_path = SCREENSHOT_DIR / f"headless_smoke_booking_{started_at.strftime('%Y%m%d_%H%M%S')}.png"
    payload: dict[str, Any] = {
        "timestamp": started_at.isoformat(sep=" ", timespec="seconds"),
        "headless": True,
        "url": "https://www.booking.com",
        "screenshot_path": str(screenshot_path.resolve()),
        "python_executable": sys.executable,
        "status": "running",
        "page_title": None,
        "final_url": None,
        "error_message": None,
    }
    browser = context = page = None
    try:
        with sync_playwright() as playwright:
            profile_dir = BROWSER_PROFILE_DIR / "diagnostics" / f"headless_smoke_{uuid4().hex}"
            browser, context, implementation = launch_managed_chromium_context(
                playwright,
                headless=True,
                profile_dir=profile_dir,
                args=["--disable-dev-shm-usage"],
                viewport={"width": 1366, "height": 900},
            )
            payload["browser_implementation"] = implementation
            payload["profile_dir"] = str(profile_dir.resolve())
            page = context.new_page()
            page.goto("https://www.booking.com", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            payload["page_title"] = page.title()
            payload["final_url"] = page.url
            safe_screenshot(page, screenshot_path, "headless smoke", full_page=False)
        payload["status"] = "completed"
    except Exception as exc:
        payload["status"] = "failed"
        payload["error_message"] = (
            f"{exc}\n\nPlaywright Chromium may not be installed. Run: python -m playwright install --with-deps chromium\n"
            f"Streamlit Python executable: {sys.executable}"
        )
    finally:
        safe_close_browser(browser=browser, context=context, page=page)
    write_json_file(DEBUG_DIR / "headless_smoke_test_latest.json", payload)
    return payload


def latest_log_file() -> Path | None:
    if not LOG_DIR.exists():
        return None
    files = [path for path in LOG_DIR.iterdir() if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def read_json_path(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_last_error() -> dict[str, Any]:
    return read_json_path(STATUS_DIR / "last_error.json")


def clear_last_error(reason: str) -> None:
    write_json_file(
        STATUS_DIR / "last_error.json",
        {
            "timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "job_id": None,
            "instance_id": INSTANCE_CONFIG.instance_id,
            "current_date": None,
            "current_step": reason,
            "status": "cleared",
            "error_type": None,
            "error_message": None,
            "traceback_file": None,
            "log_file": None,
        },
    )


def reset_period_1_running_state() -> None:
    for path in [STATUS_FILE, HEARTBEAT_FILE]:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            st.warning(f"Could not clear {path}: {exc}")
    st.session_state.current_job = None
    st.session_state.status = "ready"
    st.session_state.status_message = "Instance running state reset. Scraped data, exports, checkpoints, logs, and debug files were not deleted."
    st.session_state.progress = 0.0
    st.session_state.metrics = {}


def record_fatal_error(
    *,
    exc: BaseException,
    job_id: str | None,
    status: str,
    current_step: str,
    current_date: str | None = None,
    log_file: str | None = None,
    queue: Queue | None = None,
) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    traceback_text = traceback.format_exc()
    if traceback_text.strip() == "NoneType: None":
        traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    traceback_file = LOG_DIR / f"fatal_error_{timestamp}.txt"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    traceback_file.write_text(
        "\n".join(
            [
                f"timestamp: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
                f"job_id: {job_id or ''}",
                f"instance_id: {INSTANCE_CONFIG.instance_id}",
                f"status: {status}",
                f"current_step: {current_step}",
                f"current_date: {current_date or ''}",
                f"error: {exc}",
                "",
                traceback_text,
            ]
        ),
        encoding="utf-8",
    )
    payload = {
        "timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "job_id": job_id,
        "instance_id": INSTANCE_CONFIG.instance_id,
        "current_date": current_date,
        "current_step": current_step,
        "status": status,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback_file": str(traceback_file.resolve()),
        "log_file": log_file,
        "traceback": traceback_text,
    }
    write_json_file(STATUS_DIR / "last_error.json", payload)
    update_status_file(
        job_id=job_id,
        status=status,
        current_message=str(exc),
        last_error=str(exc),
        last_traceback_file=str(traceback_file.resolve()),
        current_step=current_step,
        current_date=current_date,
        log_file=log_file,
    )
    if queue is not None:
        put_log(queue, f"Fatal error saved: {traceback_file.resolve()}")
        queue.put(("failed", {"status": status, "error": str(exc)}))
    return payload


def read_current_status_file() -> dict[str, Any]:
    if not STATUS_FILE.exists():
        return {}
    try:
        payload = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def maybe_mark_stale_status_file(status: dict[str, Any]) -> dict[str, Any]:
    if not status or status.get("status") not in {"starting", "running"}:
        return status
    last_updated_text = status.get("last_updated_at")
    if not last_updated_text:
        return status
    try:
        last_updated_at = datetime.fromisoformat(str(last_updated_text))
    except ValueError:
        return status
    stale_seconds = (datetime.now() - last_updated_at).total_seconds()
    if stale_seconds < ACTIVE_STATUS_STALE_AFTER_SECONDS:
        return status

    stale_message = (
        "Previous collection job stopped updating and was marked stale. "
        "You can start a new collection."
    )
    detected_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    status = {
        **status,
        "status": "failed",
        "current_message": stale_message,
        "last_error": stale_message,
        "stale_after_seconds": ACTIVE_STATUS_STALE_AFTER_SECONDS,
        "stale_detected_at": detected_at,
        "last_updated_at": detected_at,
    }
    write_json_file(STATUS_FILE, status)
    return status


def build_diagnostic_summary() -> str:
    status = maybe_mark_stale_status_file(read_current_status_file())
    lines = [
        f"instance_id: {INSTANCE_CONFIG.instance_id}",
        f"port: {INSTANCE_CONFIG.instance_port}",
        f"data_dir: {INSTANCE_CONFIG.data_dir.resolve()}",
        f"status_file: {STATUS_FILE.resolve()}",
        f"log_file: {st.session_state.active_log_file_path or status.get('log_file') or ''}",
        f"current_status: {status.get('status') or st.session_state.status}",
        f"last_error: {status.get('last_error') or ''}",
        f"price_rows: {status.get('rows_with_parsed_price') or status.get('rows_with_raw_price') or ''}",
        f"rows_missing_price: {status.get('rows_missing_price') or ''}",
        f"load_more_clicks: {status.get('load_more_click_count') or status.get('load_more_clicks') or ''}",
    ]
    return "\n".join(lines)


def update_status_file(**updates: Any) -> None:
    payload = read_current_status_file()
    payload.update(updates)
    payload["last_updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    write_json_file(STATUS_FILE, payload)


def _preflight_check_playwright_chromium() -> tuple[bool, str]:
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
        if executable and Path(executable).exists():
            return True, str(executable)
        return False, f"Chromium executable not found: {executable}"
    except Exception as exc:
        return False, str(exc)


def run_start_preflight(config: CollectionConfig) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    def require_dir(name: str, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        add(name, path.exists() and path.is_dir(), str(path.resolve()))

    try:
        require_dir("Instance data folder exists", INSTANCE_CONFIG.data_dir)
        add("SQLite database path is valid", SQLITE_DB_PATH.parent.exists(), str(SQLITE_DB_PATH.resolve()))
        require_dir("logs folder exists", LOG_DIR)
        require_dir("status folder exists", STATUS_DIR)
        require_dir("checkpoints folder exists", CHECKPOINT_DIR)
        require_dir("exports folder exists", EXPORT_DIR)
        require_dir("browser_profile folder exists", BROWSER_PROFILE_DIR)
        require_dir("partial folder exists", PARTIAL_DIR)
        options = CollectorOptions(**collector_options_kwargs(config, Event(), attempt=1))
        add("CollectorOptions can be instantiated", isinstance(options, CollectorOptions), "CollectorOptions OK")
        chromium_ok, chromium_detail = _preflight_check_playwright_chromium()
        add("Playwright chromium is installed", chromium_ok, chromium_detail)
        collector = get_collector(config.source)
        add("Booking.com collector can be created", isinstance(collector, BookingPlaywrightCollector) if is_booking_source(config.source) else True, type(collector).__name__)
    except Exception as exc:
        add("Preflight exception", False, str(exc))

    failed = [row for row in checks if not row["ok"]]
    payload = {
        "timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "first_failed_check": failed[0] if failed else None,
    }
    write_json_file(DEBUG_DIR / "instance_preflight_latest.json", payload)
    if failed:
        message = f"Preflight failed: {failed[0]['check']} - {failed[0]['detail']}"
        write_json_file(
            STATUS_DIR / "last_error.json",
            {
                "timestamp": payload["timestamp"],
                "job_id": None,
                "instance_id": INSTANCE_CONFIG.instance_id,
                "current_date": None,
                "current_step": "preflight",
                "status": "preflight_failed",
                "error_type": "PreflightError",
                "error_message": message,
                "traceback_file": None,
                "log_file": None,
                "checks": checks,
            },
        )
        update_status_file(status="preflight_failed", current_message=message, last_error=message, current_step="preflight")
    else:
        clear_last_error("preflight_passed")
    return payload


def run_background_job_with_fatal_guard(
    runner,
    config: CollectionConfig,
    stop_event: Any,
    queue: Any,
    job_id: str,
    log_file: str,
) -> None:
    queue.job_id = job_id
    queue.instance_log_file = log_file
    current_step = "background_job_start"
    lock = SingleScraperLock(SCRAPER_LOCK_FILE)

    def cleanup_log(message: str) -> None:
        put_log(queue, message)

    try:
        lock.acquire(job_id)
        guard = ResourceGuard(INSTANCE_CONFIG.data_dir, owner_pid=os.getpid())
        queue.resource_guard = guard
        initial = guard.check(starting=True, force=True)
        update_status_file(job_id=job_id, status="running", current_step=current_step, log_file=log_file)
        put_metrics(queue, {**initial.as_dict(), "resource_guard_level": guard.last_level})
        runner(config, stop_event, queue)
    except ScraperAlreadyRunning as exc:
        record_fatal_error(
            exc=exc,
            job_id=job_id,
            status="refused_second_scraper",
            current_step=current_step,
            current_date=None,
            log_file=log_file,
            queue=queue,
        )
    except ResourceLimitExceeded as exc:
        update_status_file(
            job_id=job_id,
            status="stopped_resource_limit",
            current_step=current_step,
            current_message=str(exc),
            last_error=str(exc),
            **exc.snapshot.as_dict(),
        )
        put_metrics(queue, {**exc.snapshot.as_dict(), "resource_guard_level": "emergency"})
        queue.put(("failed", {"status": "stopped_resource_limit", "error": str(exc)}))
    except Exception as exc:
        status = "fatal_startup_error"
        record_fatal_error(
            exc=exc,
            job_id=job_id,
            status=status,
            current_step=current_step,
            current_date=None,
            log_file=log_file,
            queue=queue,
        )
    finally:
        cleanup_owned_browser_processes(os.getpid(), cleanup_log)
        lock.release()


def build_resume_preview(config: CollectionConfig) -> dict[str, Any]:
    planned_dates = date_range(config.checkin_start, config.checkin_end)
    checkpoint = load_checkpoint(_checkpoint_path(config), _checkpoint_signature(config)) or {}
    db_counts: dict[str, int] = {}
    run_id = checkpoint.get("run_id")
    try:
        if run_id:
            db_counts = result_date_counts_by_run_id(int(run_id), backend=config.db_backend)
        else:
            matched_run_id = matching_saved_run_id(config)
            if matched_run_id is not None:
                db_counts = result_date_counts_by_run_id(matched_run_id, backend=config.db_backend)
    except Exception:
        db_counts = {}
    completed = completed_dates_from_checkpoint_and_db(checkpoint, db_counts) if checkpoint else set(db_counts)
    remaining = [value for value in planned_dates if value.isoformat() not in completed]
    return {
        "Total planned dates": len(planned_dates),
        "Already completed dates": len(completed),
        "Remaining dates": len(remaining),
        "First remaining date": remaining[0].isoformat() if remaining else "none",
        "Existing saved rows": sum(db_counts.values()),
    }


def price_validation_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    raw_count = sum(1 for row in results if row.get("raw_price_text"))
    parsed_values = [row.get("cheapest_price_total") or row.get("parsed_price") for row in results]
    parsed_numbers: list[float] = []
    for value in parsed_values:
        if value is None or value == "":
            continue
        try:
            parsed_numbers.append(float(value))
        except (TypeError, ValueError):
            continue
    missing = max(len(results) - len(parsed_numbers), 0)
    return {
        "rows_with_raw_price": raw_count,
        "rows_with_parsed_price": len(parsed_numbers),
        "rows_missing_price": missing,
        "lowest_price": min(parsed_numbers) if parsed_numbers else None,
        "highest_price": max(parsed_numbers) if parsed_numbers else None,
        "price_missing_ratio": round(missing / max(len(results), 1), 4),
    }


def write_price_pipeline_debug(stay_date: date, results: list[dict[str, Any]]) -> Path:
    rows = []
    for row in results[:30]:
        rows.append(
            {
                "hotel_name": row.get("hotel_name"),
                "raw_price_text": row.get("raw_price_text"),
                "parsed_price": row.get("parsed_price"),
                "saved_price": row.get("cheapest_price_total"),
                "exported_price": row.get("cheapest_price_total"),
            }
        )
    path = DEBUG_DIR / f"price_pipeline_debug_{stay_date.isoformat()}.json"
    write_json_file(path, {"stay_date": stay_date.isoformat(), "rows": rows, "summary": price_validation_summary(results)})
    return path.resolve()


def export_final_csv(
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    session_id: Any,
    log_callback: Any = None,
    stream_sqlite_run_id: int | None = None,
) -> dict[str, Any]:
    summary["scrape_session_id"] = session_id
    summary["instance_id"] = INSTANCE_CONFIG.instance_id
    row_count = count_results_by_run_id(stream_sqlite_run_id, backend="sqlite") if stream_sqlite_run_id is not None else len(results)
    try:
        if stream_sqlite_run_id is not None:
            csv_path = export_sqlite_run_to_csv(
                stream_sqlite_run_id,
                summary,
                EXPORT_DIR,
                instance_id=INSTANCE_CONFIG.instance_id,
            )
        else:
            csv_path = export_results_to_csv(
                results,
                summary,
                EXPORT_DIR,
                instance_id=INSTANCE_CONFIG.instance_id,
                session_id=session_id,
            )
        payload = {
            "csv_file_path": str(csv_path),
            "csv_export_status": "succeeded",
            "csv_export_error": None,
            "csv_export_traceback_file": None,
            "csv_rows_exported": row_count,
        }
        summary["final CSV export path"] = str(csv_path)
        summary["final CSV export status"] = "succeeded"
        summary["final CSV rows exported"] = row_count
        if log_callback:
            log_callback(f"Final CSV exported atomically: {csv_path}")
    except Exception as exc:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        traceback_file = LOG_DIR / f"csv_export_error_{timestamp}.txt"
        traceback_file.write_text(
            "\n".join(
                [
                    f"timestamp: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
                    f"instance_id: {INSTANCE_CONFIG.instance_id}",
                    f"session_id: {session_id}",
                    f"error: {exc}",
                    "",
                    traceback.format_exc(),
                ]
            ),
            encoding="utf-8",
        )
        payload = {
            "csv_file_path": None,
            "csv_export_status": "failed",
            "csv_export_error": str(exc),
            "csv_export_traceback_file": str(traceback_file.resolve()),
            "csv_rows_exported": row_count,
        }
        summary["final CSV export path"] = None
        summary["final CSV export status"] = "failed"
        summary["final CSV export error"] = str(exc)
        summary["final CSV export traceback file"] = str(traceback_file.resolve())
        summary["final CSV rows exported"] = row_count
        if log_callback:
            log_callback(f"Final CSV export failed: {exc}. Traceback: {traceback_file.resolve()}")
    try:
        update_status_file(**payload)
    except Exception:
        pass
    return payload


def build_batch_blocks(start_date: date, end_date: date, batch_mode: str, custom_block_days: int = 1) -> list[BatchBlock]:
    if end_date < start_date:
        end_date = start_date
    if batch_mode == "single":
        ranges = [(start_date, end_date)]
    elif batch_mode == "weekly":
        ranges = split_date_range_into_blocks(start_date, end_date, "weekly")
    elif batch_mode == "monthly":
        ranges = split_date_range_into_blocks(start_date, end_date, "monthly")
    else:
        ranges = []
        cursor = start_date
        block_days = max(1, int(custom_block_days))
        while cursor <= end_date:
            block_end = min(cursor + timedelta(days=block_days - 1), end_date)
            ranges.append((cursor, block_end))
            cursor = block_end + timedelta(days=1)
    return [BatchBlock(index, block_start, block_end) for index, (block_start, block_end) in enumerate(ranges, start=1)]


def parse_selected_block_ids(text: str) -> tuple[int, ...]:
    selected: set[int] = set()
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start = int(start_text.strip())
                end = int(end_text.strip())
            except ValueError:
                continue
            selected.update(range(min(start, end), max(start, end) + 1))
        else:
            try:
                selected.add(int(part))
            except ValueError:
                continue
    return tuple(sorted(value for value in selected if value > 0))


def as_display_df(results: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(results)
    if df.empty:
        return df
    for column in ["cheapest_price_total", "review_score", "star_rating"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def render_summary(summary: dict[str, Any]) -> None:
    if not summary:
        return
    if summary.get("price warning"):
        st.warning(str(summary["price warning"]))
    first_row = st.columns(5)
    first_row[0].metric("Total hotels", summary.get("total hotels collected", 0))
    first_row[1].metric("Lowest price", _format_money(summary.get("lowest price"), summary.get("currency")))
    first_row[2].metric("Highest price", _format_money(summary.get("highest price"), summary.get("currency")))
    first_row[3].metric("Average price", _format_money(summary.get("average price"), summary.get("currency")))
    first_row[4].metric("Average review", _format_number(summary.get("average review score")))

    second_row = st.columns(3)
    second_row[0].metric("Successful records", summary.get("number of successful records", 0))
    second_row[1].metric("Failed records", summary.get("number of failed records", 0))
    star_counts = summary.get("hotels by star rating") or {}
    second_row[2].write("Hotels by star rating")
    second_row[2].dataframe(pd.DataFrame([{"Stars": key, "Hotels": value} for key, value in star_counts.items()]), hide_index=True, use_container_width=True)


def _format_money(value: Any, currency: str | None) -> str:
    if value is None:
        return "-"
    return f"{currency or ''} {float(value):,.2f}".strip()


def _format_number(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(BASE_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def text_rows_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([{key: "" if value is None else str(value) for key, value in row.items()} for row in rows])


def render_instance_info() -> None:
    st.subheader("Instance")
    cols = st.columns(4)
    cols[0].metric("Instance ID", INSTANCE_CONFIG.instance_id)
    cols[1].metric("Port", INSTANCE_CONFIG.instance_port)
    cols[2].metric("Mode", "Multi Instance" if INSTANCE_CONFIG.active else "Default")
    cols[3].metric("Source", INSTANCE_CONFIG.source)
    info_rows = [
        {"Item": "Instance name", "Value": INSTANCE_CONFIG.instance_name},
        {"Item": "Data folder", "Value": str(INSTANCE_CONFIG.data_dir.resolve())},
        {"Item": "SQLite database path", "Value": str(SQLITE_DB_PATH.resolve())},
        {"Item": "Export folder", "Value": str(EXPORT_DIR.resolve())},
        {"Item": "Screenshot folder", "Value": str(SCREENSHOT_DIR.resolve())},
        {"Item": "Debug folder", "Value": str(DEBUG_DIR.resolve())},
        {"Item": "Log folder", "Value": str(LOG_DIR.resolve())},
        {"Item": "Browser profile folder", "Value": str(BROWSER_PROFILE_DIR.resolve())},
        {"Item": "Partial folder", "Value": str(PARTIAL_DIR.resolve())},
        {"Item": "Status folder", "Value": str(STATUS_DIR.resolve())},
        {"Item": "Status file", "Value": str(STATUS_FILE.resolve())},
        {"Item": "Startup check file", "Value": str((DEBUG_DIR / "instance_startup_check.json").resolve())},
        {
            "Item": "Configured date range",
            "Value": (
                f"{INSTANCE_CONFIG.start_date.isoformat()} to {INSTANCE_CONFIG.end_date.isoformat()}"
                if INSTANCE_CONFIG.start_date and INSTANCE_CONFIG.end_date
                else "Not configured"
            ),
        },
        {"Item": "Active log file", "Value": st.session_state.active_log_file_path or "No active run log yet"},
    ]
    st.dataframe(text_rows_dataframe(info_rows), use_container_width=True, hide_index=True)
    st.warning(
        "This 8 GB tower is limited to one scraper worker and one scraper browser. "
        "A second scraper is refused by the host-wide safety lock."
    )


def render_instance_error_panel() -> None:
    status = maybe_mark_stale_status_file(read_current_status_file())
    last_error = read_last_error()
    heartbeat = read_json_path(HEARTBEAT_FILE)
    latest_log = latest_log_file()
    current_status = status.get("status") or st.session_state.status
    is_running = bool(st.session_state.current_job) or current_status in {"starting", "running"}
    current_status_text = str(current_status or "")
    status_is_error = (
        bool(status.get("last_error"))
        or current_status_text.startswith("fatal")
        or current_status_text in {"failed", "preflight_failed", "blocked_or_access_restricted"}
    )
    error_message = status.get("last_error") or (last_error.get("error_message") if status_is_error else None)
    traceback_file = status.get("last_traceback_file") or last_error.get("traceback_file")

    if status_is_error and (error_message or current_status_text.startswith("fatal") or current_status == "preflight_failed"):
        st.error(f"Last error: {error_message or current_status}")
        if traceback_file:
            st.code(str(traceback_file), language="text")

    with st.expander("Instance job health", expanded=True):
        rows = [
            {"Item": "Current job status", "Value": current_status or "unknown"},
            {"Item": "Is job running", "Value": "yes" if is_running else "no"},
            {"Item": "Last heartbeat time", "Value": heartbeat.get("last_updated_at") or "-"},
            {"Item": "Latest log file path", "Value": str(latest_log.resolve()) if latest_log else "-"},
            {"Item": "Last traceback file", "Value": str(traceback_file or "-")},
            {"Item": "Current step", "Value": status.get("current_step") or last_error.get("current_step") or "-"},
        ]
        st.dataframe(text_rows_dataframe(rows), use_container_width=True, hide_index=True)
        if last_error:
            st.caption(str((STATUS_DIR / "last_error.json").resolve()))
            st.json(last_error)


def reset_current_results() -> None:
    st.session_state.results = []
    st.session_state.summary = {}
    st.session_state.logs = []
    st.session_state.status_message = "Ready"
    st.session_state.status = "ready"
    st.session_state.progress = 0.0
    st.session_state.run_id = None
    st.session_state.excel_file_path = None
    st.session_state.csv_file_path = None
    st.session_state.csv_export_status = None
    st.session_state.csv_export_error = None
    st.session_state.csv_rows_exported = 0
    st.session_state.records_saved = 0
    st.session_state.metrics = {}
    st.session_state.current_job = None
    st.session_state.job_started_at = None
    st.session_state.batch_blocks = []
    st.session_state.batch_summary = {}
    st.session_state.date_status_rows = []
    st.session_state.active_log_file_path = None
    st.session_state.resume_preview = {}


def latest_export_download_button(path_text: str | None) -> None:
    if not path_text:
        return
    file_path = Path(path_text)
    if file_path.exists():
        st.download_button(
            "Download Excel file",
            data=file_path.read_bytes(),
            file_name=file_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def put_log(queue: Queue, message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    log_file = getattr(queue, "instance_log_file", None)
    if log_file:
        try:
            with Path(log_file).open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except OSError:
            pass
    queue.put(("log", line))


def put_progress(queue: Queue, value: float, message: str) -> None:
    queue.put(("progress", max(0.0, min(1.0, float(value))), message))


def put_metrics(queue: Queue, metrics: dict[str, Any]) -> None:
    queue.put(("metrics", metrics))


def filter_results_by_star_rating(results: list[dict[str, Any]], options: CollectorOptions, log_queue: Queue) -> list[dict[str, Any]]:
    selected = {int(value) for value in options.selected_star_ratings}
    before_count = len(results)
    known_count = sum(1 for row in results if row.get("star_rating") is not None)
    unknown_count = before_count - known_count
    kept: list[dict[str, Any]] = []
    skipped = 0
    for row in results:
        star = row.get("star_rating")
        if star is None:
            if options.include_unknown_star_rating:
                kept.append(row)
            else:
                skipped += 1
            continue
        if int(float(star)) in selected:
            kept.append(row)
        else:
            skipped += 1
    put_log(log_queue, f"Hotels extracted before filtering: {before_count}")
    put_log(log_queue, f"Hotels with known star rating: {known_count}")
    put_log(log_queue, f"Hotels with unknown star rating: {unknown_count}")
    put_log(log_queue, f"Hotels skipped because star rating did not match: {skipped}")
    put_log(log_queue, f"Hotels kept after filtering: {len(kept)}")
    options.stats["hotels_with_known_star_rating"] = known_count
    options.stats["hotels_with_unknown_star_rating"] = unknown_count
    options.stats["hotels_removed_by_star_filter"] = skipped
    options.stats["hotels_kept_after_star_filter"] = len(kept)
    if before_count and not kept:
        put_log(log_queue, "WARNING: All hotels were removed by the star rating filter. Try enabling Include hotels with unknown star rating or select All star ratings.")
    return kept


def terminal_status_for_results(results: list[dict[str, Any]], stopped: bool, error_message: str | None) -> str:
    if stopped:
        return "stopped_by_user"
    if error_message and "blocked_or_access_restricted" in error_message:
        return "completed_with_partial_results" if results else "blocked_or_access_restricted"
    if error_message:
        return "completed_with_partial_results" if results else "failed"
    real_hotels = [row for row in results if row.get("hotel_name") and row.get("collection_status") == "success"]
    return "completed" if real_hotels else "completed_with_zero_results"


def _collector_summary_metadata(options: CollectorOptions) -> dict[str, Any]:
    stats = options.stats
    return {
        "performance mode": options.performance_mode,
        "request blocking enabled": bool(options.block_images_and_fonts),
        "hotels only filter enabled": bool(options.hotels_only),
        "records removed by hotels only filter": stats.get("hotels_only_removed", 0),
        "total raw records extracted": stats.get("raw_records_extracted", 0),
        "total records after hotels only filter": stats.get("records_after_hotels_only_filter", 0),
        "load more button clicks": stats.get("load_more_clicks", 0),
        "load more button seen count": stats.get("load_more_button_seen_count", 0),
        "load more click failures": stats.get("load_more_click_fail_count", 0),
        "last load more error": stats.get("last_load_more_error"),
        "last load more button text": stats.get("load_more_last_button_text"),
        "last load more locator": stats.get("load_more_last_button_strategy"),
        "last load more button box": stats.get("load_more_last_button_box"),
        "last load more visible": stats.get("load_more_last_button_visible", False),
        "last load more enabled": stats.get("load_more_last_button_enabled", False),
        "cookie banner visible before load more": stats.get("load_more_cookie_banner_visible_before_click", False),
        "final number of hotel cards found": stats.get("final_cards_found", 0),
        "final extracted hotel count": stats.get("final_extracted_hotel_count", 0),
        "maximum scroll time reached": stats.get("maximum_scroll_time_reached", False),
        "total runtime seconds": stats.get("total_runtime_seconds", 0),
        "browser startup time seconds": stats.get("browser_startup_time_seconds", 0),
        "total page load time seconds": stats.get("page_load_time_seconds", 0),
        "total scrolling and load more time seconds": stats.get("load_more_time_seconds", 0),
        "total extraction time seconds": stats.get("extraction_time_seconds", 0),
        "total database save time seconds": stats.get("database_save_time_seconds", 0),
        "total excel export time seconds": stats.get("excel_export_time_seconds", 0),
        "ultra reliable loading mode": bool(options.ultra_reliable_loading_mode),
        "completion status": stats.get("completion_status"),
        "visible result count text": stats.get("visible_result_count_text"),
        "target result count": stats.get("target_result_count"),
        "visible result count is lower bound": stats.get("visible_result_count_is_lower_bound", False),
        "unique results loaded": stats.get("unique_results_loaded", stats.get("unique_hotels_collected", 0)),
        "estimated missing results": stats.get("estimated_missing_results"),
        "load more success count": stats.get("load_more_success_count", 0),
        "consecutive no new unique results": stats.get("consecutive_no_new_unique_results", 0),
        "consecutive no load more seen": stats.get("consecutive_no_load_more_seen", 0),
        "consecutive failed load more clicks": stats.get("consecutive_failed_load_more_clicks", 0),
        "bottom visible text path": stats.get("bottom_visible_text_path"),
        "full loading debug JSON path": stats.get("full_loading_debug_json_path"),
        "unique results loaded path": stats.get("unique_results_loaded_path"),
        "resume checkpoint path": stats.get("resume_checkpoint_path"),
        "final stop reason": stats.get("final_stop_reason"),
        "booking visible result count": stats.get("booking_visible_result_count"),
        "booking result count difference": stats.get("booking_result_count_difference"),
        "visible hotel cards count": stats.get("visible_hotel_cards_count", 0),
        "unique hotels collected": stats.get("unique_hotels_collected", 0),
        "new hotels added last cycle": stats.get("new_hotels_added_last_cycle", 0),
        "lowest price collected during loading": stats.get("lowest_price_collected"),
        "highest price collected during loading": stats.get("highest_price_collected"),
        "hotels with valid price during loading": stats.get("hotels_with_valid_price", 0),
        "hotels missing price during loading": stats.get("hotels_missing_price", 0),
        "final URL": stats.get("final_url"),
        "final page title": stats.get("final_page_title"),
        "final bottom screenshot path": stats.get("final_bottom_screenshot_path"),
        "collection completeness warning": stats.get("collection_completeness_warning"),
        "average seconds per hotel": stats.get("average_seconds_per_hotel", 0),
        "hotels with known star rating": stats.get("hotels_with_known_star_rating", 0),
        "hotels with unknown star rating": stats.get("hotels_with_unknown_star_rating", 0),
        "hotels removed by star filter": stats.get("hotels_removed_by_star_filter", 0),
        "hotels kept after star filter": stats.get("hotels_kept_after_star_filter", 0),
    }


def collector_options_kwargs(config: CollectionConfig, stop_event: Event | None = None, attempt: int = 1) -> dict[str, Any]:
    return {
        "collect_all_available": config.collect_all_available,
        "max_scroll_minutes": config.max_scroll_minutes,
        "selected_star_ratings": config.selected_star_ratings,
        "include_unknown_star_rating": config.include_unknown_star_rating,
        "debug_mode": config.debug_mode,
        "screenshots_enabled": config.screenshots_enabled,
        "headless": config.headless,
        "fast_mode": config.fast_mode,
        "performance_mode": config.performance_mode,
        "block_images_and_fonts": config.block_images_and_fonts,
        "stop_event": stop_event,
        "hotels_only": config.hotels_only,
        "disable_filters_during_complete_collection": config.disable_filters_during_complete_collection,
        "ultra_reliable_loading_mode": config.ultra_reliable_loading_mode,
        "screenshot_dir": SCREENSHOT_DIR,
        "screenshots_dir": SCREENSHOT_DIR,
        "debug_dir": DEBUG_DIR,
        "checkpoint_dir": CHECKPOINT_DIR,
        "status_dir": STATUS_DIR,
        "exports_dir": EXPORT_DIR,
        "log_dir": LOG_DIR,
        "instance_data_dir": INSTANCE_CONFIG.data_dir,
        "browser_profile_dir": BROWSER_PROFILE_DIR / "jobs" / f"worker_{os.getpid()}" / ("headless" if config.headless else "visible"),
        "partial_dir": PARTIAL_DIR,
        "current_attempt": attempt,
    }


def validate_collector_options(config: CollectionConfig, stop_event: Event | None = None) -> None:
    if config.batch_mode != "single" or int(config.max_parallel_workers) != 1:
        raise FatalScraperConfigError(
            "Parallel or split batch workers are disabled on this 8 GB tower. Use Single run; it checkpoints every date."
        )
    try:
        CollectorOptions(**collector_options_kwargs(config, stop_event, attempt=1))
    except TypeError as exc:
        message = str(exc)
        missing_field = None
        if "unexpected keyword argument" in message:
            missing_field = message.rsplit("'", 2)[1] if "'" in message else None
        hint = f" CollectorOptions does not accept field {missing_field}." if missing_field else ""
        raise FatalScraperConfigError(f"Configuration error:{hint} {message}") from exc


def matching_saved_run_id(config: CollectionConfig) -> int | None:
    target_checkout = calculate_checkout_date(config.checkin_end, config.nights).isoformat()
    for run in fetch_collection_runs(limit=200, backend=config.db_backend):
        if str(run.get("source") or "") != config.source:
            continue
        if str(run.get("city_or_region") or "").strip().lower() != config.city_or_region.strip().lower():
            continue
        if str(run.get("checkin_date")) != config.checkin_start.isoformat():
            continue
        if str(run.get("checkout_date")) != target_checkout:
            continue
        if int(run.get("number_of_nights") or 0) != int(config.nights):
            continue
        return int(run["id"])
    return None


def saved_date_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("collection_status") or "success")
        if status not in {"success", "completed", "completed_partial"} and not row.get("hotel_name"):
            continue
        checkin = row.get("checkin_date")
        if isinstance(checkin, date):
            key = checkin.isoformat()
        else:
            key = str(checkin)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def completed_dates_from_checkpoint_and_db(checkpoint: dict[str, Any], counts: dict[str, int]) -> set[str]:
    completed = set(str(value) for value in checkpoint.get("completed_dates") or [])
    date_statuses = checkpoint.get("date_statuses") or {}
    for date_key, count in counts.items():
        row = date_statuses.get(date_key)
        if isinstance(row, dict):
            if row.get("status") in {"completed", "completed_partial", "skipped_resume"} and count > 0:
                completed.add(date_key)
        elif count > 0:
            completed.add(date_key)
    return completed


def run_collection_job(config: CollectionConfig, stop_event: Event, queue: Queue) -> None:
    started_at = datetime.now()
    perf_start = time.perf_counter()
    run_id = None
    normalized_results: list[dict[str, Any]] = []
    saved_count = 0
    error_message = None
    planned_dates = date_range(config.checkin_start, config.checkin_end)
    checkpoint_payload: dict[str, Any] | None = None
    completed_dates: set[str] = set()
    current_stay_date: date | None = None
    options = CollectorOptions(
        collect_all_available=config.collect_all_available,
        max_scroll_minutes=config.max_scroll_minutes,
        selected_star_ratings=config.selected_star_ratings,
        include_unknown_star_rating=config.include_unknown_star_rating,
        debug_mode=config.debug_mode,
        screenshots_enabled=config.screenshots_enabled,
        headless=config.headless,
        fast_mode=config.fast_mode,
        performance_mode=config.performance_mode,
        block_images_and_fonts=config.block_images_and_fonts,
        stop_event=stop_event,
        hotels_only=config.hotels_only,
        disable_filters_during_complete_collection=config.disable_filters_during_complete_collection,
        ultra_reliable_loading_mode=config.ultra_reliable_loading_mode,
        screenshot_dir=SCREENSHOT_DIR,
        debug_dir=DEBUG_DIR,
        browser_profile_dir=(BROWSER_PROFILE_DIR / "browser_profile_headless") if config.headless and is_booking_source(config.source) else BROWSER_PROFILE_DIR,
    )

    def collector_status_callback(updates: dict[str, Any]) -> None:
        update_status_file(**updates)
        put_metrics(queue, {**(options.stats or {}), **updates})

    options.status_callback = collector_status_callback

    def log(message: str) -> None:
        put_log(queue, message)

    def progress(value: float, message: str) -> None:
        put_progress(queue, value, message)
        update_status_file(
            progress_percent=round(value * 100, 2),
            current_message=message,
            elapsed_seconds=round(time.perf_counter() - perf_start, 2),
        )

    try:
        progress(0.01, "Preparing collection run")
        update_status_file(
            job_id=getattr(queue, "job_id", None),
            status="running",
            current_stay_index=0,
            total_stay_dates=len(planned_dates),
            hotels_collected_current_date=0,
            hotels_collected_total=0,
            current_message="Preparing collection run",
            progress_percent=1.0,
            last_error=None,
        )
        if config.resume_previous_run:
            checkpoint_payload = load_resume_checkpoint(config)
            if checkpoint_payload and checkpoint_payload.get("run_id"):
                run_id = int(checkpoint_payload["run_id"])
                update_collection_run_status(run_id, "running", None, backend=config.db_backend)
                previous_results = fetch_results_by_run_id(run_id, backend=config.db_backend)
                normalized_results.extend(previous_results)
                saved_count = len(previous_results)
                completed_dates = {
                    date_text
                    for date_text, entry in (checkpoint_payload.get("dates") or {}).items()
                    if isinstance(entry, dict) and entry.get("status") == "completed"
                }
                log(f"Resuming previous run {run_id}. Completed dates already saved: {len(completed_dates)}.")
            else:
                log("Resume was enabled, but no matching checkpoint was found. Starting a new run.")
        if run_id is None:
            log(f"Creating collection run in {config.db_backend} database.")
            run_id = create_collection_run(
                config.source,
                config.city_or_region,
                config.checkin_start,
                calculate_checkout_date(config.checkin_end, config.nights),
                config.nights,
                config.adults,
                config.currency,
                0 if config.collect_all_available else config.max_hotels,
                selected_star_ratings=",".join(str(value) for value in config.selected_star_ratings),
                include_unknown_star_rating=config.include_unknown_star_rating,
                backend=config.db_backend,
            )
            checkpoint_payload = {
                "run_id": run_id,
                "status": "running",
                "planned_dates": [value.isoformat() for value in planned_dates],
                "dates": {},
            }
            checkpoint_path = save_resume_checkpoint(config, checkpoint_payload)
            log(f"Resume checkpoint created: {checkpoint_path}")
        elif checkpoint_payload is None:
            checkpoint_payload = {
                "run_id": run_id,
                "status": "running",
                "planned_dates": [value.isoformat() for value in planned_dates],
                "dates": {},
            }
            save_resume_checkpoint(config, checkpoint_payload)
        queue.put(("run_id", run_id))
        if saved_count:
            queue.put(("records_saved", saved_count))

        collector = get_collector(config.source)
        log(f"Planned stay dates: {len(planned_dates)}")
        for date_index, stay_date in enumerate(planned_dates, start=1):
            current_stay_date = stay_date
            if stop_event.is_set():
                log("Stop requested before next stay date.")
                break
            if stay_date.isoformat() in completed_dates:
                log(f"Skipping already completed stay date {stay_date.isoformat()} from resume checkpoint.")
                continue

            checkout_date = calculate_checkout_date(stay_date, config.nights)
            current_base = (date_index - 1) / max(len(planned_dates), 1)
            progress(current_base * 0.86 + 0.03, f"Collecting {config.source}: {stay_date.isoformat()} to {checkout_date.isoformat()}")
            update_status_file(
                current_stay_index=date_index,
                total_stay_dates=len(planned_dates),
                current_checkin_date=stay_date.isoformat(),
                current_checkout_date=checkout_date.isoformat(),
                hotels_collected_current_date=0,
                current_message=f"Collecting {stay_date.isoformat()}",
                progress_percent=round(((date_index - 1) / max(len(planned_dates), 1)) * 100, 2),
            )
            log(f"Starting stay date {date_index} of {len(planned_dates)}: {stay_date.isoformat()} for {config.nights} night(s).")
            checkpoint_path = mark_checkpoint_date(config, checkpoint_payload, stay_date, "in_progress")
            options.stats["resume_checkpoint_path"] = str(checkpoint_path.resolve())

            raw_results = collector.collect(
                config.city_or_region,
                stay_date,
                checkout_date,
                config.adults,
                config.currency,
                config.max_hotels,
                config.collect_all_available,
                options,
                lambda value, message, base=current_base: progress(min(0.90, base * 0.86 + value / max(len(planned_dates), 1) * 0.86), message),
                log,
            )
            if not is_booking_source(config.source):
                raw_results = filter_results_by_star_rating(raw_results, options, queue)

            date_results = [normalize_hotel_result({**row, "collection_run_id": run_id}) for row in raw_results]
            price_debug_path = write_price_pipeline_debug(stay_date, date_results)
            price_summary = price_validation_summary(date_results)
            normalized_results.extend(date_results)
            progress(0.91, f"Saving {len(date_results)} records for {stay_date.isoformat()} to {config.db_backend}")
            log(f"Saving hotel records to {config.db_backend} for {stay_date.isoformat()} in one batch.")
            db_save_start = time.perf_counter()
            saved_count += insert_hotel_results(date_results, backend=config.db_backend)
            options.stats["database_save_time_seconds"] = round(float(options.stats.get("database_save_time_seconds", 0)) + (time.perf_counter() - db_save_start), 2)
            queue.put(("records_saved", saved_count))
            update_status_file(
                hotels_collected_current_date=len(date_results),
                hotels_collected_total=len(normalized_results),
                visible_card_count=options.stats.get("visible_hotel_cards_count", options.stats.get("final_cards_found", len(raw_results))),
                unique_hotels_collected=options.stats.get("unique_hotels_collected", len(normalized_results)),
                booking_visible_result_count=options.stats.get("booking_visible_result_count"),
                load_more_click_count=options.stats.get("load_more_clicks", 0),
                lowest_price=price_summary.get("lowest_price") or options.stats.get("lowest_price_collected"),
                highest_price=price_summary.get("highest_price") or options.stats.get("highest_price_collected"),
                rows_with_raw_price=price_summary.get("rows_with_raw_price"),
                rows_with_parsed_price=price_summary.get("rows_with_parsed_price"),
                rows_missing_price=price_summary.get("rows_missing_price"),
                price_pipeline_debug_file=str(price_debug_path),
                stay_dates_completed=date_index,
                errors_current_date=sum(1 for row in date_results if row.get("collection_status") != "success"),
                api_requests_used=options.stats.get("api_requests_used", 0),
                progress_percent=round((date_index / max(len(planned_dates), 1)) * 100, 2),
                current_message=f"Saved {len(date_results)} records for {stay_date.isoformat()}",
            )
            checkpoint_path = mark_checkpoint_date(config, checkpoint_payload, stay_date, "completed", len(date_results))
            completed_dates.add(stay_date.isoformat())
            options.stats["resume_checkpoint_path"] = str(checkpoint_path.resolve())
            log(f"Saved progress checkpoint for {stay_date.isoformat()}: {checkpoint_path}")

            elapsed_minutes = max((datetime.now() - started_at).total_seconds() / 60, 0.01)
            hotels_per_minute = len(normalized_results) / elapsed_minutes
            remaining_dates = len(planned_dates) - date_index
            avg_seconds_per_date = (datetime.now() - started_at).total_seconds() / date_index
            estimated_remaining = remaining_dates * avg_seconds_per_date
            put_metrics(
                queue,
                {
                    "hotels_collected_per_minute": round(hotels_per_minute, 2),
                    "current_stay_date": stay_date.isoformat(),
                    "current_hotel_card_count": len(raw_results),
                    "stay_dates_completed": date_index,
                    "total_stay_dates": len(planned_dates),
                    "hotels_collected_current_date": len(date_results),
                    "errors_current_date": sum(1 for row in date_results if row.get("collection_status") != "success"),
                    "api_requests_used": options.stats.get("api_requests_used", 0),
                    "elapsed_time": str(datetime.now() - started_at).split(".")[0],
                    "estimated_remaining_time": str(timedelta(seconds=int(estimated_remaining))),
                    "load_more_clicks": options.stats.get("load_more_clicks", 0),
                    "load_more_button_seen_count": options.stats.get("load_more_button_seen_count", 0),
                    "load_more_click_fail_count": options.stats.get("load_more_click_fail_count", 0),
                    "last_load_more_error": options.stats.get("last_load_more_error"),
                    "load_more_last_button_text": options.stats.get("load_more_last_button_text"),
                    "load_more_last_button_strategy": options.stats.get("load_more_last_button_strategy"),
                    "load_more_last_button_box": options.stats.get("load_more_last_button_box"),
                    "load_more_last_button_visible": options.stats.get("load_more_last_button_visible", False),
                    "load_more_last_button_enabled": options.stats.get("load_more_last_button_enabled", False),
                    "load_more_cookie_banner_visible_before_click": options.stats.get("load_more_cookie_banner_visible_before_click", False),
                    "hotel_cards_before_last_click": options.stats.get("hotel_cards_before_last_click", 0),
                    "hotel_cards_after_last_click": options.stats.get("hotel_cards_after_last_click", 0),
                    "new_cards_after_last_click": options.stats.get("new_cards_after_last_click", 0),
                    "consecutive_no_new_card_attempts": options.stats.get("consecutive_no_new_card_attempts", 0),
                    "consecutive_no_button_attempts": options.stats.get("consecutive_no_button_attempts", 0),
                    "maximum_scroll_time_reached": options.stats.get("maximum_scroll_time_reached", False),
                    "cookie_banner_handled": bool(options.stats.get("cookie_banner_declined")),
                    "unexpected_property_pages_closed": options.stats.get("unexpected_new_pages_closed", 0),
                    "unexpected_property_navigation_count": options.stats.get("unexpected_property_navigation_count", 0),
                    "final_result_card_count": options.stats.get("final_cards_found", 0),
                    "final_extracted_hotel_count": options.stats.get("final_extracted_hotel_count", 0),
                    "unique_hotels_collected": options.stats.get("unique_hotels_collected", len(normalized_results)),
                    "unique_results_loaded": options.stats.get("unique_results_loaded", options.stats.get("unique_hotels_collected", len(normalized_results))),
                    "new_hotels_added_last_cycle": options.stats.get("new_hotels_added_last_cycle", 0),
                    "completion_status": options.stats.get("completion_status"),
                    "visible_result_count_text": options.stats.get("visible_result_count_text"),
                    "target_result_count": options.stats.get("target_result_count"),
                    "visible_result_count_is_lower_bound": options.stats.get("visible_result_count_is_lower_bound", False),
                    "estimated_missing_results": options.stats.get("estimated_missing_results"),
                    "booking_visible_result_count": options.stats.get("booking_visible_result_count"),
                    "booking_result_count_difference": options.stats.get("booking_result_count_difference"),
                    "visible_hotel_cards_count": options.stats.get("visible_hotel_cards_count", 0),
                    "lowest_price_collected": options.stats.get("lowest_price_collected"),
                    "highest_price_collected": options.stats.get("highest_price_collected"),
                    "hotels_with_valid_price": options.stats.get("hotels_with_valid_price", 0),
                    "hotels_missing_price": options.stats.get("hotels_missing_price", 0),
                    "final_stop_reason": options.stats.get("final_stop_reason"),
                    "final_url": options.stats.get("final_url"),
                    "final_page_title": options.stats.get("final_page_title"),
                    "final_bottom_screenshot_path": options.stats.get("final_bottom_screenshot_path"),
                    "full_loading_debug_json_path": options.stats.get("full_loading_debug_json_path"),
                    "unique_results_loaded_path": options.stats.get("unique_results_loaded_path"),
                    "bottom_visible_text_path": options.stats.get("bottom_visible_text_path"),
                    "collection_completeness_warning": options.stats.get("collection_completeness_warning"),
                    "resume_checkpoint_path": options.stats.get("resume_checkpoint_path"),
                    "load_more_success_count": options.stats.get("load_more_success_count", 0),
                    "consecutive_no_new_unique_results": options.stats.get("consecutive_no_new_unique_results", 0),
                    "consecutive_no_load_more_seen": options.stats.get("consecutive_no_load_more_seen", 0),
                    "consecutive_failed_load_more_clicks": options.stats.get("consecutive_failed_load_more_clicks", 0),
                    "average_seconds_per_hotel": round((datetime.now() - started_at).total_seconds() / max(len(normalized_results), 1), 2),
                    "database_insert_time": options.stats.get("database_save_time_seconds", 0),
                    "excel_export_time": options.stats.get("excel_export_time_seconds", 0),
                    "browser_startup_time_seconds": options.stats.get("browser_startup_time_seconds", 0),
                    "page_load_time_seconds": options.stats.get("page_load_time_seconds", 0),
                    "load_more_time_seconds": options.stats.get("load_more_time_seconds", 0),
                    "extraction_time_seconds": options.stats.get("extraction_time_seconds", 0),
                    "hotels_extracted": options.stats.get("hotel_records_extracted", len(raw_results)),
                    "hotels_kept_after_filters": options.stats.get("records_after_hotels_only_filter", len(raw_results)),
                    "hotels_removed_by_hotels_only_filter": options.stats.get("hotels_only_removed", 0),
                    "hotels_with_known_star_rating": options.stats.get("hotels_with_known_star_rating", 0),
                    "hotels_with_unknown_star_rating": options.stats.get("hotels_with_unknown_star_rating", 0),
                    "hotels_removed_by_star_filter": options.stats.get("hotels_removed_by_star_filter", 0),
                    "hotels_kept_after_star_filter": options.stats.get("hotels_kept_after_star_filter", 0),
                },
            )

        run_metadata = {
            "source": config.source,
            "city_or_region": config.city_or_region,
            "checkin_date": config.checkin_start,
            "checkout_date": calculate_checkout_date(config.checkin_end, config.nights),
            "number_of_nights": config.nights,
            "adults": config.adults,
            "currency": config.currency,
            "started_at": started_at,
            "completed_at": datetime.now(),
        }
        run_metadata.update(_collector_summary_metadata(options))
        summary = create_summary(normalized_results, run_metadata)
        price_summary = price_validation_summary(normalized_results)
        summary.update(price_summary)
        if price_summary["price_missing_ratio"] > 0.30 and normalized_results:
            summary["price warning"] = "Warning: many rows are missing prices. Check price extraction debug file."
        progress(0.96, "Creating Excel export")
        log("Creating Excel file at the end of the collection run.")
        excel_start = time.perf_counter()
        excel_path = export_results_to_excel(normalized_results, summary, EXPORT_DIR)
        options.stats["excel_export_time_seconds"] = round(time.perf_counter() - excel_start, 2)
        update_collection_run_excel_path(run_id, str(excel_path), backend=config.db_backend)
        csv_payload = export_final_csv(normalized_results, summary, session_id=run_id, log_callback=log)

        status = terminal_status_for_results(normalized_results, stop_event.is_set(), None)
        if str(options.stats.get("completion_status") or "").startswith("incomplete") and normalized_results:
            status = "completed_with_partial_results"
        update_collection_run_status(run_id, status, backend=config.db_backend)
        if checkpoint_payload is not None:
            checkpoint_payload["status"] = status
            checkpoint_payload["completed_dates"] = sorted(
                date_text
                for date_text, entry in (checkpoint_payload.get("dates") or {}).items()
                if isinstance(entry, dict) and entry.get("status") == "completed"
            )
            checkpoint_path = save_resume_checkpoint(config, checkpoint_payload)
            options.stats["resume_checkpoint_path"] = str(checkpoint_path.resolve())
        log(f"Database run status updated: {status}")

        queue.put(("complete", {"results": normalized_results, "summary": summary, "excel_file_path": str(excel_path), "status": status, **csv_payload}))
        update_status_file(status=status, progress_percent=100.0, current_message=f"Collection finished: {status}", latest_excel_file=str(excel_path), **csv_payload, **price_validation_summary(normalized_results))
        put_metrics(queue, {
            **(options.stats or {}),
            "hotels_collected_per_minute": round(len(normalized_results) / max((datetime.now() - started_at).total_seconds() / 60, 0.01), 2),
            "elapsed_time": str(datetime.now() - started_at).split(".")[0],
            "load_more_clicks": options.stats.get("load_more_clicks", 0),
            "load_more_button_seen_count": options.stats.get("load_more_button_seen_count", 0),
            "load_more_click_fail_count": options.stats.get("load_more_click_fail_count", 0),
            "last_load_more_error": options.stats.get("last_load_more_error"),
            "load_more_last_button_text": options.stats.get("load_more_last_button_text"),
            "load_more_last_button_strategy": options.stats.get("load_more_last_button_strategy"),
            "load_more_last_button_box": options.stats.get("load_more_last_button_box"),
            "load_more_last_button_visible": options.stats.get("load_more_last_button_visible", False),
            "load_more_last_button_enabled": options.stats.get("load_more_last_button_enabled", False),
            "load_more_cookie_banner_visible_before_click": options.stats.get("load_more_cookie_banner_visible_before_click", False),
            "hotel_cards_before_last_click": options.stats.get("hotel_cards_before_last_click", 0),
            "hotel_cards_after_last_click": options.stats.get("hotel_cards_after_last_click", 0),
            "new_cards_after_last_click": options.stats.get("new_cards_after_last_click", 0),
            "consecutive_no_new_card_attempts": options.stats.get("consecutive_no_new_card_attempts", 0),
            "consecutive_no_button_attempts": options.stats.get("consecutive_no_button_attempts", 0),
            "maximum_scroll_time_reached": options.stats.get("maximum_scroll_time_reached", False),
            "cookie_banner_handled": bool(options.stats.get("cookie_banner_declined")),
            "unexpected_property_pages_closed": options.stats.get("unexpected_new_pages_closed", 0),
            "unexpected_property_navigation_count": options.stats.get("unexpected_property_navigation_count", 0),
            "current_hotel_card_count": options.stats.get("final_cards_found", len(normalized_results)),
            "final_result_card_count": options.stats.get("final_cards_found", len(normalized_results)),
            "final_extracted_hotel_count": options.stats.get("final_extracted_hotel_count", len(normalized_results)),
            "unique_hotels_collected": options.stats.get("unique_hotels_collected", len(normalized_results)),
            "unique_results_loaded": options.stats.get("unique_results_loaded", options.stats.get("unique_hotels_collected", len(normalized_results))),
            "new_hotels_added_last_cycle": options.stats.get("new_hotels_added_last_cycle", 0),
            "completion_status": options.stats.get("completion_status"),
            "visible_result_count_text": options.stats.get("visible_result_count_text"),
            "target_result_count": options.stats.get("target_result_count"),
            "visible_result_count_is_lower_bound": options.stats.get("visible_result_count_is_lower_bound", False),
            "estimated_missing_results": options.stats.get("estimated_missing_results"),
            "booking_visible_result_count": options.stats.get("booking_visible_result_count"),
            "booking_result_count_difference": options.stats.get("booking_result_count_difference"),
            "visible_hotel_cards_count": options.stats.get("visible_hotel_cards_count", 0),
            "lowest_price_collected": options.stats.get("lowest_price_collected"),
            "highest_price_collected": options.stats.get("highest_price_collected"),
            "hotels_with_valid_price": options.stats.get("hotels_with_valid_price", 0),
            "hotels_missing_price": options.stats.get("hotels_missing_price", 0),
            "final_stop_reason": options.stats.get("final_stop_reason"),
            "final_url": options.stats.get("final_url"),
            "final_page_title": options.stats.get("final_page_title"),
            "final_bottom_screenshot_path": options.stats.get("final_bottom_screenshot_path"),
            "full_loading_debug_json_path": options.stats.get("full_loading_debug_json_path"),
            "unique_results_loaded_path": options.stats.get("unique_results_loaded_path"),
            "bottom_visible_text_path": options.stats.get("bottom_visible_text_path"),
            "collection_completeness_warning": options.stats.get("collection_completeness_warning"),
            "resume_checkpoint_path": options.stats.get("resume_checkpoint_path"),
            "load_more_success_count": options.stats.get("load_more_success_count", 0),
            "consecutive_no_new_unique_results": options.stats.get("consecutive_no_new_unique_results", 0),
            "consecutive_no_load_more_seen": options.stats.get("consecutive_no_load_more_seen", 0),
            "consecutive_failed_load_more_clicks": options.stats.get("consecutive_failed_load_more_clicks", 0),
            "average_seconds_per_hotel": round((datetime.now() - started_at).total_seconds() / max(len(normalized_results), 1), 2),
            "database_insert_time": options.stats.get("database_save_time_seconds", 0),
            "excel_export_time": options.stats.get("excel_export_time_seconds", 0),
            "total_runtime_seconds": round(time.perf_counter() - perf_start, 2),
            "browser_startup_time_seconds": options.stats.get("browser_startup_time_seconds", 0),
            "page_load_time_seconds": options.stats.get("page_load_time_seconds", 0),
            "load_more_time_seconds": options.stats.get("load_more_time_seconds", 0),
            "extraction_time_seconds": options.stats.get("extraction_time_seconds", 0),
            "hotels_extracted": options.stats.get("hotel_records_extracted", len(normalized_results)),
            "hotels_kept_after_filters": options.stats.get("records_after_hotels_only_filter", len(normalized_results)),
            "hotels_removed_by_hotels_only_filter": options.stats.get("hotels_only_removed", 0),
            "hotels_with_known_star_rating": options.stats.get("hotels_with_known_star_rating", 0),
            "hotels_with_unknown_star_rating": options.stats.get("hotels_with_unknown_star_rating", 0),
            "hotels_removed_by_star_filter": options.stats.get("hotels_removed_by_star_filter", 0),
            "hotels_kept_after_star_filter": options.stats.get("hotels_kept_after_star_filter", 0),
        })
        progress(1.0, f"Collection finished: {status}")
        if status == "completed_with_zero_results":
            log("Collection did not collect any real hotels. Dashboard status is completed_with_zero_results, not completed.")
        log(f"Excel file created: {excel_path}")
        log(f"Performance summary: total={round(time.perf_counter() - perf_start, 2)}s, browser={options.stats.get('browser_startup_time_seconds', 0)}s, page_load={options.stats.get('page_load_time_seconds', 0)}s, load_more={options.stats.get('load_more_time_seconds', 0)}s, extraction={options.stats.get('extraction_time_seconds', 0)}s, database={options.stats.get('database_save_time_seconds', 0)}s, excel={options.stats.get('excel_export_time_seconds', 0)}s")
    except Exception as exc:
        error_message = str(exc)
        update_status_file(status="failed", last_error=error_message, current_message=error_message)
        log(f"Error: {error_message}")
        if checkpoint_payload is not None:
            checkpoint_payload["status"] = "failed"
            if current_stay_date is not None and current_stay_date.isoformat() not in completed_dates:
                checkpoint_path = mark_checkpoint_date(config, checkpoint_payload, current_stay_date, "failed", 0, error_message)
                options.stats["resume_checkpoint_path"] = str(checkpoint_path.resolve())
        status = terminal_status_for_results(normalized_results, stop_event.is_set(), error_message)
        if run_id is not None:
            if normalized_results:
                run_metadata = {
                    "source": config.source,
                    "city_or_region": config.city_or_region,
                    "checkin_date": config.checkin_start,
                    "checkout_date": calculate_checkout_date(config.checkin_end, config.nights),
                    "number_of_nights": config.nights,
                    "adults": config.adults,
                    "currency": config.currency,
                    "started_at": started_at,
                    "completed_at": datetime.now(),
                }
                run_metadata.update(_collector_summary_metadata(options))
                summary = create_summary(normalized_results, run_metadata)
                excel_path = export_results_to_excel(normalized_results, summary, EXPORT_DIR)
                update_collection_run_excel_path(run_id, str(excel_path), backend=config.db_backend)
                csv_payload = export_final_csv(normalized_results, summary, session_id=run_id, log_callback=log)
                queue.put(("complete", {"results": normalized_results, "summary": summary, "excel_file_path": str(excel_path), "status": status, **csv_payload}))
            update_collection_run_status(run_id, status, error_message, backend=config.db_backend)
            log(f"Database run status updated: {status}")
        queue.put(("failed", {"status": status, "error": error_message, **(locals().get("csv_payload") or {})}))
        progress(1.0, f"Collection finished: {status}")


def run_resilient_collection_job(config: CollectionConfig, stop_event: Any, queue: Any) -> None:
    started_at = datetime.now()
    perf_start = time.perf_counter()
    planned_dates = date_range(config.checkin_start, config.checkin_end)
    retry_settings = RetrySettings(
        retry_failed_dates_automatically=config.retry_failed_dates_automatically,
        max_retries_per_date=max(1, int(config.max_retries_per_date)),
        retry_delay_seconds=max(0, int(os.getenv("RETRY_DELAY_SECONDS", "30") or "30")),
        restart_browser_between_retries=True,
        continue_if_date_fails=config.continue_if_date_fails,
        auto_export_partial_excel=config.auto_export_partial_excel,
        partial_export_frequency=config.partial_export_frequency,
        resume_from_checkpoint=config.resume_previous_run,
    )
    normalized_results: list[dict[str, Any]] = []
    date_status_rows: list[dict[str, Any]] = []
    saved_count = 0
    last_log_message = "Preparing collection run"
    last_snapshot_time = time.monotonic()
    fatal_error: str | None = None
    run_id: int | None = None
    guard: ResourceGuard = getattr(queue, "resource_guard", ResourceGuard(INSTANCE_CONFIG.data_dir, owner_pid=os.getpid()))
    checkpoint_path = _checkpoint_path(config)
    current_checkpoint_path = CHECKPOINT_DIR / "current_run_resume.json"

    try:
        validate_collector_options(config, stop_event)
    except FatalScraperConfigError as exc:
        message = str(exc)
        update_status_file(
            job_id=getattr(queue, "job_id", None),
            status="fatal_config_error",
            current_message=message,
            last_error=message,
            progress_percent=0.0,
        )
        put_log(queue, message)
        queue.put(("failed", {"status": "fatal_config_error", "error": message}))
        return

    def log(message: str) -> None:
        nonlocal last_log_message
        last_log_message = message
        put_log(queue, message)
        update_heartbeat(
            HEARTBEAT_FILE,
            job_id=getattr(queue, "job_id", None),
            instance_id=INSTANCE_CONFIG.instance_id,
            current_date=None,
            status="running",
            last_log_message=message,
        )

    def progress(value: float, message: str) -> None:
        snapshot = guard.check()
        put_progress(queue, value, message)
        put_metrics(queue, {**snapshot.as_dict(), "resource_guard_level": guard.last_level})
        update_status_file(
            progress_percent=round(value * 100, 2),
            current_message=message,
            elapsed_seconds=round(time.perf_counter() - perf_start, 2),
            **snapshot.as_dict(),
        )

    def retain_recent(records: list[dict[str, Any]]) -> None:
        combined = normalized_results + records
        deduped_reversed: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for row in reversed(combined):
            key = (
                row.get("collection_run_id"),
                str(row.get("source") or "").lower(),
                str(row.get("checkin_date") or ""),
                str(row.get("hotel_url") or row.get("hotel_name") or "").lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped_reversed.append(row)
            if len(deduped_reversed) >= MAX_UI_RESULTS:
                break
        normalized_results[:] = list(reversed(deduped_reversed))

    def save_checkpoint(payload: dict[str, Any]) -> None:
        atomic_write_json(checkpoint_path, payload)
        atomic_write_json(current_checkpoint_path, payload)
        log("Checkpoint updated")

    def push_date_rows(checkpoint: dict[str, Any]) -> None:
        nonlocal date_status_rows
        rows = list((checkpoint.get("date_statuses") or {}).values())
        rows.sort(key=lambda row: str(row.get("checkin_date") or ""))
        date_status_rows = rows
        queue.put(("date_status_rows", date_status_rows))
        put_metrics(queue, {"date_status_rows": date_status_rows, "resume_checkpoint_path": str(checkpoint_path.resolve())})

    def build_summary(status: str) -> dict[str, Any]:
        run_metadata = {
            "source": config.source,
            "city_or_region": config.city_or_region,
            "checkin_date": config.checkin_start,
            "checkout_date": calculate_checkout_date(config.checkin_end, config.nights),
            "number_of_nights": config.nights,
            "adults": config.adults,
            "currency": config.currency,
            "started_at": started_at,
            "completed_at": datetime.now(),
            "resume checkpoint path": str(checkpoint_path.resolve()),
            "completion status": status,
            "__date_status_rows": date_status_rows,
        }
        summary = create_summary(normalized_results, run_metadata)
        summary["total_records"] = saved_count
        summary["results retained in worker memory"] = len(normalized_results)
        summary["__date_status_rows"] = date_status_rows
        summary.update(price_validation_summary(normalized_results))
        return summary

    def export_partial(status: str, snapshot: bool = False) -> Path | None:
        if not retry_settings.auto_export_partial_excel:
            return None
        summary = build_summary(status)
        filename = "partial_current_run.xlsx"
        if snapshot:
            snapshot_dir = EXPORT_DIR / "snapshots"
            filename = f"partial_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            output_dir = snapshot_dir
        else:
            output_dir = EXPORT_DIR
        if config.db_backend == "sqlite" and run_id is not None:
            excel_path = export_sqlite_run_to_excel(run_id, summary, output_dir, filename=filename)
        else:
            excel_path = export_results_to_excel(normalized_results, summary, output_dir, filename=filename)
        log(f"Partial Excel exported: {excel_path}")
        return excel_path

    def make_options(attempt: int) -> CollectorOptions:
        nonlocal saved_count
        options = CollectorOptions(**collector_options_kwargs(config, stop_event, attempt=attempt))
        partial_seen_keys: set[str] = set()
        job_token = safe_filename(str(getattr(queue, "job_id", None) or f"worker_{os.getpid()}"))
        mode_token = "headless" if config.headless else "visible"
        options.browser_profile_dir = BROWSER_PROFILE_DIR / "jobs" / job_token / mode_token
        options.screenshot_dir = SCREENSHOT_DIR / "runs" / job_token
        options.screenshots_dir = options.screenshot_dir
        options.stats["screenshot_limit"] = max(1, int(os.getenv("OTA_SCREENSHOT_LIMIT_PER_DATE", "12")))

        def collector_status_callback(updates: dict[str, Any]) -> None:
            snapshot = guard.check()
            telemetry = {**snapshot.as_dict(), "resource_guard_level": guard.last_level}
            update_status_file(**updates, **telemetry)
            put_metrics(queue, {**(options.stats or {}), **updates, **telemetry})

        def partial_results_callback(stay_date: date, records: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
            if not records:
                return
            paths = save_partial_records(PARTIAL_DIR, stay_date, records)
            if run_id is not None:
                persistable: list[dict[str, Any]] = []
                checkout_date = calculate_checkout_date(stay_date, config.nights)
                for index, record in enumerate(records, start=1):
                    identity = str(record.get("url") or record.get("hotel_url") or record.get("name") or record.get("hotel_name") or "").strip().lower()
                    if not identity or identity in partial_seen_keys:
                        continue
                    partial_seen_keys.add(identity)
                    persistable.append(
                        normalize_hotel_result(
                            {
                                "collection_run_id": run_id,
                                "source": config.source,
                                "city_or_region": config.city_or_region,
                                "search_url": record.get("search_url"),
                                "hotel_name": record.get("name") or record.get("hotel_name"),
                                "hotel_url": record.get("url") or record.get("hotel_url"),
                                "raw_price_text": record.get("price") or record.get("raw_price_text"),
                                "parsed_price": record.get("price") or record.get("parsed_price"),
                                "cheapest_price_total": record.get("price") or record.get("cheapest_price_total"),
                                "currency": config.currency,
                                "checkin_date": stay_date,
                                "checkout_date": checkout_date,
                                "number_of_nights": config.nights,
                                "adults": config.adults,
                                "provider_name": config.source,
                                "ranking_position_on_page": record.get("index") or index,
                                "collection_status": "partial",
                                "collected_at": datetime.now(),
                            }
                        )
                    )
                if persistable:
                    with _db_write_lock_for(config):
                        insert_hotel_results(persistable, backend=config.db_backend)
                    saved_count = count_results_by_run_id(run_id, backend=config.db_backend)
                    retain_recent(persistable)
                    queue.put(("records_saved", saved_count))
                    log(f"Incremental SQLite checkpoint: {len(persistable)} newly discovered hotels for {stay_date.isoformat()}.")
            options.stats["partial_json_path"] = paths["json"]
            options.stats["partial_csv_path"] = paths["csv"]
            options.stats["partial_records_saved"] = paths["records"]
            collector_status_callback(
                {
                    "partial_json_path": paths["json"],
                    "partial_csv_path": paths["csv"],
                    "partial_records_saved": paths["records"],
                    **metadata,
                }
            )
            log(f"Partial records saved for {stay_date.isoformat()}: {paths['records']}")

        options.status_callback = collector_status_callback
        options.partial_results_callback = partial_results_callback
        return options

    def save_current_date_results(
        *,
        stay_date: date,
        checkout_date: date,
        raw_results: list[dict[str, Any]],
        run_id_value: int,
        options: CollectorOptions,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        nonlocal saved_count
        if not is_booking_source(config.source):
            raw_results = filter_results_by_star_rating(raw_results, options, queue)
        date_results = [normalize_hotel_result({**row, "collection_run_id": run_id_value}) for row in raw_results]
        partial_paths = save_partial_records(PARTIAL_DIR, stay_date, date_results)
        if date_results:
            log(f"Saving {len(date_results)} records for {stay_date.isoformat()} to {config.db_backend}.")
            db_save_start = time.perf_counter()
            with _db_write_lock_for(config):
                insert_hotel_results(date_results, backend=config.db_backend)
            saved_count = count_results_by_run_id(run_id_value, backend=config.db_backend)
            options.stats["database_save_time_seconds"] = round(float(options.stats.get("database_save_time_seconds", 0)) + (time.perf_counter() - db_save_start), 2)
            queue.put(("records_saved", saved_count))
        retain_recent(date_results)
        price_debug_path = write_price_pipeline_debug(stay_date, date_results)
        price_summary = price_validation_summary(date_results)
        update_status_file(
            hotels_collected_current_date=len(date_results),
            hotels_collected_total=saved_count,
            rows_with_raw_price=price_summary.get("rows_with_raw_price"),
            rows_with_parsed_price=price_summary.get("rows_with_parsed_price"),
            rows_missing_price=price_summary.get("rows_missing_price"),
            price_pipeline_debug_file=str(price_debug_path),
        )
        return date_results, {
            "partial_json": partial_paths["json"],
            "partial_csv": partial_paths["csv"],
            "saved_to_sqlite": bool(date_results),
        }

    try:
        progress(0.01, "Preparing resilient collection run")
        init_db(config.db_backend)
        update_status_file(
            job_id=getattr(queue, "job_id", None),
            status="running",
            current_stay_index=0,
            total_stay_dates=len(planned_dates),
            hotels_collected_current_date=0,
            hotels_collected_total=0,
            current_message="Preparing resilient collection run",
            progress_percent=1.0,
            last_error=None,
            heartbeat_file=str(HEARTBEAT_FILE.resolve()),
        )
        log("Creating or resuming collection run.")
        checkpoint = load_checkpoint(checkpoint_path, _checkpoint_signature(config)) if retry_settings.resume_from_checkpoint else None
        if checkpoint and checkpoint.get("run_id"):
            run_id = int(checkpoint["run_id"])
            update_collection_run_status(run_id, "running", None, backend=config.db_backend)
            normalized_results = fetch_results_by_run_id_limited(run_id, MAX_UI_RESULTS, backend=config.db_backend)
            saved_count = count_results_by_run_id(run_id, backend=config.db_backend)
            log(f"Resuming previous run {run_id}. Existing saved rows: {saved_count}.")
        elif retry_settings.resume_from_checkpoint:
            matched_run_id = matching_saved_run_id(config)
            if matched_run_id is not None:
                run_id = matched_run_id
                update_collection_run_status(run_id, "running", None, backend=config.db_backend)
                normalized_results = fetch_results_by_run_id_limited(run_id, MAX_UI_RESULTS, backend=config.db_backend)
                saved_count = count_results_by_run_id(run_id, backend=config.db_backend)
                checkpoint = build_checkpoint(
                    job_id=getattr(queue, "job_id", None),
                    run_id=run_id,
                    source=config.source,
                    city=config.city_or_region,
                    start_date=config.checkin_start,
                    end_date=config.checkin_end,
                    nights=config.nights,
                    planned_dates=planned_dates,
                    signature=_checkpoint_signature(config),
                )
                log(f"Resuming matching saved run {run_id} from SQLite. Existing saved rows: {saved_count}.")
        else:
            matched_run_id = None
        if run_id is None:
            run_id = create_collection_run(
                config.source,
                config.city_or_region,
                config.checkin_start,
                calculate_checkout_date(config.checkin_end, config.nights),
                config.nights,
                config.adults,
                config.currency,
                0 if config.collect_all_available else config.max_hotels,
                selected_star_ratings=",".join(str(value) for value in config.selected_star_ratings),
                include_unknown_star_rating=config.include_unknown_star_rating,
                backend=config.db_backend,
            )
            checkpoint = build_checkpoint(
                job_id=getattr(queue, "job_id", None),
                run_id=run_id,
                source=config.source,
                city=config.city_or_region,
                start_date=config.checkin_start,
                end_date=config.checkin_end,
                nights=config.nights,
                planned_dates=planned_dates,
                signature=_checkpoint_signature(config),
            )
            save_checkpoint(checkpoint)
            log(f"New run created: {run_id}")
        queue.put(("run_id", run_id))
        queue.put(("records_saved", saved_count))
        db_date_counts = result_date_counts_by_run_id(run_id, backend=config.db_backend)
        completed_dates = completed_dates_from_checkpoint_and_db(checkpoint, db_date_counts)
        checkpoint["completed_dates"] = sorted(completed_dates)
        for completed_date in sorted(completed_dates):
            if completed_date not in (checkpoint.get("date_statuses") or {}):
                stay_date_for_row = date.fromisoformat(completed_date)
                checkout_for_row = calculate_checkout_date(stay_date_for_row, config.nights)
                checkpoint = update_checkpoint_date(
                    checkpoint,
                    stay_date=stay_date_for_row,
                    checkout_date=checkout_for_row,
                    status="completed",
                    attempts=int((checkpoint.get("retry_counts_by_date") or {}).get(completed_date) or 0),
                    records_collected=db_date_counts.get(completed_date, 0),
                    output_files={"saved_to_sqlite": True},
                )
        remaining_dates = [value for value in planned_dates if value.isoformat() not in completed_dates]
        first_remaining = remaining_dates[0].isoformat() if remaining_dates else "none"
        log("Resume summary:")
        log(f"Total planned dates: {len(planned_dates)}")
        log(f"Completed dates already saved: {len(completed_dates)}")
        log(f"Remaining dates to scrape: {len(remaining_dates)}")
        log(f"First remaining date: {first_remaining}")
        log(f"Existing saved rows: {saved_count}")
        save_checkpoint(checkpoint)
        push_date_rows(checkpoint)

        collector = get_collector(config.source)

        for date_index, stay_date in enumerate(planned_dates, start=1):
            date_key = stay_date.isoformat()
            checkout_date = calculate_checkout_date(stay_date, config.nights)
            if stop_event.is_set():
                log("Stop requested before next stay date.")
                checkpoint = update_checkpoint_date(
                    checkpoint,
                    stay_date=stay_date,
                    checkout_date=checkout_date,
                    status="stopped_by_user",
                    attempts=0,
                    error="Stop requested before date started.",
                    records_collected=0,
                )
                save_checkpoint(checkpoint)
                break
            if retry_settings.resume_from_checkpoint and date_key in completed_dates:
                log(f"Skipping {date_key} because it already has saved records.")
                checkpoint = update_checkpoint_date(
                    checkpoint,
                    stay_date=stay_date,
                    checkout_date=checkout_date,
                    status="skipped_resume",
                    attempts=int((checkpoint.get("retry_counts_by_date") or {}).get(date_key) or 0),
                    records_collected=int(((checkpoint.get("date_statuses") or {}).get(date_key) or {}).get("records_collected") or 0),
                )
                save_checkpoint(checkpoint)
                push_date_rows(checkpoint)
                continue

            current_base = (date_index - 1) / max(len(planned_dates), 1)
            progress(current_base * 0.86 + 0.03, f"Collecting {config.source}: {date_key} to {checkout_date.isoformat()}")
            log(f"Starting date {date_index} of {len(planned_dates)}: {date_key}")
            checkpoint = update_checkpoint_date(
                checkpoint,
                stay_date=stay_date,
                checkout_date=checkout_date,
                status="running",
                attempts=0,
                records_collected=0,
            )
            save_checkpoint(checkpoint)
            push_date_rows(checkpoint)

            date_completed = False
            last_error = None
            max_attempts = retry_settings.max_retries_per_date if retry_settings.retry_failed_dates_automatically else 1
            for attempt in range(1, max_attempts + 1):
                if stop_event.is_set():
                    log("Stop requested. Saving current state before stopping.")
                    break
                log(f"Attempt {attempt} of {max_attempts} for {date_key}")
                update_heartbeat(
                    HEARTBEAT_FILE,
                    job_id=getattr(queue, "job_id", None),
                    instance_id=INSTANCE_CONFIG.instance_id,
                    current_date=date_key,
                    status="running",
                    last_log_message=last_log_message,
                )
                options = make_options(attempt)
                try:
                    simulate_date = os.getenv("SIMULATE_BROWSER_CLOSE_ON_DATE")
                    simulate_repeated_date = os.getenv("SIMULATE_REPEATED_FAILURE_DATE")
                    if simulate_repeated_date and simulate_repeated_date == date_key:
                        raise BrowserClosedError(f"Simulated repeated browser close on {date_key}")
                    if simulate_date and simulate_date == date_key and attempt == 1:
                        raise BrowserClosedError(f"Simulated browser closed on {date_key}")
                    raw_results = collector.collect(
                        config.city_or_region,
                        stay_date,
                        checkout_date,
                        config.adults,
                        config.currency,
                        config.max_hotels,
                        config.collect_all_available,
                        options,
                        lambda value, message, base=current_base: progress(min(0.90, base * 0.86 + value / max(len(planned_dates), 1) * 0.86), message),
                        log,
                    )
                    date_results, output_files = save_current_date_results(
                        stay_date=stay_date,
                        checkout_date=checkout_date,
                        raw_results=raw_results,
                        run_id_value=run_id,
                        options=options,
                    )
                    output_files["partial_excel"] = None
                    checkpoint = update_checkpoint_date(
                        checkpoint,
                        stay_date=stay_date,
                        checkout_date=checkout_date,
                        status="completed" if date_results else "completed_partial",
                        attempts=attempt,
                        records_collected=len(date_results),
                        output_files=output_files,
                        metrics=options.stats,
                    )
                    save_checkpoint(checkpoint)
                    push_date_rows(checkpoint)
                    completed_dates.add(date_key)
                    date_completed = True
                    log(f"Date completed: {date_key}; records collected: {len(date_results)}")
                    memory = guard.check(force=True)
                    log(
                        "Memory telemetry: "
                        f"date={date_key} rows_inserted={len(date_results)} python_rss_mb={memory.python_rss_mb} "
                        f"chromium_rss_mb={memory.browser_rss_mb} available_ram_mb={memory.available_ram_mb} "
                        f"swap_used_mb={memory.swap_used_mb} browser_processes={memory.browser_process_count} "
                        f"disk_free_gb={memory.disk_free_gb} elapsed_seconds={round(time.perf_counter() - perf_start, 2)}"
                    )
                    if config.db_backend == "sqlite":
                        wal_busy, wal_pages, wal_checkpointed = sqlite_wal_checkpoint("PASSIVE")
                        log(
                            "SQLite WAL checkpoint: "
                            f"busy={wal_busy} pages={wal_pages} checkpointed={wal_checkpointed}"
                        )
                    del raw_results
                    del date_results
                    gc.collect()
                    completed_count = len([row for row in date_status_rows if row.get("status") in {"completed", "completed_partial", "skipped_resume"}])
                    if retry_settings.auto_export_partial_excel and should_export_snapshot(completed_count, last_snapshot_time, retry_settings.partial_export_frequency):
                        export_partial("running", snapshot=True)
                        last_snapshot_time = time.monotonic()
                    break
                except ResourceLimitExceeded as exc:
                    if hasattr(stop_event, "set"):
                        try:
                            stop_event.set("resource_limit")
                        except TypeError:
                            stop_event.set()
                    last_error = str(exc)
                    checkpoint = update_checkpoint_date(
                        checkpoint,
                        stay_date=stay_date,
                        checkout_date=checkout_date,
                        status="stopped_resource_limit",
                        attempts=attempt,
                        records_collected=0,
                        error=last_error,
                        metrics=exc.snapshot.as_dict(),
                    )
                    save_checkpoint(checkpoint)
                    push_date_rows(checkpoint)
                    raise
                except Exception as exc:
                    if stop_event.is_set():
                        last_error = "Stop requested by user."
                        checkpoint = update_checkpoint_date(
                            checkpoint,
                            stay_date=stay_date,
                            checkout_date=checkout_date,
                            status="stopped_by_user",
                            attempts=attempt,
                            records_collected=0,
                            error=last_error,
                        )
                        save_checkpoint(checkpoint)
                        push_date_rows(checkpoint)
                        break
                    classified = classify_scraper_error(exc)
                    last_error = str(classified)
                    if is_browser_closed_error(exc):
                        classified = BrowserClosedError(str(exc))
                    log(f"Attempt {attempt} failed for {date_key}: {classified.reason}: {classified}")
                    checkpoint = update_checkpoint_date(
                        checkpoint,
                        stay_date=stay_date,
                        checkout_date=checkout_date,
                        status="running",
                        attempts=attempt,
                        records_collected=0,
                        error=last_error,
                    )
                    save_checkpoint(checkpoint)
                    push_date_rows(checkpoint)
                    if isinstance(classified, AccessRestrictionError):
                        log("Access restriction or CAPTCHA detected. No bypass attempted; marking date blocked.")
                        checkpoint = update_checkpoint_date(
                            checkpoint,
                            stay_date=stay_date,
                            checkout_date=checkout_date,
                            status="blocked_or_access_restricted",
                            attempts=attempt,
                            records_collected=0,
                            error=last_error,
                            metrics=getattr(options, "stats", {}),
                        )
                        save_checkpoint(checkpoint)
                        push_date_rows(checkpoint)
                        date_completed = True
                        break
                    if isinstance(classified, FatalScraperConfigError):
                        fatal_error = last_error
                        raise
                    if attempt < max_attempts:
                        log(f"Retrying date after {retry_settings.retry_delay_seconds} seconds.")
                        slept = 0
                        while slept < retry_settings.retry_delay_seconds and not stop_event.is_set():
                            time.sleep(min(1, retry_settings.retry_delay_seconds - slept))
                            slept += 1
                        continue
                    log(f"Date failed after retries: {date_key}")
                    checkpoint = update_checkpoint_date(
                        checkpoint,
                        stay_date=stay_date,
                        checkout_date=checkout_date,
                        status="failed_after_retries",
                        attempts=attempt,
                        records_collected=0,
                        error=last_error,
                        metrics=getattr(options, "stats", {}),
                    )
                    save_checkpoint(checkpoint)
                    push_date_rows(checkpoint)
                    if not retry_settings.continue_if_date_fails:
                        fatal_error = last_error
                        raise
            if stop_event.is_set() and not date_completed:
                checkpoint = update_checkpoint_date(
                    checkpoint,
                    stay_date=stay_date,
                    checkout_date=checkout_date,
                    status="stopped_by_user",
                    attempts=int((checkpoint.get("retry_counts_by_date") or {}).get(date_key) or 0),
                    records_collected=0,
                    error="Stop requested by user.",
                )
                save_checkpoint(checkpoint)
                push_date_rows(checkpoint)
                break
            progress(date_index / max(len(planned_dates), 1), f"Finished date {date_index} of {len(planned_dates)}")

        status = final_run_status(date_status_rows, stop_event.is_set(), fatal_error)
        summary = build_summary(status)
        if config.db_backend == "sqlite":
            final_excel_path = export_sqlite_run_to_excel(run_id, summary, EXPORT_DIR)
        else:
            final_excel_path = export_results_to_excel(normalized_results, summary, EXPORT_DIR)
        update_collection_run_excel_path(run_id, str(final_excel_path), backend=config.db_backend)
        csv_payload = export_final_csv(
            normalized_results,
            summary,
            session_id=run_id,
            log_callback=log,
            stream_sqlite_run_id=run_id if config.db_backend == "sqlite" else None,
        )
        update_collection_run_status(run_id, status, fatal_error, backend=config.db_backend)
        checkpoint["status"] = status
        checkpoint.setdefault("output_files", {})["final_excel"] = str(final_excel_path)
        checkpoint.setdefault("output_files", {})["final_csv"] = csv_payload.get("csv_file_path")
        checkpoint.setdefault("output_files", {})["final_csv_status"] = csv_payload.get("csv_export_status")
        save_checkpoint(checkpoint)
        queue.put(("complete", {"results": normalized_results, "summary": summary, "excel_file_path": str(final_excel_path), "status": status, **csv_payload}))
        update_status_file(status=status, progress_percent=100.0, current_message=f"Collection finished: {status}", latest_excel_file=str(final_excel_path), **csv_payload, **price_validation_summary(normalized_results))
        log(f"Database run status updated: {status}")
        log(f"Excel file created: {final_excel_path}")
        update_heartbeat(HEARTBEAT_FILE, job_id=getattr(queue, "job_id", None), instance_id=INSTANCE_CONFIG.instance_id, current_date=None, status=status, last_log_message=f"Collection finished: {status}")
    except Exception as exc:
        error_message = str(exc)
        fatal_error = fatal_error or error_message
        if isinstance(exc, ResourceLimitExceeded):
            status = "stopped_resource_limit"
        elif isinstance(exc, FatalScraperConfigError):
            status = "fatal_config_error"
        elif not date_status_rows and not normalized_results:
            status = "fatal_startup_error"
        else:
            status = final_run_status(date_status_rows, stop_event.is_set(), fatal_error)
        record_fatal_error(
            exc=exc,
            job_id=getattr(queue, "job_id", None),
            status=status,
            current_step="run_resilient_collection_job",
            current_date=None,
            log_file=getattr(queue, "instance_log_file", None),
            queue=None,
        )
        log(f"Fatal job error after saving checkpoint/partials: {error_message}")
        csv_payload: dict[str, Any] = {}
        if run_id is not None:
            if normalized_results and status != "fatal_config_error":
                summary = build_summary(status)
                if config.db_backend == "sqlite":
                    excel_path = export_sqlite_run_to_excel(run_id, summary, EXPORT_DIR, filename="partial_current_run.xlsx")
                else:
                    excel_path = export_results_to_excel(normalized_results, summary, EXPORT_DIR, filename="partial_current_run.xlsx")
                update_collection_run_excel_path(run_id, str(excel_path), backend=config.db_backend)
                csv_payload = export_final_csv(
                    normalized_results,
                    summary,
                    session_id=run_id,
                    log_callback=log,
                    stream_sqlite_run_id=run_id if config.db_backend == "sqlite" else None,
                )
                queue.put(("complete", {"results": normalized_results, "summary": summary, "excel_file_path": str(excel_path), "status": status, **csv_payload}))
            elif status != "fatal_config_error":
                summary = build_summary(status)
                csv_payload = export_final_csv([], summary, session_id=run_id, log_callback=log)
            update_collection_run_status(run_id, status, error_message, backend=config.db_backend)
        queue.put(("failed", {"status": status, "error": error_message, **csv_payload}))
        put_progress(queue, 1.0, f"Collection finished: {status}")
        update_status_file(status=status, progress_percent=100.0, current_message=f"Collection finished: {status}", last_error=error_message, **csv_payload)
        update_heartbeat(HEARTBEAT_FILE, job_id=getattr(queue, "job_id", None), instance_id=INSTANCE_CONFIG.instance_id, current_date=None, status=status, last_log_message=error_message)


def run_batch_collection_job(config: CollectionConfig, stop_event: Event, queue: Queue) -> None:
    started_at = datetime.now()
    batch_timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    blocks = build_batch_blocks(config.checkin_start, config.checkin_end, config.batch_mode, config.custom_block_days)
    batch_checkpoint_lock = Lock()
    checkpoint_payload = load_batch_checkpoint(config, blocks) if config.resume_previous_run else None
    if checkpoint_payload:
        batch_id = str(checkpoint_payload.get("batch_id") or f"batch_{batch_timestamp}")
        batch_timestamp = str(checkpoint_payload.get("timestamp") or batch_timestamp)
        batch_folder = Path(str(checkpoint_payload.get("output_folder") or EXPORT_DIR / batch_id))
        put_log(queue, f"Resuming batch {batch_id}.")
    else:
        batch_id = f"batch_{batch_timestamp}"
        batch_folder = EXPORT_DIR / batch_id
        checkpoint_payload = {
            "batch_id": batch_id,
            "timestamp": batch_timestamp,
            "output_folder": str(batch_folder.resolve()),
            "status": "running",
            "blocks": {},
        }
    batch_folder.mkdir(parents=True, exist_ok=True)
    save_batch_checkpoint(config, blocks, checkpoint_payload)

    block_rows = [block_status_row(block) for block in blocks]
    queue.put(("batch_blocks", block_rows))
    put_log(queue, f"Batch parallel collection started: {batch_id}")
    put_log(queue, f"Blocks planned: {len(blocks)}. Maximum parallel workers: {config.max_parallel_workers}.")
    put_log(queue, "Internal parallel workers are experimental. Recommended method is Multi Instance Mode.")
    put_log(queue, f"Batch output folder: {batch_folder.resolve()}")
    put_log(queue, f"Instance browser profile root: {BROWSER_PROFILE_DIR.resolve()}")

    existing_blocks = checkpoint_payload.setdefault("blocks", {})
    selected_blocks = set(config.selected_batch_blocks)
    runnable_blocks: list[BatchBlock] = []
    for block in blocks:
        block_key = str(block.block_id)
        previous = existing_blocks.get(block_key) if isinstance(existing_blocks.get(block_key), dict) else {}
        previous_status = previous.get("status")
        should_skip_completed = config.rerun_batch_scope == "resume_incomplete" and previous_status == "completed"
        should_run_selected = not selected_blocks or block.block_id in selected_blocks
        if should_skip_completed or not should_run_selected:
            block_rows[block.block_id - 1].update(
                {
                    "status": previous_status or "skipped",
                    "completion percentage": 100.0 if previous_status == "completed" else 0.0,
                    "hotels collected": previous.get("hotels_collected", 0),
                    "Excel file path": previous.get("excel_file_path") or "-",
                    "stop reason": previous.get("stop_reason") or "skipped by resume",
                }
            )
            continue
        runnable_blocks.append(block)

    queue.put(("batch_blocks", block_rows))
    if not runnable_blocks:
        put_log(queue, "No batch blocks need to run. Creating master workbook from completed checkpoint entries.")

    def update_block(block_id: int, **updates: Any) -> None:
        block_rows[block_id - 1].update(updates)
        completed_units = sum(float(row.get("completion percentage") or 0) for row in block_rows)
        put_progress(queue, completed_units / max(len(block_rows) * 100, 1), f"Batch running: block {block_id}")
        queue.put(("batch_blocks", [dict(row) for row in block_rows]))

    def persist_block_checkpoint(block_id: int, data: dict[str, Any]) -> None:
        with batch_checkpoint_lock:
            blocks_payload = checkpoint_payload.setdefault("blocks", {})
            current = blocks_payload.get(str(block_id)) if isinstance(blocks_payload.get(str(block_id)), dict) else {}
            current.update(data)
            blocks_payload[str(block_id)] = current
            save_batch_checkpoint(config, blocks, checkpoint_payload)

    def run_one_block(block: BatchBlock) -> dict[str, Any]:
        block_started = datetime.now()
        safe_range = f"{block.start_date.isoformat()}_to_{block.end_date.isoformat()}"
        block_folder = batch_folder / f"block_{block.block_id:02d}_{safe_range}"
        screenshot_folder = block_folder / "screenshots"
        debug_folder = block_folder / "debug"
        block_folder.mkdir(parents=True, exist_ok=True)
        screenshot_folder.mkdir(parents=True, exist_ok=True)
        debug_folder.mkdir(parents=True, exist_ok=True)
        log_file = block_folder / "block.log"
        log_file.write_text("", encoding="utf-8")
        run_id = None
        saved_count = 0
        normalized_results: list[dict[str, Any]] = []
        options = CollectorOptions(
            collect_all_available=config.collect_all_available,
            max_scroll_minutes=config.max_scroll_minutes,
            selected_star_ratings=config.selected_star_ratings,
            include_unknown_star_rating=config.include_unknown_star_rating,
            debug_mode=config.debug_mode,
            screenshots_enabled=config.screenshots_enabled,
            headless=config.headless,
            fast_mode=False,
            performance_mode=config.performance_mode,
            block_images_and_fonts=config.block_images_and_fonts,
            stop_event=stop_event,
            hotels_only=config.hotels_only,
            disable_filters_during_complete_collection=config.disable_filters_during_complete_collection,
            ultra_reliable_loading_mode=config.ultra_reliable_loading_mode,
            screenshot_dir=screenshot_folder,
            debug_dir=debug_folder,
            browser_profile_dir=(BROWSER_PROFILE_DIR / f"block_{block.block_id:02d}_headless") if config.headless and is_booking_source(config.source) else (BROWSER_PROFILE_DIR / f"block_{block.block_id:02d}"),
        )

        def block_status_callback(updates: dict[str, Any]) -> None:
            update_status_file(**updates)

        options.status_callback = block_status_callback

        def block_log(message: str) -> None:
            text = f"Block {block.block_id}: {message}"
            append_block_log(log_file, message)
            put_log(queue, text)

        def block_progress(value: float, message: str) -> None:
            update_block(
                block.block_id,
                status="running",
                **{"current stay date": message},
                **{"completion percentage": round(value * 100, 1)},
                runtime=str(datetime.now() - block_started).split(".")[0],
            )

        try:
            block_log(f"Starting {block.start_date.isoformat()} to {block.end_date.isoformat()}.")
            block_log(f"Output folder: {block_folder.resolve()}")
            block_log(f"Browser profile folder: {(BROWSER_PROFILE_DIR / f'block_{block.block_id:02d}').resolve()}")
            persist_block_checkpoint(
                block.block_id,
                {
                    "status": "running",
                    "batch_id": batch_id,
                    "source": config.source,
                    "city": config.city_or_region,
                    "block_start_date": block.start_date.isoformat(),
                    "block_end_date": block.end_date.isoformat(),
                    "started_at": block_started.isoformat(sep=" ", timespec="seconds"),
                    "output_folder": str(block_folder.resolve()),
                    "log_file_path": str(log_file.resolve()),
                    "debug_file_path": str((block_folder / "block_debug.json").resolve()),
                },
            )
            with _db_write_lock_for(config):
                run_id = create_collection_run(
                    config.source,
                    config.city_or_region,
                    block.start_date,
                    calculate_checkout_date(block.end_date, config.nights),
                    config.nights,
                    config.adults,
                    config.currency,
                    0 if config.collect_all_available else config.max_hotels,
                    selected_star_ratings=",".join(str(value) for value in config.selected_star_ratings),
                    include_unknown_star_rating=config.include_unknown_star_rating,
                    backend=config.db_backend,
                )
            collector = get_collector(config.source)
            dates = block.stay_dates
            for date_index, stay_date in enumerate(dates, start=1):
                if stop_event.is_set():
                    block_log("Stop requested before next stay date.")
                    break
                checkout_date = calculate_checkout_date(stay_date, config.nights)
                update_block(
                    block.block_id,
                    status="running",
                    **{
                        "current stay date": stay_date.isoformat(),
                        "runtime": str(datetime.now() - block_started).split(".")[0],
                    },
                )
                block_log(f"Collecting stay date {date_index} of {len(dates)}: {stay_date.isoformat()} to {checkout_date.isoformat()}.")
                base = (date_index - 1) / max(len(dates), 1)
                raw_results = collector.collect(
                    config.city_or_region,
                    stay_date,
                    checkout_date,
                    config.adults,
                    config.currency,
                    config.max_hotels,
                    config.collect_all_available,
                    options,
                    lambda value, message, base=base: block_progress(
                        min(0.95, base + value / max(len(dates), 1)),
                        message,
                    ),
                    block_log,
                )
                if not is_booking_source(config.source):
                    raw_results = filter_results_by_star_rating(raw_results, options, queue)
                date_results = [normalize_hotel_result({**row, "collection_run_id": run_id}) for row in raw_results]
                normalized_results.extend(date_results)
                with _db_write_lock_for(config):
                    saved_count += insert_hotel_results(date_results, backend=config.db_backend)
                update_block(
                    block.block_id,
                    **{
                        "hotels collected": len(normalized_results),
                        "completion percentage": round(date_index / max(len(dates), 1) * 95, 1),
                        "runtime": str(datetime.now() - block_started).split(".")[0],
                    },
                )
                persist_block_checkpoint(
                    block.block_id,
                    {
                        "status": "running",
                        "completed_dates": [value.isoformat() for value in dates[:date_index]],
                        "hotels_collected": len(normalized_results),
                    },
                )

            run_metadata = {
                "source": config.source,
                "city_or_region": config.city_or_region,
                "checkin_date": block.start_date,
                "checkout_date": calculate_checkout_date(block.end_date, config.nights),
                "number_of_nights": config.nights,
                "adults": config.adults,
                "currency": config.currency,
                "started_at": block_started,
                "completed_at": datetime.now(),
            }
            run_metadata.update(_collector_summary_metadata(options))
            summary = create_summary(normalized_results, run_metadata)
            filename = build_block_excel_filename(config.source, config.city_or_region, block.start_date, block.end_date, batch_timestamp)
            excel_path = export_results_to_excel(normalized_results, summary, block_folder, filename=filename)
            status = terminal_status_for_results(normalized_results, stop_event.is_set(), None)
            if str(options.stats.get("completion_status") or "").startswith("incomplete") and normalized_results:
                status = "completed_incomplete"
            with _db_write_lock_for(config):
                update_collection_run_excel_path(run_id, str(excel_path), backend=config.db_backend)
                update_collection_run_status(run_id, status, backend=config.db_backend)
            debug_file = write_block_debug_file(
                block_folder,
                {
                    "batch_id": batch_id,
                    "block_id": block.block_id,
                    "run_id": run_id,
                    "status": status,
                    "stats": options.stats,
                    "excel_file_path": str(excel_path),
                    "log_file_path": str(log_file.resolve()),
                    "completed_at": datetime.now(),
                },
            )
            persist_block_checkpoint(
                block.block_id,
                {
                    "status": status,
                    "completed_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                    "stop_reason": options.stats.get("final_stop_reason") or status,
                    "excel_file_path": str(excel_path),
                    "log_file_path": str(log_file.resolve()),
                    "debug_file_path": str(debug_file),
                    "hotels_collected": len(normalized_results),
                    "run_id": run_id,
                },
            )
            update_block(
                block.block_id,
                status=status,
                **{
                    "current stay date": "-",
                    "hotels collected": len(normalized_results),
                    "completion percentage": 100.0,
                    "runtime": str(datetime.now() - block_started).split(".")[0],
                    "stop reason": options.stats.get("final_stop_reason") or status,
                    "Excel file path": str(excel_path),
                },
            )
            block_log(f"Completed with status {status}. Excel file: {excel_path}")
            return {"block": block, "status": status, "results": normalized_results, "summary": summary, "excel_file_path": str(excel_path)}
        except Exception as exc:
            error_message = str(exc)
            status = terminal_status_for_results(normalized_results, stop_event.is_set(), error_message)
            if run_id is not None:
                with _db_write_lock_for(config):
                    update_collection_run_status(run_id, status, error_message, backend=config.db_backend)
            debug_file = write_block_debug_file(
                block_folder,
                {"batch_id": batch_id, "block_id": block.block_id, "run_id": run_id, "status": status, "error": error_message, "stats": options.stats},
            )
            persist_block_checkpoint(
                block.block_id,
                {
                    "status": status,
                    "completed_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                    "stop_reason": error_message,
                    "excel_file_path": None,
                    "log_file_path": str(log_file.resolve()),
                    "debug_file_path": str(debug_file),
                    "hotels_collected": len(normalized_results),
                    "run_id": run_id,
                },
            )
            update_block(
                block.block_id,
                status=status,
                **{
                    "hotels collected": len(normalized_results),
                    "runtime": str(datetime.now() - block_started).split(".")[0],
                    "stop reason": error_message,
                },
            )
            block_log(f"Failed with status {status}: {error_message}")
            return {"block": block, "status": status, "results": normalized_results, "summary": {}, "error": error_message}

    results_by_block: list[dict[str, Any]] = []
    max_workers = max(1, min(int(config.max_parallel_workers), 4, len(runnable_blocks) or 1))
    if runnable_blocks:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(run_one_block, block): block for block in runnable_blocks}
            for future in as_completed(futures):
                results_by_block.append(future.result())

    all_results: list[dict[str, Any]] = []
    for result in results_by_block:
        if result.get("status") in {"completed", "completed_incomplete", "completed_with_partial_results"}:
            all_results.extend(result.get("results") or [])

    checkpoint_payload = load_batch_checkpoint(config, blocks) or checkpoint_payload
    completed_checkpoint_results: list[dict[str, Any]] = []
    for block in blocks:
        payload = (checkpoint_payload.get("blocks") or {}).get(str(block.block_id), {})
        if payload.get("run_id") and not any(item.get("block").block_id == block.block_id for item in results_by_block if item.get("block")):
            try:
                completed_checkpoint_results.extend(fetch_results_by_run_id(int(payload["run_id"]), backend=config.db_backend))
            except Exception as exc:
                put_log(queue, f"Could not reload completed block {block.block_id} from database: {exc}")
    all_results.extend(completed_checkpoint_results)

    failed_or_incomplete = []
    final_block_rows = []
    checkpoint_blocks = checkpoint_payload.get("blocks") or {}
    for block in blocks:
        payload = checkpoint_blocks.get(str(block.block_id), {})
        row = {
            "block_id": block.block_id,
            "block_start_date": block.start_date.isoformat(),
            "block_end_date": block.end_date.isoformat(),
            "status": payload.get("status") or block_rows[block.block_id - 1].get("status"),
            "stay_dates": len(block.stay_dates),
            "hotels_collected": payload.get("hotels_collected", block_rows[block.block_id - 1].get("hotels collected", 0)),
            "excel_file_path": payload.get("excel_file_path") or block_rows[block.block_id - 1].get("Excel file path"),
            "log_file_path": payload.get("log_file_path"),
            "debug_file_path": payload.get("debug_file_path"),
            "stop_reason": payload.get("stop_reason"),
        }
        final_block_rows.append(row)
        if row["status"] not in {"completed"}:
            failed_or_incomplete.append(row)

    master_summary = {
        "batch_id": batch_id,
        "source": config.source,
        "city_or_region": config.city_or_region,
        "start_date": config.checkin_start.isoformat(),
        "end_date": config.checkin_end.isoformat(),
        "split_mode": config.batch_mode,
        "maximum_parallel_workers": config.max_parallel_workers,
        "total_blocks": len(blocks),
        "total_stay_dates": sum(len(block.stay_dates) for block in blocks),
        "total_hotels_collected": len(all_results),
        "started_at": started_at,
        "completed_at": datetime.now(),
        "output_folder": str(batch_folder.resolve()),
        "checkpoint_path": str(_batch_checkpoint_path(config).resolve()),
    }
    master_filename = build_master_excel_filename(config.source, config.city_or_region, config.checkin_start, config.checkin_end, batch_timestamp)
    master_excel_path = export_batch_master_excel(all_results, final_block_rows, master_summary, failed_or_incomplete, batch_folder, master_filename)
    checkpoint_payload["status"] = "completed" if not failed_or_incomplete and not stop_event.is_set() else "completed_incomplete"
    checkpoint_payload["master_excel_file_path"] = str(master_excel_path)
    save_batch_checkpoint(config, blocks, checkpoint_payload)
    status = "stopped_by_user" if stop_event.is_set() else checkpoint_payload["status"]
    summary = create_summary(
        all_results,
        {
            "source": config.source,
            "city_or_region": config.city_or_region,
            "checkin_date": config.checkin_start,
            "checkout_date": calculate_checkout_date(config.checkin_end, config.nights),
            "number_of_nights": config.nights,
            "adults": config.adults,
            "currency": config.currency,
            "started_at": started_at,
            "completed_at": datetime.now(),
        },
    )
    csv_payload = export_final_csv(all_results, summary, session_id=batch_id, log_callback=lambda message: put_log(queue, message))
    checkpoint_payload["master_csv_file_path"] = csv_payload.get("csv_file_path")
    checkpoint_payload["master_csv_export_status"] = csv_payload.get("csv_export_status")
    save_batch_checkpoint(config, blocks, checkpoint_payload)
    update_status_file(status=status, progress_percent=100.0, current_message=f"Batch collection finished: {status}", latest_excel_file=str(master_excel_path), **csv_payload)
    queue.put(("complete", {"results": all_results, "summary": summary, "excel_file_path": str(master_excel_path), "status": status, **csv_payload}))
    queue.put(("batch_summary", master_summary))
    put_progress(queue, 1.0, f"Batch collection finished: {status}")
    put_log(queue, f"Master Excel file created: {master_excel_path}")


def drain_job_queue() -> None:
    job = st.session_state.current_job
    if not job:
        return
    queue = job["queue"]
    while True:
        try:
            item = queue.get_nowait()
        except Empty:
            break
        kind = item[0]
        if kind == "log":
            st.session_state.logs.append(item[1])
            if len(st.session_state.logs) > 500:
                del st.session_state.logs[:-500]
        elif kind == "progress":
            st.session_state.progress = item[1]
            st.session_state.status_message = item[2]
        elif kind == "metrics":
            st.session_state.metrics = item[1]
        elif kind == "run_id":
            st.session_state.run_id = item[1]
        elif kind == "records_saved":
            st.session_state.records_saved = item[1]
        elif kind == "batch_blocks":
            st.session_state.batch_blocks = item[1]
        elif kind == "batch_summary":
            st.session_state.batch_summary = item[1]
        elif kind == "date_status_rows":
            st.session_state.date_status_rows = item[1]
        elif kind == "complete":
            payload = item[1]
            st.session_state.results = payload["results"]
            st.session_state.summary = payload["summary"]
            st.session_state.excel_file_path = payload["excel_file_path"]
            st.session_state.csv_file_path = payload.get("csv_file_path")
            st.session_state.csv_export_status = payload.get("csv_export_status")
            st.session_state.csv_export_error = payload.get("csv_export_error")
            st.session_state.csv_rows_exported = payload.get("csv_rows_exported", len(payload["results"]))
            st.session_state.status = payload["status"]
        elif kind == "failed":
            payload = item[1]
            st.session_state.status = payload["status"]
            st.session_state.status_message = payload["error"]
            if "csv_file_path" in payload:
                st.session_state.csv_file_path = payload.get("csv_file_path")
                st.session_state.csv_export_status = payload.get("csv_export_status")
                st.session_state.csv_export_error = payload.get("csv_export_error")
                st.session_state.csv_rows_exported = payload.get("csv_rows_exported", 0)

    process = job["process"]
    stop_requested_at = job.get("stop_requested_at")
    if process.is_alive() and stop_requested_at is not None:
        elapsed = time.monotonic() - float(stop_requested_at)
        if elapsed >= WORKER_STOP_GRACE_SECONDS:
            cleanup_owned_browser_processes(process.pid, lambda message: st.session_state.logs.append(message))
            process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            st.session_state.status = "stopped_by_user"
            st.session_state.status_message = "Worker exceeded the stop grace period and was terminated with its scraper-owned browser descendants."
            update_status_file(
                status="stopped_by_user",
                current_message=st.session_state.status_message,
                forced_worker_termination=True,
            )
    if not process.is_alive():
        process.join(timeout=0.2)
        exit_code = process.exitcode
        if st.session_state.status in {"starting", "running", "stopping"}:
            if stop_requested_at is not None:
                st.session_state.status = "stopped_by_user"
                st.session_state.status_message = "Collection stopped by user."
            elif exit_code not in {0, None}:
                st.session_state.status = "application_exception"
                st.session_state.status_message = f"Worker exited unexpectedly with code {exit_code}."
                update_status_file(status="application_exception", current_message=st.session_state.status_message)
        try:
            job["queue"].close()
            job["queue"].join_thread()
        except (AttributeError, OSError, ValueError):
            pass
        st.session_state.current_job = None


def start_background_job(config: CollectionConfig) -> None:
    reset_current_results()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"instance_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_file.write_text("", encoding="utf-8")
    job_id = str(uuid4())
    stop_event = CancellationSignal(context.Event(), CANCEL_FILE, job_id)
    stop_event.reset()
    write_json_file(
        STATUS_FILE,
        {
            "job_id": job_id,
            "status": "starting",
            "current_message": "Collection job starting",
            "progress_percent": 0.0,
            "last_updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "log_file": str(log_file.resolve()),
        },
    )
    runner = run_resilient_collection_job if config.batch_mode == "single" else run_batch_collection_job
    process = context.Process(
        target=run_background_job_with_fatal_guard,
        args=(runner, config, stop_event, queue, job_id, str(log_file.resolve())),
        name=f"ota-scraper-{job_id[:8]}",
        daemon=False,
    )
    process.start()
    st.session_state.current_job = {
        "id": job_id,
        "process": process,
        "queue": queue,
        "stop_event": stop_event,
        "log_file": str(log_file.resolve()),
        "worker_pid": process.pid,
        "stop_requested_at": None,
    }
    st.session_state.job_started_at = datetime.now()
    st.session_state.status = "running"
    st.session_state.status_message = f"Collection job started: {job_id}"
    st.session_state.active_log_file_path = str(log_file.resolve())
    st.session_state.logs = [f"[{datetime.now().strftime('%H:%M:%S')}] Worker process started: {job_id} PID={process.pid}", f"[{datetime.now().strftime('%H:%M:%S')}] Log file: {log_file.resolve()}"]


def render_job_status() -> None:
    file_status = maybe_mark_stale_status_file(read_current_status_file())
    if file_status:
        st.session_state.progress = max(0.0, min(1.0, float(file_status.get("progress_percent") or 0) / 100))
        st.session_state.status_message = str(file_status.get("current_message") or st.session_state.status_message)
        if file_status.get("status"):
            st.session_state.status = str(file_status["status"])
        st.session_state.metrics = {**(st.session_state.metrics or {}), **file_status}
    progress_box = st.empty()
    status_box = st.empty()
    progress_box.progress(st.session_state.progress)

    status = st.session_state.status
    message = st.session_state.status_message
    if status in {"failed", "blocked_or_access_restricted", "browser_crash", "application_exception", "fatal_startup_error", "fatal_config_error"}:
        status_box.error(message)
    elif status == "stopped_resource_limit":
        status_box.error(f"{message} Completed data is safe in SQLite and the run is resumable.")
    elif status in {"completed_with_zero_results", "completed_with_partial_results", "stopped_by_user", "stopped_by_user_with_partial_results", "stopping"}:
        status_box.warning(message)
    elif status in {"completed", "completed_all_dates"}:
        status_box.success(message)
    else:
        status_box.info(message)

    metrics = st.session_state.metrics or {}
    metric_cols = st.columns(5)
    metric_cols[0].metric("Hotels/min", metrics.get("hotels_collected_per_minute", "-"))
    metric_cols[1].metric("Current date", metrics.get("current_checkin_date") or metrics.get("current_stay_date", "-"))
    metric_cols[2].metric("Card count", metrics.get("visible_card_count") or metrics.get("current_hotel_card_count", "-"))
    metric_cols[3].metric("Elapsed", metrics.get("elapsed_seconds", metrics.get("elapsed_time", "-")))
    metric_cols[4].metric("Remaining", metrics.get("estimated_remaining_seconds", metrics.get("estimated_remaining_time", "-")))
    terminal_statuses = {
        "completed",
        "completed_all_dates",
        "completed_with_failed_dates",
        "completed_with_blocked_dates",
        "completed_with_partial_results",
        "completed_with_zero_results",
        "completed_incomplete",
        "stopped_by_user",
        "stopped_by_user_with_partial_results",
        "stopped_resource_limit",
        "browser_crash",
        "application_exception",
        "refused_second_scraper",
        "fatal_error_with_partial_results",
        "failed",
        "fatal_startup_error",
        "fatal_config_error",
    }
    if status in terminal_statuses:
        rows_collected = metrics.get("hotels_collected_total")
        if rows_collected is None:
            rows_collected = st.session_state.csv_rows_exported or st.session_state.records_saved or len(st.session_state.results)
        st.write(f"Rows collected: {rows_collected}")
        csv_status = metrics.get("csv_export_status") or st.session_state.csv_export_status
        csv_path = metrics.get("csv_file_path") or st.session_state.csv_file_path
        csv_error = metrics.get("csv_export_error") or st.session_state.csv_export_error
        if csv_status == "succeeded":
            st.success("Final CSV export succeeded.")
            st.write(f"Final CSV path: {csv_path}")
        elif csv_status == "failed":
            st.warning("Final CSV export failed.")
            if csv_error:
                st.code(str(csv_error), language="text")
            if metrics.get("csv_export_traceback_file"):
                st.write(f"CSV export traceback: {metrics.get('csv_export_traceback_file')}")
        else:
            st.info("Final CSV export has not run for this session yet.")
    if metrics.get("latest_screenshot_path"):
        st.write(f"Latest screenshot path: {metrics.get('latest_screenshot_path')}")
    if metrics.get("last_error"):
        st.warning(f"Latest error: {metrics.get('last_error')}")
    if heartbeat_is_stale(HEARTBEAT_FILE):
        st.warning(
            "Warning: scraper heartbeat is stale. The process may have stopped. "
            "Partial results should be available in the partial and exports folders."
        )

    if file_status:
        with st.expander("Instance status file", expanded=True):
            st.caption(str(STATUS_FILE.resolve()))
            st.json(file_status)

    if st.session_state.batch_blocks:
        st.subheader("Batch block progress")
        display_rows = []
        for row in st.session_state.batch_blocks:
            display_rows.append(
                {
                    "block_id": row.get("block_id"),
                    "date range": row.get("date range"),
                    "status": row.get("status"),
                    "current stay date": row.get("current stay date") or row.get("current_stay_date"),
                    "hotels collected": row.get("hotels collected"),
                    "completion percentage": row.get("completion percentage"),
                    "runtime": row.get("runtime"),
                    "stop reason": row.get("stop reason"),
                    "Excel file path": row.get("Excel file path"),
                }
            )
        st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

    date_rows = st.session_state.date_status_rows or metrics.get("date_status_rows") or []
    if date_rows:
        st.subheader("Per date status")
        st.dataframe(pd.DataFrame(date_rows), use_container_width=True, hide_index=True)
        if st.session_state.batch_summary:
            with st.expander("Batch run summary", expanded=False):
                st.json(st.session_state.batch_summary)

    with st.expander("Booking.com Loading Completeness", expanded=True):
        completeness_rows = [
            {"Metric": "Booking.com visible count", "Value": metrics.get("visible_result_count_text") or metrics.get("booking_visible_result_count") or "-"},
            {"Metric": "Target result count", "Value": metrics.get("target_result_count") or "-"},
            {"Metric": "Count is lower bound", "Value": "yes" if metrics.get("visible_result_count_is_lower_bound") else "no"},
            {"Metric": "Unique results loaded", "Value": metrics.get("unique_results_loaded", metrics.get("unique_hotels_collected", "-"))},
            {"Metric": "Estimated missing", "Value": metrics.get("estimated_missing_results") if metrics.get("estimated_missing_results") is not None else "-"},
            {"Metric": "Completion status", "Value": metrics.get("completion_status") or "-"},
            {"Metric": "Final stop reason", "Value": metrics.get("final_stop_reason") or "-"},
            {"Metric": "Load more seen count", "Value": metrics.get("load_more_button_seen_count", 0)},
            {"Metric": "Load more click count", "Value": metrics.get("load_more_clicks", 0)},
            {"Metric": "Load more success count", "Value": metrics.get("load_more_success_count", 0)},
            {"Metric": "Failed click count", "Value": metrics.get("load_more_click_fail_count", 0)},
            {"Metric": "Total scroll attempts", "Value": metrics.get("total_scroll_attempts", 0)},
            {"Metric": "Current highest price", "Value": metrics.get("highest_price_collected") or "-"},
            {"Metric": "Final screenshot path", "Value": metrics.get("final_bottom_screenshot_path") or "-"},
            {"Metric": "Latest screenshot path", "Value": metrics.get("latest_screenshot_path") or "-"},
            {"Metric": "Debug JSON path", "Value": metrics.get("full_loading_debug_json_path") or "-"},
            {"Metric": "Resume checkpoint path", "Value": metrics.get("resume_checkpoint_path") or "-"},
            {"Metric": "Rows with raw price", "Value": metrics.get("rows_with_raw_price", "-")},
            {"Metric": "Rows with parsed price", "Value": metrics.get("rows_with_parsed_price", "-")},
            {"Metric": "Rows missing price", "Value": metrics.get("rows_missing_price", "-")},
            {"Metric": "Price debug file", "Value": metrics.get("price_pipeline_debug_file", "-")},
        ]
        st.dataframe(text_rows_dataframe(completeness_rows), use_container_width=True, hide_index=True)
        if metrics.get("collection_completeness_warning"):
            st.warning(str(metrics["collection_completeness_warning"]))

    with st.expander("Debug counters", expanded=False):
        debug_rows = [
            {"Counter": "Current stay date", "Value": metrics.get("current_stay_date", "-")},
            {"Counter": "Final URL", "Value": metrics.get("final_url") or "-"},
            {"Counter": "Page title", "Value": metrics.get("final_page_title") or "-"},
            {"Counter": "Visible result count text", "Value": metrics.get("visible_result_count_text") or "-"},
            {"Counter": "Target result count", "Value": metrics.get("target_result_count") or "-"},
            {"Counter": "Completion status", "Value": metrics.get("completion_status") or "-"},
            {"Counter": "Estimated missing results", "Value": metrics.get("estimated_missing_results") if metrics.get("estimated_missing_results") is not None else "-"},
            {"Counter": "Booking.com visible result count", "Value": metrics.get("booking_visible_result_count") or "-"},
            {"Counter": "Visible hotel cards count", "Value": metrics.get("visible_hotel_cards_count", 0)},
            {"Counter": "Unique hotels collected", "Value": metrics.get("unique_hotels_collected", "-")},
            {"Counter": "Unique results loaded", "Value": metrics.get("unique_results_loaded", "-")},
            {"Counter": "New hotels added in last cycle", "Value": metrics.get("new_hotels_added_last_cycle", 0)},
            {"Counter": "Load more clicks", "Value": metrics.get("load_more_clicks", 0)},
            {"Counter": "Load more success count", "Value": metrics.get("load_more_success_count", 0)},
            {"Counter": "Load more button seen count", "Value": metrics.get("load_more_button_seen_count", 0)},
            {"Counter": "Load more click failures", "Value": metrics.get("load_more_click_fail_count", 0)},
            {"Counter": "Consecutive no-new-unique attempts", "Value": metrics.get("consecutive_no_new_unique_results", 0)},
            {"Counter": "Consecutive no Load more seen", "Value": metrics.get("consecutive_no_load_more_seen", 0)},
            {"Counter": "Consecutive failed Load more clicks", "Value": metrics.get("consecutive_failed_load_more_clicks", 0)},
            {"Counter": "Last Load more error", "Value": metrics.get("last_load_more_error") or "-"},
            {"Counter": "Last Load more button text", "Value": metrics.get("load_more_last_button_text") or "-"},
            {"Counter": "Last Load more locator", "Value": metrics.get("load_more_last_button_strategy") or "-"},
            {"Counter": "Last Load more button box", "Value": metrics.get("load_more_last_button_box") or "-"},
            {"Counter": "Load more button currently visible", "Value": "yes" if metrics.get("load_more_last_button_visible") else "no"},
            {"Counter": "Last Load more enabled", "Value": "yes" if metrics.get("load_more_last_button_enabled") else "no"},
            {"Counter": "Cookie banner visible before Load more", "Value": "yes" if metrics.get("load_more_cookie_banner_visible_before_click") else "no"},
            {"Counter": "Cards before last Load more", "Value": metrics.get("hotel_cards_before_last_click", 0)},
            {"Counter": "Cards after last Load more", "Value": metrics.get("hotel_cards_after_last_click", 0)},
            {"Counter": "New cards after last Load more", "Value": metrics.get("new_cards_after_last_click", 0)},
            {"Counter": "Consecutive no-new-card attempts", "Value": metrics.get("consecutive_no_new_card_attempts", 0)},
            {"Counter": "Consecutive no-button attempts", "Value": metrics.get("consecutive_no_button_attempts", 0)},
            {"Counter": "Lowest price collected", "Value": metrics.get("lowest_price_collected") or "-"},
            {"Counter": "Highest price collected", "Value": metrics.get("highest_price_collected") or "-"},
            {"Counter": "Hotels with valid price", "Value": metrics.get("hotels_with_valid_price", 0)},
            {"Counter": "Hotels missing price", "Value": metrics.get("hotels_missing_price", 0)},
            {"Counter": "Maximum scroll time reached", "Value": "yes" if metrics.get("maximum_scroll_time_reached") else "no"},
            {"Counter": "Final stop reason", "Value": metrics.get("final_stop_reason") or "-"},
            {"Counter": "Final bottom screenshot path", "Value": metrics.get("final_bottom_screenshot_path") or "-"},
            {"Counter": "Difference visible vs collected", "Value": metrics.get("booking_result_count_difference") if metrics.get("booking_result_count_difference") is not None else "-"},
            {"Counter": "Completeness warning", "Value": metrics.get("collection_completeness_warning") or "-"},
            {"Counter": "Resume checkpoint path", "Value": metrics.get("resume_checkpoint_path") or "-"},
            {"Counter": "Cookie banner handled", "Value": "yes" if metrics.get("cookie_banner_handled") else "no"},
            {"Counter": "Unexpected property pages closed", "Value": metrics.get("unexpected_property_pages_closed", 0)},
            {"Counter": "Unexpected property navigations", "Value": metrics.get("unexpected_property_navigation_count", 0)},
            {"Counter": "Current result card count", "Value": metrics.get("current_hotel_card_count", "-")},
            {"Counter": "Final result card count", "Value": metrics.get("final_result_card_count", "-")},
            {"Counter": "Final extracted hotel count", "Value": metrics.get("final_extracted_hotel_count", "-")},
            {"Counter": "Average seconds per hotel", "Value": metrics.get("average_seconds_per_hotel", "-")},
            {"Counter": "Database insert time", "Value": metrics.get("database_insert_time", 0)},
            {"Counter": "Excel export time", "Value": metrics.get("excel_export_time", 0)},
            {"Counter": "Total runtime seconds", "Value": metrics.get("total_runtime_seconds", "-")},
            {"Counter": "Browser startup seconds", "Value": metrics.get("browser_startup_time_seconds", 0)},
            {"Counter": "Page load seconds", "Value": metrics.get("page_load_time_seconds", 0)},
            {"Counter": "Load more seconds", "Value": metrics.get("load_more_time_seconds", 0)},
            {"Counter": "Extraction seconds", "Value": metrics.get("extraction_time_seconds", 0)},
            {"Counter": "Hotels extracted", "Value": metrics.get("hotels_extracted", "-")},
            {"Counter": "Hotels kept after filters", "Value": metrics.get("hotels_kept_after_filters", "-")},
            {"Counter": "Hotels removed by hotels only filter", "Value": metrics.get("hotels_removed_by_hotels_only_filter", 0)},
            {"Counter": "Hotels with known star rating", "Value": metrics.get("hotels_with_known_star_rating", 0)},
            {"Counter": "Hotels with unknown star rating", "Value": metrics.get("hotels_with_unknown_star_rating", 0)},
            {"Counter": "Hotels removed by star filter", "Value": metrics.get("hotels_removed_by_star_filter", 0)},
            {"Counter": "Hotels kept after star filter", "Value": metrics.get("hotels_kept_after_star_filter", 0)},
        ]
        st.dataframe(text_rows_dataframe(debug_rows), use_container_width=True, hide_index=True)

    with st.expander("Live logs", expanded=True):
        st.text("\n".join(st.session_state.logs[-400:]))


def main() -> None:
    ensure_session_state()
    INSTANCE_CONFIG.ensure_directories()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    startup_check = write_startup_check()

    with st.sidebar:
        st.header("Database")
        backend_label = st.selectbox(
            "Database backend",
            ["SQLite local database recommended", "PostgreSQL advanced optional"],
            index=0,
        )
        db_backend = "postgres" if backend_label.startswith("PostgreSQL") else "sqlite"

    initialize_database(db_backend)
    setup_diagnostics = build_setup_diagnostics() if db_backend == "postgres" else None
    drain_job_queue()

    st.title("Hotel Price Collector")
    render_instance_info()
    render_instance_error_panel()
    with st.expander("Startup self check", expanded=False):
        st.json(startup_check)
    if st.session_state.last_preflight:
        with st.expander("Last start preflight", expanded=st.session_state.last_preflight.get("status") != "passed"):
            st.json(st.session_state.last_preflight)

    if db_backend == "sqlite" and st.session_state.db_ready:
        st.success("SQLite database ready.")
        st.write(f"Database file: {display_path(SQLITE_DB_PATH)}")
    elif db_backend == "postgres" and st.session_state.db_ready:
        st.success("PostgreSQL connection ready.")
    else:
        if db_backend == "postgres":
            st.warning("PostgreSQL is optional. Switch to SQLite to run immediately.")
        else:
            st.error("SQLite database initialization failed.")
        with st.expander("Database error", expanded=True):
            st.caption("Project folder")
            st.code(str(BASE_DIR.resolve()), language="text")
            if db_backend == "postgres":
                st.caption("Expected .env file")
                st.code(str(ENV_PATH.resolve()), language="text")
            else:
                st.caption("Expected SQLite database file")
                st.code(str(SQLITE_DB_PATH.resolve()), language="text")
            st.code(st.session_state.db_error or "No error captured.")
    if db_backend == "postgres" and setup_diagnostics is not None:
        render_setup_diagnostics(setup_diagnostics)

    job_active = bool(st.session_state.current_job)

    with st.sidebar:
        if db_backend == "sqlite":
            st.caption(f"SQLite file: {display_path(SQLITE_DB_PATH)}")
            if st.button("Reset instance running state only", use_container_width=True, disabled=job_active):
                reset_period_1_running_state()
                st.success("Instance running state reset. Scraped data was not deleted.")
                st.rerun()
            confirm_reset_sqlite = st.checkbox("I understand reset deletes local stored runs")
            if st.button("Reset SQLite database", use_container_width=True, disabled=job_active or not confirm_reset_sqlite):
                reset_sqlite_database()
                reset_current_results()
                st.success("SQLite database reset.")
                st.rerun()
        else:
            st.caption("PostgreSQL is optional. Switch to SQLite to run immediately.")

        st.header("Search")
        source_options = ["Demo", "Booking.com", "Expedia"]
        source_index = source_options.index(INSTANCE_CONFIG.source) if INSTANCE_CONFIG.source in source_options else 1
        source = st.selectbox("Source", source_options, index=source_index)
        city_or_region = st.text_input("City or region", value=INSTANCE_CONFIG.city, placeholder="Orlando, Miami Beach, San Felipe")
        default_start = INSTANCE_CONFIG.start_date or date.today() + timedelta(days=30)
        default_end = INSTANCE_CONFIG.end_date or default_start
        checkin_start = st.date_input("Start check in date", value=default_start)
        checkin_end = st.date_input("End check in date", value=default_end)
        if checkin_end < checkin_start:
            st.warning("End date is before start date. The run will use only the start date.")

        stay_choice = st.selectbox("Length of stay", ["1 night", "2 nights", "3 nights", "4 nights", "5 nights", "7 nights", "Custom number of nights"])
        if stay_choice == "Custom number of nights":
            nights = int(st.number_input("Custom nights", min_value=1, max_value=60, value=3, step=1))
        else:
            nights = int(stay_choice.split()[0])

        adults = int(st.number_input("Adults", min_value=1, max_value=12, value=2, step=1))
        currency = st.selectbox("Currency", ["EUR", "USD", "GBP", "MXN"], index=1)
        collect_all_available = st.checkbox("Collect all available hotels for this date", value=False)
        disable_filters_complete = False
        ultra_reliable_loading_mode = False
        if is_booking_source(source) and collect_all_available:
            disable_filters_complete = st.checkbox("Disable filters during complete collection test", value=True)
            ultra_reliable_loading_mode = st.checkbox("Ultra reliable loading mode", value=True)

        max_choice = st.selectbox("Maximum hotels", ["10", "25", "50", "100", "Custom number"], index=1, disabled=collect_all_available)
        if max_choice == "Custom number":
            max_hotels = int(st.number_input("Custom maximum hotels", min_value=1, max_value=500, value=25, step=1, disabled=collect_all_available))
        else:
            max_hotels = int(max_choice)

        if is_booking_source(source) and collect_all_available:
            max_scroll_minutes = int(st.selectbox("Full collection maximum time", [10, 15, 25, 40], index=2))
        else:
            max_scroll_minutes = int(st.selectbox("Maximum scroll time in minutes", [2, 5, 10, 15, 25], index=1))
        star_ratings = st.multiselect("Star ratings", [1, 2, 3, 4, 5], default=[1, 2, 3, 4, 5])
        if not star_ratings:
            st.warning("No star ratings selected. The run will use all star ratings.")
            star_ratings = [1, 2, 3, 4, 5]
        include_unknown = st.checkbox("Include hotels with unknown star rating", value=True)
        hotels_only = True
        if is_booking_source(source):
            hotels_only = st.checkbox("Hotels only, exclude vacation rentals", value=True)
        performance_label = st.selectbox(
            "Performance mode",
            ["Debug mode slow but detailed", "Balanced mode recommended"],
            index=1,
        )
        performance_mode = "debug" if performance_label.startswith("Debug") else "balanced"
        browser_mode = st.radio(
            "Browser mode",
            ["Visible debug mode", "Headless reliable mode"],
            index=1 if performance_mode == "balanced" else 0,
        )
        headless = browser_mode == "Headless reliable mode"
        debug_mode = performance_mode == "debug"
        fast_mode = False
        screenshots_enabled = debug_mode
        block_images_and_fonts = False
        if performance_mode == "debug":
            st.caption("Debug mode uses detailed logs, raw card HTML, and screenshots at major steps.")
        elif performance_mode == "balanced":
            st.caption("Balanced mode keeps safe waits and reduces screenshots/debug files.")
        else:
            st.caption("Fast mode is temporarily ignored while Booking.com complete collection is stabilized.")
        if is_booking_source(source) and headless:
            st.info("Headless reliable mode uses the same Booking.com scraper logic as visible mode, launched with headless=True.")
        smoke_clicked = st.button("Run headless smoke test", use_container_width=True, disabled=job_active)
        test_mode = st.checkbox("Test mode", value=False)
        auto_refresh = st.checkbox("Auto refresh while running", value=True)
        if db_backend == "sqlite":
            st.info("SQLite mode can run Demo, Booking.com, and Expedia without PostgreSQL setup.")

        planned_dates = date_range(checkin_start, checkin_end)
        st.header("Batch parallel collection")
        st.info("This 8 GB tower is limited to one scraper worker and one scraper browser at a time.")
        batch_label = st.selectbox(
            "Batch collection mode",
            ["Single run"],
            index=0,
            disabled=True,
        )
        batch_mode = {
            "Single run": "single",
            "Split by week": "weekly",
            "Split by month": "monthly",
            "Custom date blocks": "custom",
        }[batch_label]
        custom_block_days = 1
        if batch_mode == "custom":
            custom_block_days = int(st.number_input("Days per custom block", min_value=1, max_value=31, value=1, step=1))
        worker_label = st.selectbox(
            "Maximum parallel workers",
            ["1 internal workers disabled"],
            index=0,
            disabled=True,
        )
        max_parallel_workers = 1
        st.warning("Parallel scraper workers are disabled by the host-wide safety lock.")
        resume_previous_run = st.checkbox("Resume incomplete batch", value=True)
        st.header("Reliability")
        retry_failed_dates_automatically = st.checkbox("Retry failed dates automatically", value=True)
        max_retries_per_date = int(st.selectbox("Max retries per date", [1, 2, 3, 5], index=2))
        continue_if_date_fails = st.checkbox("Continue if one date fails", value=True)
        auto_export_partial_excel = st.checkbox("Auto export periodic partial Excel", value=False)
        partial_frequency_label = st.selectbox(
            "Partial export frequency",
            ["Every 25 dates", "Every 30 minutes", "Every 5 dates"],
            index=0,
            disabled=not auto_export_partial_excel,
        )
        partial_export_frequency = {
            "Every 25 dates": "every_25_dates",
            "Every 5 dates": "every_5_dates",
            "Every 30 minutes": "every_30_minutes",
        }[partial_frequency_label]
        rerun_scope_label = st.selectbox(
            "Batch resume/rerun scope",
            ["Skip completed, run failed and incomplete blocks", "Run only selected block IDs", "Run all blocks again"],
            index=0,
            disabled=batch_mode == "single",
        )
        rerun_batch_scope = {
            "Skip completed, run failed and incomplete blocks": "resume_incomplete",
            "Run only selected block IDs": "selected",
            "Run all blocks again": "all",
        }[rerun_scope_label]
        selected_block_text = ""
        if rerun_batch_scope == "selected" and batch_mode != "single":
            selected_block_text = st.text_input("Selected block IDs", value="", placeholder="1,3,5-7")
        selected_batch_blocks = parse_selected_block_ids(selected_block_text)
        preview_blocks = build_batch_blocks(checkin_start, checkin_end, batch_mode, custom_block_days)
        estimated_total_stay_dates = sum(len(block.stay_dates) for block in preview_blocks)
        st.subheader("Batch preview")
        st.write(f"Total date range: {preview_blocks[0].start_date.isoformat()} to {preview_blocks[-1].end_date.isoformat()}")
        st.write(f"Split mode: {batch_label}")
        st.write(f"{len(preview_blocks)} block(s)")
        st.write(f"{estimated_total_stay_dates} total stay date(s)")
        st.write(f"First block: {preview_blocks[0].start_date.isoformat()} to {preview_blocks[0].end_date.isoformat()}")
        st.write(f"Last block: {preview_blocks[-1].start_date.isoformat()} to {preview_blocks[-1].end_date.isoformat()}")
        preview_rows = [
            {
                "Block": block.block_id,
                "Start": block.start_date.isoformat(),
                "End": block.end_date.isoformat(),
                "Stay dates": len(block.stay_dates),
            }
            for block in preview_blocks[: min(12, len(preview_blocks))]
        ]
        st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
        if len(preview_blocks) > 12:
            st.caption(f"Showing first 12 of {len(preview_blocks)} blocks.")
        if test_mode:
            planned_dates = planned_dates[:1]
            collect_all_available = False
            max_hotels = 25
            headless = False
            debug_mode = True
            screenshots_enabled = True
            performance_mode = "debug"
            block_images_and_fonts = False

        st.subheader("Planned run")
        st.write(f"{len(planned_dates)} stay date(s)")
        st.write(f"{nights} night(s) each")
        st.write(f"Source: {source}")
        st.write(f"City: {city_or_region}")
        st.write(f"Performance mode: {performance_label}")
        st.write(f"Browser mode: {browser_mode}")
        st.write(f"Star ratings selected: {', '.join(map(str, star_ratings))}")
        st.write(f"Collect all available hotels: {'enabled' if collect_all_available else 'disabled'}")
        if is_booking_source(source) and collect_all_available:
            st.write(f"Disable filters during complete collection test: {'enabled' if disable_filters_complete else 'disabled'}")
            st.write(f"Ultra reliable loading mode: {'enabled' if ultra_reliable_loading_mode else 'disabled'}")
            st.write(f"Full collection maximum time: {max_scroll_minutes} minutes")
        st.write(f"Resume previous incomplete run: {'enabled' if resume_previous_run else 'disabled'}")
        st.write(f"Retry failed dates automatically: {'enabled' if retry_failed_dates_automatically else 'disabled'}")
        st.write(f"Max retries per date: {max_retries_per_date}")
        st.write(f"Continue if one date fails: {'enabled' if continue_if_date_fails else 'disabled'}")
        st.write(f"Auto export partial Excel: {'enabled' if auto_export_partial_excel else 'disabled'}")
        st.write(f"Batch mode: {batch_label}")
        st.write(f"Maximum parallel workers: {max_parallel_workers}")

        preview_config = CollectionConfig(
            source=source,
            city_or_region=city_or_region,
            checkin_start=planned_dates[0],
            checkin_end=planned_dates[-1],
            nights=nights,
            adults=adults,
            currency=currency,
            max_hotels=max_hotels,
            headless=headless,
            collect_all_available=collect_all_available,
            max_scroll_minutes=max_scroll_minutes,
            selected_star_ratings=tuple(int(value) for value in star_ratings),
            include_unknown_star_rating=include_unknown,
            debug_mode=debug_mode,
            screenshots_enabled=screenshots_enabled,
            fast_mode=fast_mode,
            performance_mode=performance_mode,
            block_images_and_fonts=block_images_and_fonts,
            test_mode=test_mode,
            db_backend=db_backend,
            hotels_only=hotels_only,
            disable_filters_during_complete_collection=disable_filters_complete,
            ultra_reliable_loading_mode=ultra_reliable_loading_mode,
            resume_previous_run=resume_previous_run,
            batch_mode=batch_mode,
            custom_block_days=custom_block_days,
            max_parallel_workers=max_parallel_workers,
            rerun_batch_scope=rerun_batch_scope,
            selected_batch_blocks=selected_batch_blocks,
            retry_failed_dates_automatically=retry_failed_dates_automatically,
            max_retries_per_date=max_retries_per_date,
            continue_if_date_fails=continue_if_date_fails,
            auto_export_partial_excel=auto_export_partial_excel,
            partial_export_frequency=partial_export_frequency,
        )
        if st.session_state.db_ready:
            st.session_state.resume_preview = build_resume_preview(preview_config)
            st.subheader("Resume preview")
            for key, value in st.session_state.resume_preview.items():
                st.write(f"{key}: {value}")

        if len(planned_dates) > 1 and collect_all_available and is_booking_source(source):
            st.warning("Full Booking.com collection will run every selected stay date. Progress is saved after each date so you can resume if it stops.")
        elif len(planned_dates) > 7 and collect_all_available and source in {"Booking.com", "Expedia"}:
            st.warning("This may take a long time. Progress is saved after each date.")
        if max_parallel_workers > 2 or collect_all_available or len(planned_dates) > 30:
            st.warning("This can be very slow and may use a lot of memory. Start with 1 or 2 workers.")

        required_inputs_present = bool(city_or_region.strip()) and bool(planned_dates)
        start_allowed = st.session_state.db_ready and required_inputs_present
        if not start_allowed:
            if db_backend == "postgres" and source in {"Booking.com", "Expedia"}:
                st.warning("Booking.com and Expedia require PostgreSQL because real scrape results must be stored before export.")
            elif not required_inputs_present:
                st.warning("Enter the required search inputs before starting.")
            else:
                st.warning("Start collection is disabled because database initialization failed.")
        start_clicked = st.button("Start collection", type="primary", use_container_width=True, disabled=not start_allowed or job_active)
        test_run_clicked = st.button("Start test run", use_container_width=True, disabled=not st.session_state.db_ready or job_active)
        stop_clicked = st.button("Stop collection", use_container_width=True, disabled=not job_active)
        refresh_clicked = st.button("Manual refresh", use_container_width=True)
        export_clicked = st.button("Export latest results to Excel", use_container_width=True, disabled=job_active)
        merge_clicked = st.button("Merge instance exports", use_container_width=True, disabled=job_active)
        clear_clicked = st.button("Clear current dashboard results", use_container_width=True, disabled=job_active)

    if stop_clicked and st.session_state.current_job:
        st.session_state.current_job["stop_event"].set("user")
        st.session_state.current_job["stop_requested_at"] = time.monotonic()
        st.session_state.status_message = "Stop requested. Saving the current checkpoint and closing the scraper browser."
        st.session_state.status = "stopping"
        update_status_file(
            status="stopping",
            current_message=st.session_state.status_message,
            stop_requested_at=datetime.now().isoformat(sep=" ", timespec="seconds"),
        )

    if refresh_clicked:
        st.rerun()

    if smoke_clicked:
        payload = run_headless_smoke_test()
        if payload.get("status") == "completed":
            st.success(f"Headless smoke test passed. Title: {payload.get('page_title')}")
            st.write(f"Screenshot: {payload.get('screenshot_path')}")
        else:
            st.error("Headless smoke test failed.")
            st.code(str(payload.get("error_message") or "Unknown error"), language="text")

    if clear_clicked:
        reset_current_results()
        st.rerun()

    if test_run_clicked:
        test_config = CollectionConfig(
            source="Booking.com",
            city_or_region="Orlando",
            checkin_start=planned_dates[0],
            checkin_end=planned_dates[0],
            nights=1,
            adults=2,
            currency="USD",
            max_hotels=10,
            headless=False,
            collect_all_available=False,
            max_scroll_minutes=1,
            selected_star_ratings=(1, 2, 3, 4, 5),
            include_unknown_star_rating=True,
            debug_mode=True,
            screenshots_enabled=True,
            fast_mode=False,
            performance_mode="debug",
            block_images_and_fonts=False,
            test_mode=True,
            db_backend=db_backend,
            hotels_only=True,
            disable_filters_during_complete_collection=False,
            ultra_reliable_loading_mode=False,
            resume_previous_run=True,
            batch_mode="single",
            custom_block_days=1,
            max_parallel_workers=1,
            rerun_batch_scope="resume_incomplete",
            selected_batch_blocks=(),
            retry_failed_dates_automatically=True,
            max_retries_per_date=1,
            continue_if_date_fails=True,
            auto_export_partial_excel=False,
            partial_export_frequency="every_25_dates",
        )
        preflight = run_start_preflight(test_config)
        st.session_state.last_preflight = preflight
        if preflight["status"] == "passed":
            st.session_state.resume_preview = build_resume_preview(test_config)
            start_background_job(test_config)
            st.rerun()
        else:
            st.error(f"Test run did not start. {preflight['first_failed_check']}")

    if start_clicked:
        effective_config = preview_config
        preflight = run_start_preflight(effective_config)
        st.session_state.last_preflight = preflight
        if preflight["status"] == "passed":
            st.session_state.resume_preview = build_resume_preview(effective_config)
            start_background_job(effective_config)
            st.rerun()
        else:
            st.error(f"Collection did not start. {preflight['first_failed_check']}")

    if export_clicked:
        results = st.session_state.results
        if not results and st.session_state.db_ready:
            try:
                results = fetch_latest_results(backend=db_backend)
            except Exception as exc:
                st.warning(f"Could not load latest database results: {exc}")
                results = []
        if results:
            run_metadata = {
                "source": results[0].get("source"),
                "city_or_region": results[0].get("city_or_region"),
                "checkin_date": results[0].get("checkin_date"),
                "checkout_date": results[0].get("checkout_date"),
                "number_of_nights": results[0].get("number_of_nights"),
                "adults": results[0].get("adults"),
                "currency": results[0].get("currency"),
                "started_at": None,
                "completed_at": datetime.now(),
            }
            summary = create_summary(results, run_metadata)
            excel_path = export_results_to_excel(results, summary, EXPORT_DIR)
            st.session_state.excel_file_path = str(excel_path)
            st.session_state.summary = summary
            st.session_state.results = results
            st.success(f"Exported latest results to {excel_path}")
        else:
            st.warning("No results are available to export.")

    if merge_clicked:
        try:
            merged_path = merge_instance_exports()
            st.session_state.excel_file_path = str(merged_path)
            st.success(f"Merged instance exports to {merged_path}")
        except Exception as exc:
            st.error(f"Could not merge instance exports: {exc}")

    render_job_status()

    st.subheader("Instance files")
    latest_log = st.session_state.active_log_file_path or read_current_status_file().get("log_file")
    st.write(f"Latest log file path: {latest_log or 'No active run log yet'}")
    st.write(f"Open log folder path: {LOG_DIR.resolve()}")
    st.write(f"Latest debug folder path: {DEBUG_DIR.resolve()}")
    st.write(f"Latest Excel export path: {st.session_state.excel_file_path or read_current_status_file().get('latest_excel_file') or 'No export yet'}")
    st.write(f"Latest CSV export path: {st.session_state.csv_file_path or read_current_status_file().get('csv_file_path') or 'No CSV export yet'}")
    if st.button("Copy diagnostic summary", use_container_width=True):
        st.text_area("Diagnostic summary", value=build_diagnostic_summary(), height=220)

    if st.session_state.run_id:
        st.info(f"Database collection run ID: {st.session_state.run_id}")
        st.write(f"Records saved: {st.session_state.records_saved}")
    if st.session_state.excel_file_path:
        st.write(f"Excel file: {st.session_state.excel_file_path}")
        latest_export_download_button(st.session_state.excel_file_path)

    st.subheader("Summary")
    render_summary(st.session_state.summary)

    st.subheader("Results")
    display_df = as_display_df(st.session_state.results)
    if not display_df.empty and {"raw_star_signal", "star_rating"}.issubset(display_df.columns):
        with st.expander("Booking.com star extraction debug", expanded=False):
            debug_columns = [
                column
                for column in ["hotel_name", "raw_star_signal", "star_aria_label", "star_icon_count", "star_rating", "review_score", "cheapest_price_total", "star_rating_missing_reason"]
                if column in display_df.columns
            ]
            st.dataframe(display_df[debug_columns].head(50), use_container_width=True, hide_index=True)
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    with st.expander("Recent collection runs"):
        if st.session_state.db_ready:
            try:
                st.dataframe(pd.DataFrame(fetch_collection_runs(backend=db_backend)), use_container_width=True, hide_index=True)
            except Exception as exc:
                st.warning(f"Could not load recent collection runs: {exc}")

    if st.session_state.current_job and auto_refresh:
        time.sleep(3)
        st.rerun()


if __name__ == "__main__":
    main()
