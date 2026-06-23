"""Exit-rule checks for open paper positions."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .calibration import Calibrator
from .config import CONFIG
from .synth_client import Opportunity


@dataclass
class ExitDecision:
    timestamp: str
    condition_id: str
    side: str
    reason: str
    calibrated_probability: float
    market_price: float
    edge: float
    seconds_to_event_end: Optional[float]


def _load_open_fills() -> List[Dict[str, Any]]:
    path = os.path.join(CONFIG.log_dir, "fills.jsonl")
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("resolved_outcome") or row.get("exit_timestamp"):
                continue
            rows.append(row)
    return rows


def _append_exit(decision: ExitDecision) -> None:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    path = os.path.join(CONFIG.log_dir, "exit_signals.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(decision), sort_keys=True) + "\n")


def _market_exit_price(opp: Opportunity, side: str) -> Optional[float]:
    side = side.upper()
    if side == "UP":
        return opp.yes_bid_price if opp.yes_bid_price is not None else opp.best_bid_price
    if side == "DOWN":
        if opp.no_bid_price is not None:
            return opp.no_bid_price
        if opp.best_ask_price is not None:
            return max(0.0, 1.0 - opp.best_ask_price)
    return None


def evaluate_exit_for_fill(
    fill: Dict[str, Any],
    opportunity: Opportunity,
    calibrator: Optional[Calibrator] = None,
) -> Optional[ExitDecision]:
    calibrator = calibrator or Calibrator()
    side = str(fill.get("side") or "").upper()
    p_up = calibrator.calibrate(opportunity.asset, opportunity.horizon, opportunity.synth_probability_up)
    calibrated_probability = p_up if side == "UP" else 1.0 - p_up
    market_price = _market_exit_price(opportunity, side)
    if market_price is None:
        return None

    edge = calibrated_probability - market_price
    reason = ""
    if edge < CONFIG.take_profit_edge_collapse:
        reason = "TAKE_PROFIT_EDGE_COLLAPSE"
    elif CONFIG.model_reversal_exit and calibrated_probability < 0.5:
        reason = "MODEL_REVERSAL_EXIT"
    elif (
        opportunity.seconds_to_event_end is not None
        and opportunity.seconds_to_event_end < CONFIG.time_stop_seconds
    ):
        reason = "TIME_STOP"

    if not reason:
        return None

    return ExitDecision(
        timestamp=datetime.now(timezone.utc).isoformat(),
        condition_id=str(fill.get("condition_id") or ""),
        side=side,
        reason=reason,
        calibrated_probability=round(calibrated_probability, 6),
        market_price=round(market_price, 6),
        edge=round(edge, 6),
        seconds_to_event_end=opportunity.seconds_to_event_end,
    )


def evaluate_open_exits(opportunities: Iterable[Opportunity]) -> List[ExitDecision]:
    by_key = {opp.slug: opp for opp in opportunities}
    calibrator = Calibrator()
    decisions: List[ExitDecision] = []
    for fill in _load_open_fills():
        condition_id = str(fill.get("condition_id") or "")
        opp = by_key.get(condition_id)
        if opp is None:
            continue
        decision = evaluate_exit_for_fill(fill, opp, calibrator=calibrator)
        if decision is not None:
            decisions.append(decision)
            _append_exit(decision)
    return decisions
