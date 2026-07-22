from __future__ import annotations

import os
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import psycopg
from dotenv import load_dotenv

from services.normalizer import RESULT_FIELDS
from services.instance_config import INSTANCE_CONFIG


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
SQLITE_DB_PATH = DATA_DIR / "hotel_price_collector.db"
SQLITE_DB_PATH = INSTANCE_CONFIG.sqlite_path
ENV_PATH = BASE_DIR / ".env"
SCHEMA_PATH = BASE_DIR / "database" / "schema.sql"

SQLITE_COLLECTION_RUN_FIELDS = [
    "source",
    "city_or_region",
    "checkin_date",
    "checkout_date",
    "number_of_nights",
    "adults",
    "currency",
    "max_hotels",
    "started_at",
    "status",
    "selected_star_ratings",
    "include_unknown_star_rating",
    "job_signature",
    "job_signature_hash",
]


def normalize_backend(backend: str | None = None) -> str:
    value = (backend or os.getenv("DB_BACKEND") or "sqlite").strip().lower()
    if value.startswith("postgres"):
        return "postgres"
    return "sqlite"


def init_db(backend: str | None = None) -> None:
    if normalize_backend(backend) == "postgres":
        _init_postgres()
    else:
        _init_sqlite()


def create_collection_run(
    source: str,
    city_or_region: str,
    checkin_date: date,
    checkout_date: date,
    number_of_nights: int,
    adults: int,
    currency: str,
    max_hotels: int,
    status: str = "running",
    selected_star_ratings: str | None = None,
    include_unknown_star_rating: bool = True,
    job_signature: str | None = None,
    job_signature_hash: str | None = None,
    backend: str | None = None,
) -> int:
    if normalize_backend(backend) == "postgres":
        return _create_collection_run_postgres(
            source,
            city_or_region,
            checkin_date,
            checkout_date,
            number_of_nights,
            adults,
            currency,
            max_hotels,
            status,
            selected_star_ratings,
            include_unknown_star_rating,
            job_signature, job_signature_hash,
        )
    return _create_collection_run_sqlite(
        source,
        city_or_region,
        checkin_date,
        checkout_date,
        number_of_nights,
        adults,
        currency,
        max_hotels,
        status,
        selected_star_ratings,
        include_unknown_star_rating,
        job_signature, job_signature_hash,
    )


def insert_hotel_results(results: list[dict[str, Any]], backend: str | None = None) -> int:
    if not results:
        return 0
    if normalize_backend(backend) == "postgres":
        return _insert_hotel_results_postgres(results)
    return _insert_hotel_results_sqlite(results)


def update_collection_run_status(run_id: int, status: str, error_message: str | None = None, backend: str | None = None) -> None:
    if normalize_backend(backend) == "postgres":
        _update_collection_run_status_postgres(run_id, status, error_message)
    else:
        _update_collection_run_status_sqlite(run_id, status, error_message)


def update_collection_run_excel_path(run_id: int, excel_file_path: str, backend: str | None = None) -> None:
    if normalize_backend(backend) == "postgres":
        _update_collection_run_excel_path_postgres(run_id, excel_file_path)
    else:
        _update_collection_run_excel_path_sqlite(run_id, excel_file_path)


def fetch_latest_results(limit: int = 500, backend: str | None = None) -> list[dict[str, Any]]:
    if normalize_backend(backend) == "postgres":
        return _fetch_latest_results_postgres(limit)
    return _fetch_latest_results_sqlite(limit)


def fetch_results_by_run_id(run_id: int, backend: str | None = None) -> list[dict[str, Any]]:
    if normalize_backend(backend) == "postgres":
        return _fetch_results_by_run_id_postgres(run_id)
    return _fetch_results_by_run_id_sqlite(run_id)


