"""Position-based paper exit rules."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .config import CONFIG
from .position_manager import Position, close_position, open_positions, update_position
from .signal_engine import Signal


@dataclass
class ExitDecision:
    timestamp: str
    position_id: str
    event_key: str
    side: str
    reason: str
    fair_probability: float
    exit_bid: float
    exit_fill_price: float
    hold_edge: float
    score: float
    realized_pnl: float


def _exit_fill_price(bid: float) -> float:
    slip = CONFIG.assumed_slippage_bps / 10_000.0
    return max(0.001, bid - slip)


def _append_exit(decision: ExitDecision) -> None:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    with open(os.path.join(CONFIG.log_dir, "exit_signals.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(decision), sort_keys=True) + "\n")


def _append_fill(position: Position, decision: ExitDecision) -> None:
    row = {
        "timestamp": decision.timestamp,
        "entry_or_exit": "exit",
        "position_id": position.position_id,
        "event_key": position.event_key,
        "condition_id": position.condition_id,
        "slug": position.slug,
        "asset": position.asset,
        "horizon": position.horizon,
        "side": position.side,
        "intended_price": decision.exit_bid,
        "fill_price": decision.exit_fill_price,
        "estimated_slippage": decision.exit_bid - decision.exit_fill_price,
        "estimated_fee": (decision.exit_fill_price * position.contracts) * (CONFIG.taker_fee_bps / 10_000.0),
        "contracts": position.contracts,
        "notional_usd": decision.exit_fill_price * position.contracts,
        "fair_probability": decision.fair_probability,
        "entry_edge": position.entry_edge,
        "score": decision.score,
        "exit_reason": decision.reason,
        "realized_pnl": decision.realized_pnl,
        "mode": "paper",
    }
    with open(os.path.join(CONFIG.log_dir, "fills.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _exit_reason(position: Position, same_side: Optional[Signal], opposite: Optional[Signal]) -> Optional[str]:
    if same_side is None:
        return "STALE_DATA"
    hold_edge = same_side.fair_probability - same_side.exit_price - _exit_cost()
    if hold_edge < CONFIG.min_exit_edge:
        return "EDGE_COLLAPSE"
    if opposite is not None and opposite.net_edge > same_side.net_edge:
        return "MODEL_REVERSAL"
    if same_side.score < CONFIG.min_confidence_score * CONFIG.min_entry_edge:
        return "RANK_DECAY"
    if same_side.seconds_to_event_end is not None and same_side.seconds_to_event_end < CONFIG.time_stop_seconds:
        return "TIME_STOP"
    return None


def _exit_cost() -> float:
    return (CONFIG.taker_fee_bps + CONFIG.assumed_slippage_bps) / 10_000.0


def evaluate_and_apply_exits(signals: Iterable[Signal]) -> List[ExitDecision]:
    signal_map: Dict[tuple[str, str], Signal] = {(s.event_key, s.side): s for s in signals}
    decisions: List[ExitDecision] = []
    for position in open_positions():
        same_side = signal_map.get((position.event_key, position.side))
        opposite_side = "DOWN" if position.side == "UP" else "UP"
        opposite = signal_map.get((position.event_key, opposite_side))
        reason = _exit_reason(position, same_side, opposite)
        if not reason:
            if same_side is not None:
                update_position(
                    position.position_id,
                    latest_raw_synth_probability=same_side.raw_synth_probability,
                    latest_fair_probability=same_side.fair_probability,
                    latest_bid=same_side.exit_price,
                    latest_ask=same_side.entry_price,
                    latest_score=same_side.score,
                )
            continue

        bid = same_side.exit_price if same_side is not None else position.latest_bid
        fair = same_side.fair_probability if same_side is not None else position.latest_fair_probability
        score = same_side.score if same_side is not None else position.latest_score
        fill_px = _exit_fill_price(bid)
        closed = close_position(position, fill_px, reason)
        decision = ExitDecision(
            timestamp=datetime.now(timezone.utc).isoformat(),
            position_id=position.position_id,
            event_key=position.event_key,
            side=position.side,
            reason=reason,
            fair_probability=fair,
            exit_bid=bid,
            exit_fill_price=fill_px,
            hold_edge=fair - bid - _exit_cost(),
            score=score,
            realized_pnl=closed.realized_pnl or 0.0,
        )
        _append_exit(decision)
        _append_fill(position, decision)
        decisions.append(decision)
    return decisions
