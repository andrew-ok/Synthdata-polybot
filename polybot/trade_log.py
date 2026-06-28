"""Structured trade journal.

Every signal scan, entry fill, rejection, exit, and close is written to
polybot/logs/trade_log.jsonl as a single JSON line.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import CONFIG

log = logging.getLogger(__name__)

_FIELDS = [
    "timestamp", "event_type", "market_id", "asset", "side", "execution_mode",
    "synth_up_prob", "synth_down_prob",
    "poly_up_bid", "poly_up_ask", "poly_down_bid", "poly_down_ask",
    "chosen_side", "entry_price", "exit_price",
    "entry_edge", "remaining_edge",
    "position_size", "reason",
    "time_to_resolution_sec",
    "estimated_fee", "rebate_estimate", "realized_pnl",
    "maker_fill_optimistic",
]


def _path() -> str:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    return os.path.join(CONFIG.log_dir, "trade_log.jsonl")


def _write(row: Dict[str, Any]) -> None:
    row.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def log_signal(
    *,
    event_type: str,   # "SCAN", "SKIP", "ENTRY", "EXIT", "CLOSE"
    signal: Any,
    reason: str = "",
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    position_size: Optional[float] = None,
    realized_pnl: Optional[float] = None,
    estimated_fee: float = 0.0,
    rebate_estimate: float = 0.0,
    maker_fill_optimistic: bool = False,
) -> None:
    try:
        remaining_edge = signal.raw_synth_probability - (signal.exit_price or 0.0)
        _write({
            "event_type": event_type,
            "market_id": getattr(signal, "condition_id", "") or getattr(signal, "event_key", ""),
            "asset": signal.asset,
            "side": signal.side,
            "execution_mode": getattr(signal, "execution_mode", "taker"),
            "synth_up_prob": getattr(signal, "raw_synth_probability", None) if signal.side == "UP" else round(1.0 - getattr(signal, "raw_synth_probability", 0.5), 4),
            "synth_down_prob": getattr(signal, "raw_synth_probability", None) if signal.side == "DOWN" else round(1.0 - getattr(signal, "raw_synth_probability", 0.5), 4),
            "poly_up_bid": signal.exit_price if signal.side == "UP" else None,
            "poly_up_ask": signal.entry_price if signal.side == "UP" else None,
            "poly_down_bid": signal.exit_price if signal.side == "DOWN" else None,
            "poly_down_ask": signal.entry_price if signal.side == "DOWN" else None,
            "chosen_side": signal.side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_edge": round(signal.net_edge, 4),
            "remaining_edge": round(remaining_edge, 4),
            "position_size": position_size,
            "reason": reason,
            "time_to_resolution_sec": getattr(signal, "seconds_to_event_end", None),
            "estimated_fee": estimated_fee,
            "rebate_estimate": rebate_estimate,
            "realized_pnl": realized_pnl,
            "maker_fill_optimistic": maker_fill_optimistic,
        })
    except Exception as exc:
        log.debug("trade_log write failed: %s", exc)


def log_exit(
    *,
    position: Any,
    decision: Any,
    signal: Optional[Any] = None,
) -> None:
    try:
        _write({
            "event_type": "EXIT",
            "market_id": position.condition_id or position.event_key,
            "asset": position.asset,
            "side": position.side,
            "execution_mode": "taker",  # exits are always taker
            "synth_up_prob": None,
            "synth_down_prob": None,
            "poly_up_bid": decision.exit_bid if position.side == "UP" else None,
            "poly_up_ask": None,
            "poly_down_bid": decision.exit_bid if position.side == "DOWN" else None,
            "poly_down_ask": None,
            "chosen_side": position.side,
            "entry_price": position.entry_price,
            "exit_price": decision.exit_fill_price,
            "entry_edge": position.entry_edge,
            "remaining_edge": round(decision.hold_edge, 4),
            "position_size": position.notional_usd,
            "reason": decision.reason,
            "time_to_resolution_sec": getattr(signal, "seconds_to_event_end", None) if signal else None,
            "estimated_fee": 0.0,
            "rebate_estimate": 0.0,
            "realized_pnl": decision.realized_pnl,
            "maker_fill_optimistic": False,
        })
    except Exception as exc:
        log.debug("trade_log exit write failed: %s", exc)