def fetch_results_by_run_id_limited(run_id: int, limit: int = 500, backend: str | None = None) -> list[dict[str, Any]]:
    if normalize_backend(backend) == "postgres":
        rows = _fetch_results_by_run_id_postgres(run_id)
        return rows[-max(1, int(limit)) :]
    with _sqlite_connection() as conn:
        rows = conn.execute(
            """
            select * from hotel_price_results
            where collection_run_id = ?
            order by id desc
            limit ?
            """,
            (run_id, max(1, int(limit))),
        ).fetchall()
        return list(reversed(_sqlite_rows_to_dicts(rows)))


def count_results_by_run_id(run_id: int, backend: str | None = None) -> int:
    if normalize_backend(backend) == "postgres":
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from hotel_price_results where collection_run_id = %s", (run_id,))
                return int(cur.fetchone()[0])
    with _sqlite_connection() as conn:
        return int(conn.execute("select count(*) from hotel_price_results where collection_run_id = ?", (run_id,)).fetchone()[0])


def result_date_counts_by_run_id(run_id: int, backend: str | None = None) -> dict[str, int]:
    if normalize_backend(backend) == "postgres":
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select checkin_date, count(*) from hotel_price_results where collection_run_id = %s group by checkin_date",
                    (run_id,),
                )
                return {str(row[0]): int(row[1]) for row in cur.fetchall() if row[0] is not None}
    with _sqlite_connection() as conn:
        rows = conn.execute(
            "select checkin_date, count(*) from hotel_price_results where collection_run_id = ? group by checkin_date",
            (run_id,),
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows if row[0] is not None}


def iter_sqlite_results_by_run_id(run_id: int, batch_size: int = 500) -> Iterator[list[dict[str, Any]]]:
    """Yield bounded SQLite batches without retaining the complete run."""

    conn = _sqlite_connection()
    try:
        cursor = conn.execute(
            """
            select * from hotel_price_results
            where collection_run_id = ?
            order by checkin_date, ranking_position_on_page, id
            """,
            (run_id,),
        )
        while True:
            rows = cursor.fetchmany(max(1, int(batch_size)))
            if not rows:
                break
            yield _sqlite_rows_to_dicts(rows)
    finally:
        conn.close()


def sqlite_integrity_check() -> str:
    with _sqlite_connection() as conn:
        return str(conn.execute("pragma integrity_check").fetchone()[0])


def sqlite_wal_checkpoint(mode: str = "PASSIVE") -> tuple[int, int, int]:
    safe_mode = mode.upper() if mode.upper() in {"PASSIVE", "FULL", "RESTART"} else "PASSIVE"
    with _sqlite_connection() as conn:
        row = conn.execute(f"pragma wal_checkpoint({safe_mode})").fetchone()
        return int(row[0]), int(row[1]), int(row[2])


def fetch_collection_runs(limit: int = 20, backend: str | None = None) -> list[dict[str, Any]]:
    if normalize_backend(backend) == "postgres":
        return _fetch_collection_runs_postgres(limit)
    return _fetch_collection_runs_sqlite(limit)


def reset_sqlite_database() -> None:
    if SQLITE_DB_PATH.exists():
        SQLITE_DB_PATH.unlink()
    _init_sqlite()


def get_connection(database_name: str | None = None) -> psycopg.Connection:
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=database_name or os.getenv("POSTGRES_DB", "hotel_price_collector"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        connect_timeout=5,
    )


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma synchronous=NORMAL")
    conn.execute("pragma busy_timeout=30000")
    return conn


