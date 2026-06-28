"""Structured JSONL journal for order lifecycle events."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .config import CONFIG
from .order_manager import PendingOrder


_EVENT_TYPES = frozenset({
    "ORDER_CREATED",
    "ORDER_FILLED",
    "ORDER_CANCELLED",
    "ORDER_EXPIRED",
    "ORDER_REJECTED",
})


def log_order_event(
    event_type: str,
    order: PendingOrder,
    *,
    current_bid: Optional[float] = None,
    current_ask: Optional[float] = None,
    current_synth_prob: Optional[float] = None,
    current_calibrated_prob: Optional[float] = None,
    current_edge: Optional[float] = None,
    current_liquidity: Optional[float] = None,
    time_to_resolution: Optional[float] = None,
    reason: Optional[str] = None,
) -> None:
    """Append one order lifecycle event to the order log."""
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"Unknown order event_type {event_type!r}")
    row: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "order_id": order.order_id,
        "condition_id": order.condition_id,
        "event_key": order.event_key,
        "asset": order.asset,
        "horizon": order.horizon,
        "side": order.side,
        "limit_price": order.limit_price,
        "size_usd": order.size_usd,
        "contracts": order.contracts,
        "edge_at_order": order.edge_at_order,
        "synth_prob_at_order": order.synth_prob_at_order,
        "calibrated_prob_at_order": order.calibrated_prob_at_order,
        "time_to_resolution_at_order": order.time_to_resolution_at_order,
        "current_bid": current_bid,
        "current_ask": current_ask,
        "current_synth_prob": current_synth_prob,
        "current_calibrated_prob": current_calibrated_prob,
        "current_edge": current_edge,
        "current_liquidity": current_liquidity,
        "time_to_resolution": time_to_resolution,
        "reason": reason,
        "mode": "paper",
    }
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    path = os.path.join(CONFIG.log_dir, "order_log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
