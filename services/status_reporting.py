from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_status_fields(
    status_updates: Mapping[str, Any] | None = None,
    resource_metrics: Mapping[str, Any] | None = None,
    authoritative_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one keyword dictionary with explicit last-writer precedence.

    Collector status updates are the least authoritative values, the current
    resource snapshot wins over them, and explicitly authoritative fields win
    over both. Returning one dictionary prevents duplicate ``**kwargs`` from
    raising before the status writer can run.
    """

    fields = dict(status_updates or {})
    fields.update(resource_metrics or {})
    fields.update(authoritative_fields or {})
    return fields