def _init_sqlite() -> None:
    with _sqlite_connection() as conn:
        conn.executescript(
            """
            create table if not exists collection_runs (
                id integer primary key autoincrement,
                source text,
                city_or_region text,
                checkin_date text,
                checkout_date text,
                number_of_nights integer,
                adults integer,
                currency text,
                max_hotels integer,
                started_at text,
                completed_at text,
                status text,
                error_message text,
                excel_file_path text,
                selected_star_ratings text,
                include_unknown_star_rating integer
                ,job_signature text
                ,job_signature_hash text
            );

            create table if not exists hotel_price_results (
                id integer primary key autoincrement,
                collection_run_id integer,
                source text,
                city_or_region text,
                search_url text,
                hotel_name text,
                raw_hotel_name text,
                property_type_guess text,
                excluded_by_hotels_only_filter integer,
                ota_hotel_id text,
                star_rating real,
                raw_star_signal text,
                star_aria_label text,
                star_icon_count integer,
                star_rating_missing_reason text,
                review_score real,
                review_count integer,
                room_name text,
                cheapest_room_name text,
                raw_price_text text,
                parsed_price real,
                cheapest_price_total real,
                currency text,
                taxes_and_fees_text text,
                checkin_date text,
                checkout_date text,
                number_of_nights integer,
                adults integer,
                hotel_url text,
                provider_name text,
                raw_source_payload text,
                ranking_position_on_page integer,
                screenshot_path text,
                collection_status text,
                error_message text,
                collected_at text
            );

            create index if not exists idx_sqlite_results_collection_run_id
                on hotel_price_results(collection_run_id);
            create index if not exists idx_sqlite_results_source
                on hotel_price_results(source);
            create index if not exists idx_sqlite_results_city_or_region
                on hotel_price_results(city_or_region);
            create index if not exists idx_sqlite_results_checkin_date
                on hotel_price_results(checkin_date);
            create index if not exists idx_sqlite_results_collected_at
                on hotel_price_results(collected_at);
            create index if not exists idx_sqlite_results_hotel_name
                on hotel_price_results(hotel_name);
            """
        )
        _ensure_sqlite_columns(conn)
        conn.commit()


def _ensure_sqlite_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("pragma table_info(collection_runs)").fetchall()}
    additions = {
        "selected_star_ratings": "text",
        "include_unknown_star_rating": "integer",
        "job_signature": "text",
        "job_signature_hash": "text",
    }
    for column, column_type in additions.items():
        if column not in existing:
            conn.execute(f"alter table collection_runs add column {column} {column_type}")
    result_existing = {row["name"] for row in conn.execute("pragma table_info(hotel_price_results)").fetchall()}
    result_additions = {
        "raw_hotel_name": "text",
        "property_type_guess": "text",
        "excluded_by_hotels_only_filter": "integer",
        "raw_star_signal": "text",
        "star_aria_label": "text",
        "star_icon_count": "integer",
        "star_rating_missing_reason": "text",
        "room_name": "text",
        "raw_price_text": "text",
        "parsed_price": "real",
        "provider_name": "text",
        "raw_source_payload": "text",
    }
    for column, column_type in result_additions.items():
        if column not in result_existing:
            conn.execute(f"alter table hotel_price_results add column {column} {column_type}")


def _init_postgres() -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()


def _terminal_completed_at(status: str) -> datetime | None:
    terminal_statuses = {
        "completed",
        "completed_all_dates",
        "completed_with_failed_dates",
        "completed_with_blocked_dates",
        "stopped_by_user_with_partial_results",
        "fatal_error_with_partial_results",
        "fatal_config_error",
        "completed_with_partial_results",
        "completed_with_zero_results",
        "failed",
        "blocked_or_access_restricted",
        "stopped_by_user",
        "stopped_resource_limit",
        "browser_crash",
        "application_exception",
    }
    return datetime.now() if status in terminal_statuses else None


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, default=str)
    return value


