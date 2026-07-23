from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HotelClassification:
    is_hotel_eligible: bool
    classification_reason: str
    property_type: str | None = None


_STRUCTURED_RENTAL_PATTERNS = (
    r"\bprivate (?:room|accommodation)\b",
    r"\bvacation (?:home|rentals?|retreat)\b",
    r"\bholiday home\b",
    r"\bhomestay\b",
    r"\bentire (?:apartment|house|home|place)\b",
    r"\bapartment\b",
    r"\bvilla\b",
    r"\bcondo(?:minium)?\b",
    r"\btownhouse\b",
    r"\broom in\b",
    r"\bhosted by\b",
    r"\bguest room\b",
)

_STRUCTURED_HOTEL_PATTERNS = (
    r"\bhotel\b",
    r"\bmotel\b",
    r"\binn\b",
    r"\blodge\b",
    r"\bresort\b",
    r"\bhotel chain\b",
)

_NAME_RENTAL_PATTERNS = (
    *_STRUCTURED_RENTAL_PATTERNS,
    r"\bresort dwelling\b",
    r"\bprivate room and bath\b",
    r"\bsuite with bath near\b",
    r"\bshared bath\b",
    r"\bguest suite\b",
    r"\bbed and breakfast\b",
    r"\bbnb\b",
)


def _compact(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        try:
            value = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
        except (TypeError, ValueError):
            value = str(value)
    return " ".join(str(value).lower().split())


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, re.I):
            return pattern
    return None


def classify_booking_property(
    *,
    hotel_name: Any,
    structured_metadata: Any = None,
    card_text: Any = None,
) -> HotelClassification:
    """Classify Booking results consistently for DOM and network records.

    Explicit private/rental evidence wins over generic hotel words.  This is
    important for names such as "Resort Private Room", which the old
    positive-first classifier incorrectly retained.
    """

    structured = _compact(structured_metadata)
    name = _compact(hotel_name)
    card = _compact(card_text)

    if structured:
        negative = _first_match(structured, _STRUCTURED_RENTAL_PATTERNS)
        if negative:
            return HotelClassification(
                False,
                f"structured_rental_indicator:{negative}",
                structured[:240],
            )
        positive = _first_match(structured, _STRUCTURED_HOTEL_PATTERNS)
        if positive:
            return HotelClassification(
                True,
                f"structured_hotel_indicator:{positive}",
                structured[:240],
            )

    fallback = " ".join(value for value in (name, card) if value)
    negative = _first_match(fallback, _NAME_RENTAL_PATTERNS)
    if negative:
        return HotelClassification(
            False,
            f"name_or_card_rental_indicator:{negative}",
            structured[:240] or None,
        )

    # Unknown properties remain eligible. Booking's server-side Hotels filter
    # is useful evidence, while an aggressive name allow-list would exclude
    # legitimate independent hotels whose names omit "Hotel" or "Inn".
    return HotelClassification(
        True,
        "no_rental_indicator",
        structured[:240] or None,
    )
