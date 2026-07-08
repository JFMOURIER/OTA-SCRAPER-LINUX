from __future__ import annotations

import re
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from services.date_utils import calculate_checkout_date


RESULT_FIELDS = [
    "collection_run_id",
    "source",
    "city_or_region",
    "search_url",
    "hotel_name",
    "raw_hotel_name",
    "property_type_guess",
    "excluded_by_hotels_only_filter",
    "ota_hotel_id",
    "star_rating",
    "raw_star_signal",
    "star_aria_label",
    "star_icon_count",
    "star_rating_missing_reason",
    "review_score",
    "review_count",
    "room_name",
    "cheapest_room_name",
    "raw_price_text",
    "parsed_price",
    "cheapest_price_total",
    "currency",
    "taxes_and_fees_text",
    "checkin_date",
    "checkout_date",
    "number_of_nights",
    "adults",
    "hotel_url",
    "provider_name",
    "raw_source_payload",
    "ranking_position_on_page",
    "screenshot_path",
    "collection_status",
    "error_message",
    "collected_at",
]


HOTEL_POSITIVE_PATTERNS = [
    r"\bhotel\b",
    r"\binn\b",
    r"\bsuites?\b",
    r"\bresort\b",
    r"\bmotel\b",
    r"\blodge\b",
    r"\bextended stay\b",
    r"\bhampton\b",
    r"\bdays inn\b",
    r"\bfairfield\b",
    r"\bcourtyard\b",
    r"\bresidence inn\b",
    r"\bla quinta\b",
    r"\bwyndham\b",
    r"\bmarriott\b",
    r"\bhilton\b",
    r"\bsonesta\b",
    r"\bcomfort inn\b",
    r"\bquality inn\b",
    r"\bbest western\b",
    r"\bholiday inn\b",
    r"\bhyatt\b",
    r"\bsheraton\b",
    r"\bramada\b",
]


RENTAL_NEGATIVE_PATTERNS = [
    r"\bapartments?\b",
    r"\bstudio\b",
    r"\bprivate rooms?\b",
    r"\bguest rooms?\b",
    r"\bguest suite\b",
    r"\bshared bath\b",
    r"\bhomestay\b",
    r"\bvacation home\b",
    r"\bholiday home\b",
    r"\bvillas?\b",
    r"\bbnb\b",
    r"\bbed and breakfast\b",
    r"\bentire home\b",
    r"\bentire apartment\b",
    r"\bcondo\b",
    r"\btownhouse\b",
    r"\bhouse\b",
    r"\broom in\b",
]


UNWANTED_NAME_PHRASES = [
    "opens in a new window",
    "opens in new window",
    "new window",
    "external link",
    "opens external site",
]