def _create_collection_run_sqlite(
    source: str,
    city_or_region: str,
    checkin_date: date,
    checkout_date: date,
    number_of_nights: int,
    adults: int,
    currency: str,
    max_hotels: int,
    status: str,
    selected_star_ratings: str | None,
    include_unknown_star_rating: bool,
    job_signature: str | None = None,
    job_signature_hash: str | None = None,
) -> int:
    values = {
        "source": source,
        "city_or_region": city_or_region,
        "checkin_date": checkin_date,
        "checkout_date": checkout_date,
        "number_of_nights": number_of_nights,
        "adults": adults,
        "currency": currency,
        "max_hotels": max_hotels,
        "started_at": datetime.now(),
        "status": status,
        "selected_star_ratings": selected_star_ratings,
        "include_unknown_star_rating": include_unknown_star_rating,
        "job_signature": job_signature,
        "job_signature_hash": job_signature_hash,
    }
    columns = ", ".join(SQLITE_COLLECTION_RUN_FIELDS)
    placeholders = ", ".join(["?"] * len(SQLITE_COLLECTION_RUN_FIELDS))
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            f"insert into collection_runs ({columns}) values ({placeholders})",
            tuple(_serialize(values[field]) for field in SQLITE_COLLECTION_RUN_FIELDS),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _create_collection_run_postgres(
    source: str,
    city_or_region: str,
    checkin_date: date,
    checkout_date: date,
    number_of_nights: int,
    adults: int,
    currency: str,
    max_hotels: int,
    status: str,
    selected_star_ratings: str | None,
    include_unknown_star_rating: bool,
    job_signature: str | None = None,
    job_signature_hash: str | None = None,
) -> int:
    sql = """
        insert into collection_runs (
            source, city_or_region, checkin_date, checkout_date, number_of_nights,
            adults, currency, max_hotels, status, selected_star_ratings, include_unknown_star_rating, job_signature, job_signature_hash
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    source,
                    city_or_region,
                    checkin_date,
                    checkout_date,
                    number_of_nights,
                    adults,
                    currency,
                    max_hotels,
                    status,
                    selected_star_ratings,
                    include_unknown_star_rating,
                    job_signature,
                    job_signature_hash,
                ),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
    return int(run_id)


def _insert_hotel_results_sqlite(results: list[dict[str, Any]]) -> int:
    _init_sqlite()
    fields = RESULT_FIELDS
    placeholders = ", ".join(["?"] * len(fields))
    columns = ", ".join(fields)
    unique_rows = _dedupe_rows_for_insert(results)
    values = [tuple(_serialize(row.get(field)) for field in fields) for row in unique_rows]
    with _sqlite_connection() as conn:
        for row in unique_rows:
            _delete_matching_sqlite_result(conn, row)
        conn.executemany(f"insert into hotel_price_results ({columns}) values ({placeholders})", values)
        conn.commit()
    return len(unique_rows)


def _insert_hotel_results_postgres(results: list[dict[str, Any]]) -> int:
    fields = RESULT_FIELDS
    placeholders = ", ".join(["%s"] * len(fields))
    columns = ", ".join(fields)
    unique_rows = _dedupe_rows_for_insert(results)
    values = [tuple(row.get(field) for field in fields) for row in unique_rows]
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in unique_rows:
                _delete_matching_postgres_result(cur, row)
            cur.executemany(f"insert into hotel_price_results ({columns}) values ({placeholders})", values)
        conn.commit()
    return len(unique_rows)


def _dedupe_rows_for_insert(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in results:
        deduped[_result_dedupe_key(row)] = row
    return list(deduped.values())


def _result_dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    preferred_identity = row.get("hotel_url") or row.get("hotel_name")
    return (
        row.get("collection_run_id"),
        str(row.get("source") or "").strip().lower(),
        str(row.get("city_or_region") or "").strip().lower(),
        _serialize(row.get("checkin_date")),
        _serialize(row.get("checkout_date")),
        str(preferred_identity or "").strip().lower(),
    )


def _delete_matching_sqlite_result(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    identity = row.get("hotel_url") or row.get("hotel_name")
    if not identity:
        return
    identity_field = "hotel_url" if row.get("hotel_url") else "hotel_name"
    conn.execute(
        f"""
        delete from hotel_price_results
        where collection_run_id = ?
          and lower(coalesce(source, '')) = lower(?)
          and lower(coalesce(city_or_region, '')) = lower(?)
          and checkin_date = ?
          and checkout_date = ?
          and lower(coalesce({identity_field}, '')) = lower(?)
        """,
        (
            _serialize(row.get("collection_run_id")),
            row.get("source") or "",
            row.get("city_or_region") or "",
            _serialize(row.get("checkin_date")),
            _serialize(row.get("checkout_date")),
            identity,
        ),
    )


def _delete_matching_postgres_result(cur: psycopg.Cursor, row: dict[str, Any]) -> None:
    identity = row.get("hotel_url") or row.get("hotel_name")
    if not identity:
        return
    identity_field = "hotel_url" if row.get("hotel_url") else "hotel_name"
    cur.execute(
        f"""
        delete from hotel_price_results
        where collection_run_id = %s
          and lower(coalesce(source, '')) = lower(%s)
          and lower(coalesce(city_or_region, '')) = lower(%s)
          and checkin_date = %s
          and checkout_date = %s
          and lower(coalesce({identity_field}, '')) = lower(%s)
        """,
        (
            row.get("collection_run_id"),
            row.get("source") or "",
            row.get("city_or_region") or "",
            row.get("checkin_date"),
            row.get("checkout_date"),
            identity,
        ),
    )


def _update_collection_run_status_sqlite(run_id: int, status: str, error_message: str | None = None) -> None:
    completed_at = _terminal_completed_at(status)
    with _sqlite_connection() as conn:
        conn.execute(
            """
            update collection_runs
            set status = ?,
                error_message = ?,
                completed_at = coalesce(?, completed_at)
            where id = ?
            """,
            (status, error_message, _serialize(completed_at), run_id),
        )
        conn.commit()


def _update_collection_run_status_postgres(run_id: int, status: str, error_message: str | None = None) -> None:
    completed_at = _terminal_completed_at(status)
    sql = """
        update collection_runs
        set status = %s,
            error_message = %s,
            completed_at = coalesce(%s, completed_at)
        where id = %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, error_message, completed_at, run_id))
        conn.commit()


