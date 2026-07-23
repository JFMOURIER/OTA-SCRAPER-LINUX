from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from services.job_runner import atomic_write_json


PARTIAL_SCHEMA_VERSION = 2
POLICY_DISABLED = "disabled"
POLICY_EXPLICIT = "explicit_resume"
POLICY_SAME_JOB = "same_job_crash_recovery"

CONFIG_FIELDS = (
    "instance_id",
    "source",
    "city_or_destination_id",
    "checkin",
    "checkout",
    "stay_length",
    "adults",
    "rooms",
    "currency",
    "selected_star_ratings",
    "include_unknown_star_rating",
    "hotels_only",
    "collect_all",
    "maximum_hotels",
    "sort_order",
)


def metadata_path(partial_dir: Path, stay_date: Any) -> Path:
    return Path(partial_dir) / f"{stay_date}_partial_metadata.json"


def normalized_fingerprint(values: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in CONFIG_FIELDS:
        value = values.get(key)
        if isinstance(value, (list, tuple, set)):
            value = sorted(value)
        if isinstance(value, str):
            value = value.strip()
        normalized[key] = value
    return normalized


def fingerprint_hash(values: dict[str, Any]) -> str:
    payload = json.dumps(
        normalized_fingerprint(values),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_partial_metadata(
    fingerprint: dict[str, Any],
    *,
    job_id: str | None,
    run_id: int | None,
    collector_version: str = "booking_playwright",
) -> dict[str, Any]:
    normalized = normalized_fingerprint(fingerprint)
    return {
        "schema_version": PARTIAL_SCHEMA_VERSION,
        "collector_version": collector_version,
        "created_at": datetime.now().isoformat(),
        "job_id": job_id,
        "run_id": run_id,
        "fingerprint": normalized,
        "fingerprint_hash": fingerprint_hash(normalized),
    }


def write_partial_metadata(
    partial_dir: Path,
    stay_date: Any,
    metadata: dict[str, Any],
) -> Path:
    return atomic_write_json(metadata_path(partial_dir, stay_date), metadata)


@dataclass(frozen=True, slots=True)
class PartialDecision:
    allowed: bool
    log_message: str


def partial_load_decision(
    *,
    policy: str,
    expected_fingerprint: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    current_job_id: str | None,
    current_run_id: int | None,
) -> PartialDecision:
    if policy == POLICY_DISABLED:
        return PartialDecision(False, "Partial rejected: resume disabled")
    if not isinstance(metadata, dict) or int(metadata.get("schema_version") or 0) != PARTIAL_SCHEMA_VERSION:
        return PartialDecision(False, "Partial rejected: schema mismatch")
    actual = metadata.get("fingerprint")
    if not isinstance(actual, dict):
        return PartialDecision(False, "Partial rejected: schema mismatch")
    expected = normalized_fingerprint(expected_fingerprint or {})
    actual_normalized = normalized_fingerprint(actual)
    if actual_normalized.get("checkin") != expected.get("checkin"):
        return PartialDecision(False, "Partial rejected: configuration mismatch")
    if fingerprint_hash(actual_normalized) != fingerprint_hash(expected):
        return PartialDecision(False, "Partial rejected: configuration mismatch")
    if policy == POLICY_SAME_JOB:
        if (
            str(metadata.get("job_id") or "") != str(current_job_id or "")
            or int(metadata.get("run_id") or -1) != int(current_run_id or -2)
        ):
            return PartialDecision(False, "Partial rejected: job mismatch")
        return PartialDecision(True, "Partial loaded: same-job crash recovery")
    if policy == POLICY_EXPLICIT:
        return PartialDecision(
            True,
            "Partial loaded: explicit resume enabled and fingerprint matched",
        )
    return PartialDecision(False, "Partial rejected: resume disabled")


def read_partial_metadata(partial_dir: Path, stay_date: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(metadata_path(partial_dir, stay_date).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None
