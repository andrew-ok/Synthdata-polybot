"""Signal engine.

Consumes Opportunity records from SynthInsightsClient (each has Synth's prob
AND the live Polymarket book in one shot) and emits Signals when edge
exceeds the configured raw threshold.

Important nuance: Synth's insights endpoint exposes a single best_bid /
best_ask. Per their docs and observed payloads, those quotes are for the
"Up" leg of the underlying binary. When optional Polymarket CLOB enrichment
has populated the real Down-token book, we use that first. Otherwise:

  - Up edge  = synth_probability_up    − best_ask_price
  - Down edge ≈ synth_probability_down − (1 − best_bid_price)

The Down edge uses the *complementary book*: buying NO at price p is
economically equivalent to selling YES at (1 − p), so 1 − best_bid_price is
the cheapest implied executable price for the Down leg from this endpoint.
The fallback is an approximation — it ignores Down-token liquidity that is
tighter than 1 − up_bid. For historical cached backtests where token books are
not available, it is still the conservative fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from .calibration import Calibrator
from .config import CONFIG
from .synth_client import Opportunity


@dataclass
class Signal:
    asset: str
    horizon: str
    slug: str
    market_url: str
    market_question: str          # synthesised from slug for the dashboard
    condition_id: str             # we use the slug as the dedupe / corr key
    side: str                     # "UP" or "DOWN"
    synth_probability: float
    calibrated_probability: float
    execution_price: float
    counterparty_bid: float
    raw_edge: float
    calibrated_edge: float
    net_edge: float
    model_confidence: float
    expected_value_score: float
    spread: Optional[float]
    liquidity: float              # USD at top of relevant side
    hours_to_resolution: Optional[float]
    event_age_sec: Optional[float]
    seconds_to_event_end: Optional[float]
    threshold: float
    category: str = "crypto"
    reason: str = "insights"


def _net(raw: float) -> float:
    fee = CONFIG.taker_fee_bps / 10_000.0
    slip = CONFIG.assumed_slippage_bps / 10_000.0
    return raw - fee - slip


def _spread(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    return round(ask - bid, 4)


def _liquidity_score(liquidity: float) -> float:
    if CONFIG.min_liquidity <= 0:
        return 1.0
    return max(0.0, min(1.0, liquidity / (CONFIG.min_liquidity * 5.0)))


def _question_from(opp: Opportunity) -> str:
    return f"{opp.asset} {opp.horizon} Up/Down — {opp.slug}"


def evaluate(
    opportunities: Iterable[Opportunity],
    threshold: Optional[float] = None,
    calibrator: Optional[Calibrator] = None,
) -> List[Signal]:
    thr = CONFIG.min_edge_threshold if threshold is None else threshold
    calibrator = calibrator or Calibrator()
    signals: List[Signal] = []

    for opp in opportunities:
        if calibrator.segment_disabled(opp.asset, opp.horizon):
            continue
        model_confidence = calibrator.model_confidence(opp.asset, opp.horizon)
        spread = _spread(opp.best_bid_price, opp.best_ask_price)
        htr = opp.hours_to_event_end
        event_age_sec = opp.event_age_sec
        seconds_to_event_end = opp.seconds_to_event_end

        # --- Up leg ---
        if opp.best_ask_price is not None:
            ask_up = opp.best_ask_price
            synth_up = opp.synth_probability_up
            cal_up = calibrator.calibrate(opp.asset, opp.horizon, synth_up)
            edge_up = cal_up - ask_up
            liquidity_up = opp.best_ask_size * ask_up
            if edge_up >= thr:
                signals.append(Signal(
                    asset=opp.asset, horizon=opp.horizon, slug=opp.slug,
                    market_url=opp.polymarket_url,
                    market_question=_question_from(opp),
                    condition_id=opp.slug,
                    side="UP",
                    synth_probability=synth_up,
                    calibrated_probability=cal_up,
                    execution_price=ask_up,
                    counterparty_bid=opp.best_bid_price or 0.0,
                    raw_edge=edge_up,
                    calibrated_edge=edge_up,
                    net_edge=_net(edge_up),
                    model_confidence=model_confidence,
                    expected_value_score=edge_up * _liquidity_score(liquidity_up) * model_confidence,
                    spread=spread,
                    liquidity=liquidity_up,
                    hours_to_resolution=htr,
                    event_age_sec=event_age_sec,
                    seconds_to_event_end=seconds_to_event_end,
                    threshold=thr,
                ))

        # --- Down leg (real CLOB book if available; otherwise complementary book) ---
        if opp.no_ask_price is not None:
            ask_down = opp.no_ask_price
            bid_down = opp.no_bid_price or 0.0
            spread_down = _spread(opp.no_bid_price, opp.no_ask_price)
            liquidity_down = opp.no_ask_size * ask_down
            reason = "real_down_clob"
        elif opp.best_bid_price is not None:
            implied_down_ask = max(0.0, min(1.0, 1.0 - opp.best_bid_price))
            ask_down = implied_down_ask
            bid_down = max(0.0, 1.0 - (opp.best_ask_price or 1.0))
            spread_down = spread
            liquidity_down = opp.best_bid_size * opp.best_bid_price
            reason = "complementary_book"
        else:
            ask_down = None

        if ask_down is not None:
            synth_down = opp.synth_probability_down
            cal_up_for_down = calibrator.calibrate(opp.asset, opp.horizon, opp.synth_probability_up)
            cal_down = 1.0 - cal_up_for_down
            edge_down = cal_down - ask_down
            if edge_down >= thr and 0 < ask_down < 1:
                signals.append(Signal(
                    asset=opp.asset, horizon=opp.horizon, slug=opp.slug,
                    market_url=opp.polymarket_url,
                    market_question=_question_from(opp),
                    condition_id=opp.slug,
                    side="DOWN",
                    synth_probability=synth_down,
                    calibrated_probability=cal_down,
                    execution_price=ask_down,
                    counterparty_bid=bid_down,
                    raw_edge=edge_down,
                    calibrated_edge=edge_down,
                    net_edge=_net(edge_down),
                    model_confidence=model_confidence,
                    expected_value_score=edge_down * _liquidity_score(liquidity_down) * model_confidence,
                    spread=spread_down,
                    liquidity=liquidity_down,
                    hours_to_resolution=htr,
                    event_age_sec=event_age_sec,
                    seconds_to_event_end=seconds_to_event_end,
                    threshold=thr,
                    reason=reason,
                ))

    signals.sort(key=lambda s: s.expected_value_score, reverse=True)
    return signals