def _update_collection_run_excel_path_sqlite(run_id: int, excel_file_path: str) -> None:
    with _sqlite_connection() as conn:
        conn.execute("update collection_runs set excel_file_path = ? where id = ?", (excel_file_path, run_id))
        conn.commit()


def _update_collection_run_excel_path_postgres(run_id: int, excel_file_path: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("update collection_runs set excel_file_path = %s where id = %s", (excel_file_path, run_id))
        conn.commit()


def _sqlite_rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _postgres_rows_to_dicts(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    columns = [desc.name for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _fetch_latest_results_sqlite(limit: int) -> list[dict[str, Any]]:
    with _sqlite_connection() as conn:
        rows = conn.execute(
            """
            select r.*
            from hotel_price_results r
            where r.collection_run_id = (select max(id) from collection_runs)
            order by r.ranking_position_on_page, r.id
            limit ?
            """,
            (limit,),
        ).fetchall()
        return _sqlite_rows_to_dicts(rows)


def _fetch_latest_results_postgres(limit: int) -> list[dict[str, Any]]:
    sql = """
        select r.*
        from hotel_price_results r
        where r.collection_run_id = (select max(id) from collection_runs)
        order by r.ranking_position_on_page nulls last, r.id
        limit %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return _postgres_rows_to_dicts(cur)


def _fetch_results_by_run_id_sqlite(run_id: int) -> list[dict[str, Any]]:
    with _sqlite_connection() as conn:
        rows = conn.execute(
            """
            select *
            from hotel_price_results
            where collection_run_id = ?
            order by ranking_position_on_page, id
            """,
            (run_id,),
        ).fetchall()
        return _sqlite_rows_to_dicts(rows)


def _fetch_results_by_run_id_postgres(run_id: int) -> list[dict[str, Any]]:
    sql = """
        select *
        from hotel_price_results
        where collection_run_id = %s
        order by ranking_position_on_page nulls last, id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (run_id,))
            return _postgres_rows_to_dicts(cur)


def _fetch_collection_runs_sqlite(limit: int) -> list[dict[str, Any]]:
    with _sqlite_connection() as conn:
        rows = conn.execute(
            """
            select *
            from collection_runs
            order by started_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        return _sqlite_rows_to_dicts(rows)


def _fetch_collection_runs_postgres(limit: int) -> list[dict[str, Any]]:
    sql = """
        select *
        from collection_runs
        order by started_at desc
        limit %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return _postgres_rows_to_dicts(cur)
