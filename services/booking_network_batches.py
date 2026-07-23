from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from services.booking_pagination import canonical_hotel_url, hotel_identity


BOOKING_GRAPHQL_PATH = "/dml/graphql"
BOOKING_SEARCH_OPERATION = "FullSearch"
DEFAULT_BATCH_SIZE = 25
STATE_VERSION = 1


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _path(value: Any, *parts: str) -> Any:
    current = value
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _display_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "value", "label", "name", "amount"):
            text = _display_text(value.get(key))
            if text:
                return text
    return None


def _first_path(value: dict[str, Any], paths: Iterable[tuple[str, ...]]) -> Any:
    for parts in paths:
        candidate = _path(value, *parts)
        if candidate not in (None, "", [], {}):
            return candidate
    return None


def request_payload_is_full_search(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("operationName") != BOOKING_SEARCH_OPERATION:
        return False
    pagination = _path(payload, "variables", "input", "pagination")
    return (
        isinstance(pagination, dict)
        and "offset" in pagination
        and "rowsPerPage" in pagination
    )


def is_booking_full_search_request(request: Any) -> bool:
    try:
        parsed = urlparse(str(request.url))
        payload = request.post_data_json
        return (
            str(request.method).upper() == "POST"
            and parsed.scheme == "https"
            and (
                parsed.hostname == "booking.com"
                or str(parsed.hostname or "").endswith(".booking.com")
            )
            and parsed.path == BOOKING_GRAPHQL_PATH
            and request_payload_is_full_search(payload)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def pagination_from_request_payload(payload: dict[str, Any]) -> tuple[int, int]:
    if not request_payload_is_full_search(payload):
        raise ValueError("Not a Booking FullSearch pagination payload")
    pagination = payload["variables"]["input"]["pagination"]
    offset = max(0, int(pagination.get("offset") or 0))
    rows_per_page = max(1, int(pagination.get("rowsPerPage") or DEFAULT_BATCH_SIZE))
    return offset, rows_per_page


def request_payload_for_offset(
    template: dict[str, Any],
    offset: int,
) -> dict[str, Any]:
    payload = copy.deepcopy(template)
    if not request_payload_is_full_search(payload):
        raise ValueError("Not a Booking FullSearch pagination payload")
    payload["variables"]["input"]["pagination"]["offset"] = max(0, int(offset))
    return payload


def _search_payload(payload: Any) -> dict[str, Any]:
    search = _path(payload, "data", "searchQueries", "search")
    return search if isinstance(search, dict) else {}


def response_has_access_restriction(payload: Any) -> bool:
    try:
        compact = json.dumps(payload, ensure_ascii=True, default=str).lower()
    except (TypeError, ValueError):
        compact = str(payload).lower()
    signals = (
        "captcha",
        "verify you are human",
        "access denied",
        "unusual traffic",
        "access_restricted",
        "not authorized",
    )
    return any(signal in compact for signal in signals)


def _property_url(basic: dict[str, Any]) -> str | None:
    page_name = str(basic.get("pageName") or "").strip().strip("/")
    if page_name.startswith("hotel/"):
        return canonical_hotel_url(f"https://www.booking.com/{page_name}")
    if page_name.endswith(".html") and "/" in page_name:
        return canonical_hotel_url(f"https://www.booking.com/hotel/{page_name}")
    if not page_name:
        return None
    country = _first_path(
        basic,
        (
            ("location", "countryCode"),
            ("location", "country", "code"),
            ("countryCode",),
        ),
    )
    country_code = str(country or "").strip().lower()
    if not country_code:
        return None
    return canonical_hotel_url(
        f"https://www.booking.com/hotel/{country_code}/{page_name}.html"
    )


def _network_property(result: dict[str, Any]) -> dict[str, Any] | None:
    basic = _mapping(result.get("basicPropertyData"))
    name = _display_text(
        _first_path(
            result,
            (
                ("displayName",),
                ("generatedPropertyTitle",),
                ("propertyName",),
            ),
        )
    )
    property_id = basic.get("id") or result.get("id")
    hotel_url = _property_url(basic)
    if not name or not hotel_url:
        return None

    price_value = _first_path(
        result,
        (
            ("priceDisplayInfoIrene", "displayPrice", "amountPerStay"),
            ("priceDisplayInfoIrene", "displayPrice", "amountPerNight"),
            ("priceDisplayInfoIrene", "displayPrice"),
            ("blocks", "0", "finalPrice"),
        ),
    )
    price_text = _display_text(price_value)
    price_currency = None
    if isinstance(price_value, dict):
        price_currency = _display_text(
            price_value.get("currency")
            or price_value.get("currencyCode")
        )

    star_value = _display_text(_first_path(
        result,
        (
            ("basicPropertyData", "starRating", "value"),
            ("basicPropertyData", "starRating"),
            ("basicPropertyData", "propertyClass"),
            ("propertyClass",),
        ),
    ))
    review_score = _display_text(_first_path(
        result,
        (
            ("basicPropertyData", "reviewScore", "score"),
            ("basicPropertyData", "reviewScore"),
            ("reviewScore", "score"),
            ("reviewScore",),
        ),
    ))
    review_count = _display_text(_first_path(
        result,
        (
            ("basicPropertyData", "reviewScore", "reviewCount"),
            ("basicPropertyData", "reviewScore", "totalReviews"),
            ("basicPropertyData", "reviewCount"),
            ("reviewCount",),
        ),
    ))
    property_type = _display_text(
        _first_path(
            result,
            (
                ("basicPropertyData", "accommodationType", "name"),
                ("basicPropertyData", "accommodationType"),
                ("accommodationType", "name"),
                ("accommodationType",),
            ),
        )
    )
    return {
        "property_id": property_id,
        "page_name": basic.get("pageName"),
        "hotel_name": name,
        "hotel_url": hotel_url,
        "price_text": price_text,
        "price_currency": price_currency,
        "star_rating": star_value,
        "review_score": review_score,
        "review_count": review_count,
        "property_type": property_type,
    }


@dataclass(frozen=True, slots=True)
class BookingNetworkBatch:
    offset: int
    rows_per_page: int
    total_results: int | None
    properties: tuple[dict[str, Any], ...]
    parse_failures: int
    response_hash: str

    @property
    def result_count(self) -> int:
        return len(self.properties) + self.parse_failures

    @property
    def next_offset(self) -> int:
        return self.offset + self.rows_per_page

    @property
    def proves_exhaustion(self) -> bool:
        if self.result_count < self.rows_per_page:
            return True
        return (
            self.total_results is not None
            and self.next_offset >= self.total_results
        )


def parse_booking_network_batch(
    payload: Any,
    *,
    offset: int,
    rows_per_page: int,
) -> BookingNetworkBatch:
    if not isinstance(payload, dict):
        raise ValueError("Booking FullSearch response was not a JSON object")
    search = _search_payload(payload)
    raw_results = search.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Booking FullSearch response did not contain search results")
    properties: list[dict[str, Any]] = []
    parse_failures = 0
    for result in raw_results:
        if not isinstance(result, dict):
            parse_failures += 1
            continue
        parsed = _network_property(result)
        if parsed is None:
            parse_failures += 1
            continue
        properties.append(parsed)

    pagination = _mapping(search.get("pagination"))
    total_value = pagination.get("nbResultsTotal")
    try:
        total_results = max(0, int(total_value))
    except (TypeError, ValueError):
        total_results = None
    identities = sorted(
        filter(
            None,
            (
                canonical_hotel_url(row.get("hotel_url"))
                or str(row.get("property_id") or row.get("hotel_name") or "")
                for row in properties
            ),
        )
    )
    response_hash = hashlib.sha256(
        "\n".join(identities).encode("utf-8")
    ).hexdigest()
    return BookingNetworkBatch(
        offset=max(0, int(offset)),
        rows_per_page=max(1, int(rows_per_page)),
        total_results=total_results,
        properties=tuple(properties),
        parse_failures=parse_failures,
        response_hash=response_hash,
    )


def batch_is_repeated(
    batch: BookingNetworkBatch,
    *,
    previous_hashes: set[str],
    existing_rows: Iterable[dict[str, Any]],
) -> bool:
    if batch.response_hash in previous_hashes:
        return True
    existing = {hotel_identity(row) for row in existing_rows}
    keys = {
        hotel_identity(
            {
                "hotel_name": row.get("hotel_name"),
                "hotel_url": row.get("hotel_url"),
            }
        )
        for row in batch.properties
    }
    keys.discard("")
    return bool(keys) and keys <= existing


@dataclass(frozen=True, slots=True)
class BookingBatchContinuation:
    next_offset: int
    rows_per_page: int
    response_hashes: tuple[str, ...]
    unique_records: int
    completion_status: str
    updated_at: str
    version: int = STATE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def continuation_path(partial_dir: Path, stay_date: Any) -> Path:
    return Path(partial_dir) / f"{stay_date}_network_batch_state.json"


def load_continuation(
    partial_dir: Path | None,
    stay_date: Any,
) -> BookingBatchContinuation | None:
    if partial_dir is None:
        return None
    path = continuation_path(Path(partial_dir), stay_date)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("version") or 0) != STATE_VERSION:
            return None
        return BookingBatchContinuation(
            next_offset=max(0, int(payload["next_offset"])),
            rows_per_page=max(1, int(payload["rows_per_page"])),
            response_hashes=tuple(
                str(value) for value in payload.get("response_hashes") or ()
            ),
            unique_records=max(0, int(payload.get("unique_records") or 0)),
            completion_status=str(payload.get("completion_status") or "incomplete"),
            updated_at=str(payload.get("updated_at") or ""),
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def save_continuation(
    partial_dir: Path,
    stay_date: Any,
    *,
    next_offset: int,
    rows_per_page: int,
    response_hashes: Iterable[str],
    unique_records: int,
    completion_status: str,
) -> Path:
    path = continuation_path(Path(partial_dir), stay_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    hashes = tuple(dict.fromkeys(str(value) for value in response_hashes if value))
    state = BookingBatchContinuation(
        next_offset=max(0, int(next_offset)),
        rows_per_page=max(1, int(rows_per_page)),
        response_hashes=hashes[-256:],
        unique_records=max(0, int(unique_records)),
        completion_status=str(completion_status),
        updated_at=datetime.now().isoformat(),
    )
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state.as_dict(), handle, indent=2, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return path