def clean_hotel_name(raw_name: Any) -> str | None:
    if raw_name is None:
        return None
    text = str(raw_name)
    for phrase in UNWANTED_NAME_PHRASES:
        text = re.sub(re.escape(phrase), " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    parts = text.split(" ")
    midpoint = len(parts) // 2
    if len(parts) % 2 == 0 and parts[:midpoint] == parts[midpoint:]:
        text = " ".join(parts[:midpoint])
    return text


def property_type_guess(name: Any, card_text: Any = None) -> str:
    haystack = f"{name or ''} {card_text or ''}".lower()
    if any(re.search(pattern, haystack, re.I) for pattern in HOTEL_POSITIVE_PATTERNS):
        return "probable_hotel"
    if any(re.search(pattern, haystack, re.I) for pattern in RENTAL_NEGATIVE_PATTERNS):
        return "probable_vacation_rental_or_private_accommodation"
    return "unknown"


def is_probable_hotel(name: Any, card_text: Any = None) -> bool:
    guess = property_type_guess(name, card_text)
    if guess == "probable_hotel":
        return True
    if guess == "probable_vacation_rental_or_private_accommodation":
        return False
    return True


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_price_to_decimal(price_text: Any) -> Decimal | None:
    if price_text is None or isinstance(price_text, (int, float, Decimal)):
        return _to_decimal(price_text)

    text = str(price_text).strip()
    if not text:
        return None

    text = text.replace("\xa0", " ")
    compact_amounts = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    if len(compact_amounts) > 1:
        text = compact_amounts[-1]

    cleaned = re.sub(r"[^\d,.\-]", "", text)
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    return _to_decimal(cleaned)


def parse_review_score(review_text: Any) -> Decimal | None:
    if review_text is None or isinstance(review_text, (int, float, Decimal)):
        return _to_decimal(review_text)
    match = re.search(r"\d+(?:[.,]\d+)?", str(review_text))
    if not match:
        return None
    return _to_decimal(match.group(0).replace(",", "."))


def parse_review_count(review_count_text: Any) -> int | None:
    if review_count_text is None:
        return None
    if isinstance(review_count_text, int):
        return review_count_text
    text = str(review_count_text)
    match = re.search(r"[\d,.]+", text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    return int(digits) if digits else None


def parse_star_rating(star_text: Any) -> Decimal | None:
    if star_text is None or isinstance(star_text, (int, float, Decimal)):
        return _to_decimal(star_text)
    text = str(star_text)
    star_chars = re.findall(r"[\u2605\u2b50\u2729]", text)
    if 1 <= len(star_chars) <= 5:
        return Decimal(len(star_chars))
    bounded_match = re.search(r"\b([1-5])\s*(?:out\s+of|of|sur)\s*5\b", text, re.I)
    if bounded_match:
        return Decimal(bounded_match.group(1))
    star_word_match = re.search(r"\b([1-5])\s*(?:star|stars|etoile|etoiles|\u00e9toile|\u00e9toiles)\b", text, re.I)
    if star_word_match:
        return Decimal(star_word_match.group(1))
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if match:
        return _to_decimal(match.group(0).replace(",", "."))
    lowered = text.lower()
    if "five" in lowered:
        return Decimal("5")
    if "four" in lowered:
        return Decimal("4")
    if "three" in lowered:
        return Decimal("3")
    if "two" in lowered:
        return Decimal("2")
    if "one" in lowered:
        return Decimal("1")
    if "étoile" in lowered or "etoile" in lowered or "Ã©toile" in lowered:
        word_map = {
            "cinq": "5",
            "quatre": "4",
            "trois": "3",
            "deux": "2",
            "une": "1",
            "un": "1",
        }
        for word, number in word_map.items():
            if word in lowered:
                return Decimal(number)
    return None


def normalize_hotel_result(raw_result: dict[str, Any]) -> dict[str, Any]:
    checkin_date = raw_result.get("checkin_date")
    number_of_nights = int(raw_result.get("number_of_nights") or 1)
    checkout_date = raw_result.get("checkout_date")

    if isinstance(checkin_date, str):
        checkin_date = date.fromisoformat(checkin_date)
    if checkout_date is None and checkin_date is not None:
        checkout_date = calculate_checkout_date(checkin_date, number_of_nights)
    elif isinstance(checkout_date, str):
        checkout_date = date.fromisoformat(checkout_date)

    normalized = {field: raw_result.get(field) for field in RESULT_FIELDS}
    normalized["hotel_name"] = clean_hotel_name(raw_result.get("hotel_name"))
    normalized["star_rating"] = parse_star_rating(raw_result.get("star_rating"))
    normalized["raw_star_signal"] = raw_result.get("raw_star_signal")
    normalized["star_aria_label"] = raw_result.get("star_aria_label")
    normalized["star_icon_count"] = raw_result.get("star_icon_count")
    normalized["star_rating_missing_reason"] = raw_result.get("star_rating_missing_reason")
    normalized["review_score"] = parse_review_score(raw_result.get("review_score"))
    normalized["review_count"] = parse_review_count(raw_result.get("review_count"))
    normalized["room_name"] = raw_result.get("room_name") or raw_result.get("cheapest_room_name")
    normalized["cheapest_room_name"] = raw_result.get("cheapest_room_name") or raw_result.get("room_name")
    raw_price_text = raw_result.get("raw_price_text") or raw_result.get("cheapest_price_total")
    parsed_price = parse_price_to_decimal(raw_result.get("parsed_price") or raw_price_text)
    normalized["raw_price_text"] = raw_price_text
    normalized["parsed_price"] = parsed_price
    normalized["cheapest_price_total"] = parse_price_to_decimal(raw_result.get("cheapest_price_total")) or parsed_price
    normalized["checkin_date"] = checkin_date
    normalized["checkout_date"] = checkout_date
    normalized["number_of_nights"] = number_of_nights
    normalized["adults"] = int(raw_result.get("adults") or 2)
    normalized["provider_name"] = raw_result.get("provider_name") or raw_result.get("source")
    raw_source_payload = raw_result.get("raw_source_payload")
    if isinstance(raw_source_payload, (dict, list)):
        raw_source_payload = json.dumps(raw_source_payload, ensure_ascii=True, default=str)
    normalized["raw_source_payload"] = raw_source_payload
    normalized["collection_status"] = raw_result.get("collection_status") or "success"
    normalized["collected_at"] = raw_result.get("collected_at") or datetime.now()
    return normalized
