"""Durable paper position ledger."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .config import CONFIG


@dataclass
class Position:
    position_id: str
    event_key: str
    condition_id: str
    slug: str
    asset: str
    horizon: str
    side: str
    status: str
    entry_time: str
    entry_price: float
    contracts: float
    notional_usd: float
    entry_raw_synth_probability: float
    entry_fair_probability: float
    entry_edge: float
    entry_score: float
    latest_raw_synth_probability: float
    latest_fair_probability: float
    latest_bid: float
    latest_ask: float
    latest_score: float
    latest_update_time: str
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None
    low_score_count: int = 0


def _append(row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG.positions_path), exist_ok=True)
    with open(CONFIG.positions_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _load_events() -> List[Dict[str, Any]]:
    if not os.path.exists(CONFIG.positions_path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(CONFIG.positions_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_positions(status: Optional[str] = None) -> List[Position]:
    positions: Dict[str, Dict[str, Any]] = {}
    for event in _load_events():
        kind = event.get("type")
        if kind == "open":
            data = dict(event["position"])
            positions[data["position_id"]] = data
        elif kind == "update":
            pid = event.get("position_id")
            if pid in positions:
                positions[pid].update(event.get("fields") or {})
        elif kind == "close":
            pid = event.get("position_id")
            if pid in positions:
                positions[pid].update(event.get("fields") or {})
                positions[pid]["status"] = "closed"
    out = [Position(**p) for p in positions.values()]
    if status:
        out = [p for p in out if p.status == status]
    return out


def open_positions() -> List[Position]:
    return load_positions("open")


def has_open_event(event_key: str) -> bool:
    return any(p.event_key == event_key and p.status == "open" for p in open_positions())


def recently_closed_or_opened(event_key: str, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    cutoff = CONFIG.position_cooldown_seconds
    for p in load_positions():
        if p.event_key != event_key:
            continue
        raw = p.exit_time or p.entry_time
        try:
            t = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if (now - t).total_seconds() < cutoff:
            return True
    return False


def exposure_summary() -> Dict[str, Any]:
    positions = open_positions()
    by_asset: Dict[str, float] = {}
    by_horizon: Dict[str, float] = {}
    for p in positions:
        by_asset[p.asset] = by_asset.get(p.asset, 0.0) + p.notional_usd
        by_horizon[p.horizon] = by_horizon.get(p.horizon, 0.0) + p.notional_usd
    return {
        "total": sum(p.notional_usd for p in positions),
        "count": len(positions),
        "by_asset": by_asset,
        "by_horizon": by_horizon,
    }


def open_position_from_fill(fill: Any, signal: Any, position_id: Optional[str] = None) -> Position:
    now = datetime.now(timezone.utc).isoformat()
    position = Position(
        position_id=position_id or str(uuid.uuid4()),
        event_key=signal.event_key,
        condition_id=signal.condition_id,
        slug=signal.slug,
        asset=signal.asset,
        horizon=signal.horizon,
        side=signal.side,
        status="open",
        entry_time=now,
        entry_price=fill.fill_price,
        contracts=fill.contracts,
        notional_usd=fill.notional_usd,
        entry_raw_synth_probability=signal.raw_synth_probability,
        entry_fair_probability=signal.fair_probability,
        entry_edge=signal.net_edge,
        entry_score=signal.score,
        latest_raw_synth_probability=signal.raw_synth_probability,
        latest_fair_probability=signal.fair_probability,
        latest_bid=signal.exit_price,
        latest_ask=signal.entry_price,
        latest_score=signal.score,
        latest_update_time=now,
    )
    _append({"type": "open", "position": asdict(position)})
    return position


def update_position(position_id: str, **fields: Any) -> None:
    fields["latest_update_time"] = datetime.now(timezone.utc).isoformat()
    _append({"type": "update", "position_id": position_id, "fields": fields})


def close_position(position: Position, exit_price: float, exit_reason: str) -> Position:
    now = datetime.now(timezone.utc).isoformat()
    pnl = round((exit_price * position.contracts) - position.notional_usd, 4)
    fields = {
        "status": "closed",
        "exit_time": now,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "realized_pnl": pnl,
    }
    _append({"type": "close", "position_id": position.position_id, "fields": fields})
    data = asdict(position)
    data.update(fields)
    return Position(**data)
