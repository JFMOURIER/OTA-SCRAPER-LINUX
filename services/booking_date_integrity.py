from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


DATE_INTEGRITY_VERSION = 1


def _date_text(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def query_date_values(url: str) -> dict[str, list[str]]:
    values = parse_qs(urlparse(str(url or "")).query, keep_blank_values=True)
    return {
        key: [candidate for raw in values.get(key, []) if (candidate := _date_text(raw))]
        for key in ("checkin", "checkout")
    }


def canonical_search_url(
    url: str,
    *,
    requested_checkin: date,
    requested_checkout: date,
) -> str:
    parsed = urlparse(str(url))
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["checkin"] = [requested_checkin.isoformat()]
    query["checkout"] = [requested_checkout.isoformat()]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


@dataclass(frozen=True, slots=True)
class DateIntegrityReport:
    requested_checkin_date: str
    requested_checkout_date: str
    effective_checkin_date: str | None
    effective_checkout_date: str | None
    date_integrity_verified: bool
    mismatch_reasons: tuple[str, ...]
    url_checkin_values: tuple[str, ...]
    url_checkout_values: tuple[str, ...]
    visible_checkin_values: tuple[str, ...]
    visible_checkout_values: tuple[str, ...]
    hidden_checkin_values: tuple[str, ...]
    hidden_checkout_values: tuple[str, ...]
    collector_checkin_date: str | None
    collector_checkout_date: str | None
    status_checkin_date: str | None
    status_checkout_date: str | None
    page_text_contains_requested_checkin: bool
    page_text_contains_requested_checkout: bool
    page_url: str
    version: int = DATE_INTEGRITY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_date_integrity(
    *,
    requested_checkin: date,
    requested_checkout: date,
    page_url: str,
    visible_checkin_values: list[Any] | tuple[Any, ...] = (),
    visible_checkout_values: list[Any] | tuple[Any, ...] = (),
    hidden_checkin_values: list[Any] | tuple[Any, ...] = (),
    hidden_checkout_values: list[Any] | tuple[Any, ...] = (),
    collector_checkin: Any = None,
    collector_checkout: Any = None,
    status_checkin: Any = None,
    status_checkout: Any = None,
    page_text: str = "",
    telemetry_url: str | None = None,
) -> DateIntegrityReport:
    """Resolve effective dates from the live page, never stale telemetry."""

    del telemetry_url  # Explicitly diagnostic history, never integrity evidence.
    expected_in = requested_checkin.isoformat()
    expected_out = requested_checkout.isoformat()
    query = query_date_values(page_url)
    url_in = tuple(query["checkin"])
    url_out = tuple(query["checkout"])
    visible_in = tuple(filter(None, (_date_text(value) for value in visible_checkin_values)))
    visible_out = tuple(filter(None, (_date_text(value) for value in visible_checkout_values)))
    hidden_in = tuple(filter(None, (_date_text(value) for value in hidden_checkin_values)))
    hidden_out = tuple(filter(None, (_date_text(value) for value in hidden_checkout_values)))
    collector_in = _date_text(collector_checkin)
    collector_out = _date_text(collector_checkout)
    status_in = _date_text(status_checkin)
    status_out = _date_text(status_checkout)

    reasons: list[str] = []
    if not url_in or not url_out:
        reasons.append("missing_effective_url_dates")
    if len(set(url_in)) > 1 or len(set(url_out)) > 1:
        reasons.append("conflicting_duplicate_url_dates")

    effective_in = url_in[-1] if url_in else (hidden_in[-1] if hidden_in else None)
    effective_out = url_out[-1] if url_out else (hidden_out[-1] if hidden_out else None)
    if effective_in != expected_in or effective_out != expected_out:
        reasons.append("effective_dates_do_not_match_requested")
    if collector_in not in (None, expected_in) or collector_out not in (None, expected_out):
        reasons.append("collector_options_do_not_match_requested")
    if status_in not in (None, expected_in) or status_out not in (None, expected_out):
        reasons.append("status_dates_do_not_match_requested")

    for label, values, expected in (
        ("visible_checkin", visible_in, expected_in),
        ("visible_checkout", visible_out, expected_out),
        ("hidden_checkin", hidden_in, expected_in),
        ("hidden_checkout", hidden_out, expected_out),
    ):
        if values and expected not in values:
            reasons.append(f"{label}_does_not_match_requested")

    return DateIntegrityReport(
        requested_checkin_date=expected_in,
        requested_checkout_date=expected_out,
        effective_checkin_date=effective_in,
        effective_checkout_date=effective_out,
        date_integrity_verified=not reasons,
        mismatch_reasons=tuple(dict.fromkeys(reasons)),
        url_checkin_values=url_in,
        url_checkout_values=url_out,
        visible_checkin_values=visible_in,
        visible_checkout_values=visible_out,
        hidden_checkin_values=hidden_in,
        hidden_checkout_values=hidden_out,
        collector_checkin_date=collector_in,
        collector_checkout_date=collector_out,
        status_checkin_date=status_in,
        status_checkout_date=status_out,
        page_text_contains_requested_checkin=expected_in in str(page_text or ""),
        page_text_contains_requested_checkout=expected_out in str(page_text or ""),
        page_url=str(page_url or ""),
    )
