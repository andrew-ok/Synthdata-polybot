"""Pending maker order lifecycle store for paper trading.

An order is PENDING until it is FILLED or CANCELLED. Only filled orders
create open positions or mark markets as traded. Cancelled/rejected orders
leave the market available for future entries.

Append-only event log at CONFIG.orders_path:
  {"type": "create",  "order": {...}}
  {"type": "fill",    "order_id": "...", "fields": {...}}
  {"type": "cancel",  "order_id": "...", "fields": {...}}
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, fields as dc_fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import CONFIG


@dataclass
class PendingOrder:
    order_id: str
    condition_id: str
    event_key: str
    asset: str
    horizon: str
    side: str
    limit_price: float          # maker limit price (bid + offset)
    size_usd: float
    contracts: float
    synth_prob_at_order: float
    calibrated_prob_at_order: float
    edge_at_order: float
    created_at: str
    time_to_resolution_at_order: Optional[float]
    expires_at: Optional[str] = None
    status: str = "PENDING"     # PENDING | FILLED | CANCELLED | EXPIRED
    cancel_reason: Optional[str] = None
    filled_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    fill_price: Optional[float] = None
    fill_best_bid: Optional[float] = None
    fill_best_ask: Optional[float] = None
    # "ENTRY" for resting buy orders; "EXIT" for post-only sell limit orders.
    order_type: str = "ENTRY"
    # For EXIT orders: the position this order is meant to close.
    position_id: Optional[str] = None


def _orders_path() -> str:
    return CONFIG.orders_path


def _append(row: Dict[str, Any]) -> None:
    path = _orders_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _load_events() -> List[Dict[str, Any]]:
    path = _orders_path()
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _order_from_dict(data: Dict[str, Any]) -> PendingOrder:
    valid = {f.name for f in dc_fields(PendingOrder)}
    return PendingOrder(**{k: v for k, v in data.items() if k in valid})


def load_orders(status: Optional[str] = None) -> List[PendingOrder]:
    """Replay order events and return current order states."""
    orders: Dict[str, Dict[str, Any]] = {}
    for event in _load_events():
        kind = event.get("type")
        if kind == "create":
            data = dict(event["order"])
            orders[data["order_id"]] = data
        elif kind in ("fill", "cancel"):
            oid = event.get("order_id")
            if oid in orders:
                orders[oid].update(event.get("fields") or {})
    out = [_order_from_dict(o) for o in orders.values()]
    if status:
        out = [o for o in out if o.status == status]
    return out


def get_pending_orders() -> List[PendingOrder]:
    return load_orders("PENDING")


def has_pending_order_for_condition(condition_id: str) -> bool:
    """True if any PENDING order exists for this condition_id."""
    if not condition_id:
        return False
    return any(o.condition_id == condition_id for o in get_pending_orders())


def has_pending_order_for_event(event_key: str) -> bool:
    """True if any PENDING order exists for this event_key."""
    if not event_key:
        return False
    return any(o.event_key == event_key for o in get_pending_orders())


def create_order(
    signal: Any,
    size_usd: float,
    contracts: float,
) -> PendingOrder:
    """Create and persist a PENDING maker order from a signal."""
    now = datetime.now(timezone.utc).isoformat()
    order = PendingOrder(
        order_id=str(uuid.uuid4()),
        condition_id=signal.condition_id or "",
        event_key=signal.event_key,
        asset=signal.asset,
        horizon=signal.horizon,
        side=signal.side,
        limit_price=signal.execution_price,
        size_usd=size_usd,
        contracts=contracts,
        synth_prob_at_order=signal.raw_synth_probability,
        calibrated_prob_at_order=signal.fair_probability,
        edge_at_order=signal.net_edge,
        created_at=now,
        time_to_resolution_at_order=signal.seconds_to_event_end,
        status="PENDING",
    )
    _append({"type": "create", "order": asdict(order)})
    return order


def fill_order(
    order: PendingOrder,
    fill_price: float,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
) -> PendingOrder:
    """Mark an order as FILLED and persist."""
    now = datetime.now(timezone.utc).isoformat()
    fields = {
        "status": "FILLED",
        "filled_at": now,
        "fill_price": fill_price,
        "fill_best_bid": best_bid,
        "fill_best_ask": best_ask,
    }
    _append({"type": "fill", "order_id": order.order_id, "fields": fields})
    data = asdict(order)
    data.update(fields)
    return PendingOrder(**data)


def cancel_order(order: PendingOrder, reason: str) -> PendingOrder:
    """Mark an order as CANCELLED and persist."""
    now = datetime.now(timezone.utc).isoformat()
    fields = {
        "status": "CANCELLED",
        "cancelled_at": now,
        "cancel_reason": reason,
    }
    _append({"type": "cancel", "order_id": order.order_id, "fields": fields})
    data = asdict(order)
    data.update(fields)
    return PendingOrder(**data)


def get_order_by_id(order_id: str) -> Optional[PendingOrder]:
    """Return the order with the given order_id (any status), or None."""
    for order in load_orders():
        if order.order_id == order_id:
            return order
    return None


def create_exit_order(
    position: Any,
    shares: float,
    limit_price: float,
    expires_at: Optional[str] = None,
) -> PendingOrder:
    """Create and persist a PENDING post-only exit (sell) limit order."""
    now = datetime.now(timezone.utc).isoformat()
    order = PendingOrder(
        order_id=str(uuid.uuid4()),
        condition_id=position.condition_id or "",
        event_key=position.event_key,
        asset=position.asset,
        horizon=position.horizon,
        side=position.side,
        limit_price=limit_price,
        size_usd=round(limit_price * shares, 4),
        contracts=shares,
        synth_prob_at_order=position.synth_p_current,
        calibrated_prob_at_order=position.synth_p_current,
        edge_at_order=round(limit_price - position.avg_entry_price, 4),
        created_at=now,
        time_to_resolution_at_order=None,
        expires_at=expires_at,
        status="PENDING",
        order_type="EXIT",
        position_id=position.position_id,
    )
    _append({"type": "create", "order": asdict(order)})
    return order
