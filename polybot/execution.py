"""Execution layer.

Paper-trading: simulates fills at the best ask plus a configured slippage.
Live trading: scaffolded but DISABLED. Will only emit limit orders, and even
then only if `ALLOW_MARKETABLE_ORDERS=true` was explicitly set.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from .config import CONFIG
from .position_manager import has_open_event, open_position_from_fill
from .risk_manager import Decision
from .reports import write_daily_report

log = logging.getLogger(__name__)


@dataclass
class Fill:
    timestamp: str
    asset: str
    horizon: str
    slug: str
    event_key: str
    market_url: str
    condition_id: str
    side: str
    synth_probability: float
    calibrated_probability: float
    intended_price: float
    fill_price: float
    estimated_slippage: float
    estimated_fee: float
    contracts: float
    notional_usd: float
    raw_edge: float
    calibrated_edge: float
    net_edge: float
    model_confidence: float
    expected_value_score: float
    confidence_score: float
    liquidity_score: float
    regime_score: float
    best_bid: float
    best_ask: float
    spread: Optional[float]
    entry_or_exit: str
    entry_reason: str
    exit_reason: Optional[str]
    market_question: str
    mode: str            # "paper" or "live"
    position_id: Optional[str] = None


def _slippage_adjusted_ask(ask: float) -> float:
    slip = CONFIG.assumed_slippage_bps / 10_000.0
    return min(0.999, ask + slip)


def _slippage_amount(price: float) -> float:
    return min(0.999, price + CONFIG.assumed_slippage_bps / 10_000.0) - price


def _ensure_logdir() -> str:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    return CONFIG.log_dir


def _append_jsonl(path: str, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def log_skip(decision: Decision) -> None:
    path = os.path.join(_ensure_logdir(), "skipped.jsonl")
    _append_jsonl(path, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "condition_id": decision.signal.condition_id,
        "side": decision.signal.side,
        "market_question": decision.signal.market_question,
        "raw_edge": decision.signal.raw_edge,
        "net_edge": decision.signal.net_edge,
        "reason": decision.reason,
    })


def paper_fill(decision: Decision, position_id: Optional[str] = None) -> Fill:
    CONFIG.assert_paper_only()
    sig = decision.signal
    if has_open_event(sig.event_key):
        raise RuntimeError(f"Refusing duplicate paper fill for open event_key={sig.event_key}")
    fill_px = _slippage_adjusted_ask(sig.execution_price)
    estimated_slippage = _slippage_amount(sig.execution_price)
    notional = round(fill_px * decision.contracts, 4)
    fee = round(notional * (CONFIG.taker_fee_bps / 10_000.0), 6)
    fill = Fill(
        timestamp=datetime.now(timezone.utc).isoformat(),
        asset=sig.asset,
        horizon=sig.horizon,
        slug=sig.slug,
        event_key=sig.event_key,
        market_url=sig.market_url,
        condition_id=sig.condition_id,
        side=sig.side,
        synth_probability=sig.synth_probability,
        calibrated_probability=sig.calibrated_probability,
        intended_price=sig.execution_price,
        fill_price=fill_px,
        estimated_slippage=estimated_slippage,
        estimated_fee=fee,
        contracts=decision.contracts,
        notional_usd=notional,
        raw_edge=sig.raw_edge,
        calibrated_edge=sig.calibrated_edge,
        net_edge=sig.net_edge,
        model_confidence=sig.model_confidence,
        expected_value_score=sig.expected_value_score,
        confidence_score=sig.confidence_score,
        liquidity_score=sig.liquidity_score,
        regime_score=sig.regime_score,
        best_bid=sig.exit_price,
        best_ask=sig.entry_price,
        spread=sig.spread,
        entry_or_exit="entry",
        entry_reason=decision.reason,
        exit_reason=None,
        market_question=sig.market_question,
        mode="paper",
        position_id=position_id,
    )

    log_dir = _ensure_logdir()
    _append_jsonl(os.path.join(log_dir, "fills.jsonl"), asdict(fill))

    csv_path = os.path.join(log_dir, "fills.csv")
    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(fill).keys()))
        if new_file:
            w.writeheader()
        w.writerow(asdict(fill))
    return fill


def execute_decisions(decisions: Iterable[Decision]) -> List[Fill]:
    fills: List[Fill] = []
    for d in decisions:
        if not d.accepted:
            log_skip(d)
            continue
        try:
            pos_id = str(uuid.uuid4())
            fill = paper_fill(d, position_id=pos_id)
        except RuntimeError as exc:
            log.warning("%s", exc)
            log_skip(Decision(d.signal, False, str(exc)))
            continue
        open_position_from_fill(fill, d.signal, position_id=pos_id)
        fills.append(fill)
    if fills:
        write_daily_report()
    log.info("Execution: %d fills written (paper)", len(fills))
    return fills


# --- Live trading stub ----------------------------------------------------

def place_live_limit_order(*_, **__) -> None:
    """Live trading is intentionally not implemented in this build."""
    CONFIG.assert_live_allowed()
    raise NotImplementedError(
        "Live trading is disabled. Implement CLOB signing/order placement only "
        "after paper-trading and backtests confirm Synth is well-calibrated."
    )
