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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, List

from .config import CONFIG
from .risk_manager import Decision
from .reports import write_daily_report

log = logging.getLogger(__name__)


@dataclass
class Fill:
    timestamp: str
    asset: str
    horizon: str
    slug: str
    market_url: str
    condition_id: str
    side: str
    synth_probability: float
    calibrated_probability: float
    intended_price: float
    fill_price: float
    contracts: float
    notional_usd: float
    raw_edge: float
    calibrated_edge: float
    net_edge: float
    model_confidence: float
    expected_value_score: float
    market_question: str
    mode: str            # "paper" or "live"
    entry_style: str = "taker"
    event_start_time: str = ""   # ISO — window this position resolves on
    event_end_time: str = ""
    # --- settlement fields (written later by settlement.py; absent = open) ---
    # resolved_outcome: "UP"/"DOWN" | exit_timestamp / exit_price / exit_reason
    # | realized_pnl (USD) | close_kind: "exit_at_fair" | "resolution"


def _taker_fill_price(ask: float) -> float:
    """Effective cost per contract for a taker entry: ask + slippage + taker
    fee. Both are PROPORTIONAL to price (true bps) — absolute slippage was
    500% of price on a 0.1c contract and inflated notional 6x past the cap."""
    slip = (CONFIG.assumed_slippage_bps / 10_000.0) * ask
    fee = (CONFIG.taker_fee_bps / 10_000.0) * ask
    return min(0.999, ask + slip + fee)


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


def paper_fill(decision: Decision) -> Fill:
    CONFIG.assert_paper_only()
    sig = decision.signal
    fill_px = _taker_fill_price(sig.execution_price)
    # Hard cap: notional at the ALL-IN fill price may never exceed the position
    # limit, no matter what price the contracts were originally sized against.
    contracts = decision.contracts
    max_pos_usd = CONFIG.max_position_size * CONFIG.bankroll_usd
    if fill_px * contracts > max_pos_usd:
        contracts = round(max_pos_usd / fill_px, 4)
    notional = round(fill_px * contracts, 4)
    fill = Fill(
        timestamp=datetime.now(timezone.utc).isoformat(),
        asset=sig.asset,
        horizon=sig.horizon,
        slug=sig.slug,
        market_url=sig.market_url,
        condition_id=sig.condition_id,
        side=sig.side,
        synth_probability=sig.synth_probability,
        calibrated_probability=sig.calibrated_probability,
        intended_price=sig.execution_price,
        fill_price=fill_px,
        contracts=contracts,
        notional_usd=notional,
        raw_edge=sig.raw_edge,
        calibrated_edge=sig.calibrated_edge,
        net_edge=sig.net_edge,
        model_confidence=sig.model_confidence,
        expected_value_score=sig.expected_value_score,
        market_question=sig.market_question,
        mode="paper",
        entry_style=CONFIG.entry_style,
        event_start_time=sig.event_start_time,
        event_end_time=sig.event_end_time,
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
        fill = paper_fill(d)
        fills.append(fill)
    if fills:
        write_daily_report()
    log.info("Execution: %d fills written (paper)", len(fills))
    return fills


# --- Live trading stub ----------------------------------------------------

def place_live_limit_order(*_, **__) -> None:
    """Live trading is intentionally not implemented in this build."""
    raise NotImplementedError(
        "Live trading is disabled. Implement CLOB signing/order placement only "
        "after paper-trading and backtests confirm Synth is well-calibrated."
    )
