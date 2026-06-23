"""Stable event identity helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(dt or "")


def build_event_key(
    condition_id: Optional[str] = None,
    slug: Optional[str] = None,
    event_start_time: Any = None,
    event_end_time: Any = None,
) -> str:
    """Return the canonical event key.

    Priority:
    1. Polymarket condition_id
    2. slug + event_start_time + event_end_time
    3. slug only
    """
    cond = str(condition_id or "").strip()
    if cond:
        return cond
    clean_slug = str(slug or "").strip()
    start = _iso(event_start_time)
    end = _iso(event_end_time)
    if clean_slug and start and end:
        return f"{clean_slug}:{start}:{end}"
    return clean_slug


def event_key_for_opportunity(opp: Any) -> str:
    return build_event_key(
        condition_id=getattr(opp, "condition_id", ""),
        slug=getattr(opp, "slug", ""),
        event_start_time=getattr(opp, "event_start_time", None),
        event_end_time=getattr(opp, "event_end_time", None),
    )


def event_key_for_signal(sig: Any) -> str:
    return build_event_key(
        condition_id=getattr(sig, "condition_id", ""),
        slug=getattr(sig, "slug", ""),
        event_start_time=getattr(sig, "event_start_time", None),
        event_end_time=getattr(sig, "event_end_time", None),
    )
