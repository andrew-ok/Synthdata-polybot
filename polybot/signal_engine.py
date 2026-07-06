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

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .calibration import Calibrator, CalibrationResult
from .config import CONFIG
from .event_identity import event_key_for_opportunity

from .synth_client import Opportunity

log = logging.getLogger(__name__)


@dataclass
class Signal:
    asset: str
    horizon: str
    slug: str
    event_key: str
    market_url: str
    market_question: str          # synthesised from slug for the dashboard
    condition_id: str
    side: str                     # "UP" or "DOWN"
    raw_synth_probability: float
    fair_probability: float
    calibration_method: str
    calibration_sample_count: int
    confidence_score: float
    calibration_error_estimate: float
    entry_price: float
    exit_price: float
    synth_probability: float
    calibrated_probability: float
    execution_price: float
    counterparty_bid: float
    raw_edge: float
    calibrated_edge: float
    net_edge: float
    model_confidence: float
    expected_value_score: float
    score: float
    liquidity_score: float
    regime_score: float
    entry_cost: float
    spread: Optional[float]
    liquidity: float              # USD at top of relevant side
    hours_to_resolution: Optional[float]
    event_age_sec: Optional[float]
    seconds_to_event_end: Optional[float]
    threshold: float
    category: str = "crypto"
    reason: str = "insights"
    clob_source: str = "real_clob"
    is_stale: bool = False
    execution_mode: str = "taker"  # "maker" or "taker"


def taker_fee(price: float) -> float:
    """Crypto taker fee fraction at execution price (peaks at 1.80% at 50¢)."""
    return CONFIG.crypto_taker_fee_rate * price * (1.0 - price)


def _entry_cost(price: float, is_maker: bool = False) -> float:
    slip = CONFIG.assumed_slippage_bps / 10_000.0
    if is_maker:
        return slip  # zero taker fee; rebate paid separately by Polymarket
    return taker_fee(price) + slip


def _net(raw: float, price: float = 0.5, is_maker: bool = False) -> float:
    return raw - _entry_cost(price, is_maker=is_maker)


def _spread(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    return round(ask - bid, 4)


def _liquidity_score(liquidity: float) -> float:
    if CONFIG.min_liquidity <= 0:
        return 1.0
    return max(0.0, min(1.0, liquidity / (CONFIG.min_liquidity * 5.0)))


def _effective_threshold(asset: str, threshold: float) -> float:
    """Return per-asset minimum edge, enforcing a wider bar for thin-book markets."""
    if asset.upper() in CONFIG.thin_market_assets:
        return max(threshold, CONFIG.thin_market_min_edge)
    return threshold


def _question_from(opp: Opportunity) -> str:
    return f"{opp.asset} {opp.horizon} Up/Down - {opp.slug}"


def _is_stale(opp: Opportunity) -> bool:
    if opp.event_age_sec is not None and opp.event_age_sec < -1:
        return True
    # Historical backtests may not carry a CLOB fetch timestamp; do not mark
    # those stale unless real two-sided CLOB is explicitly required by live scan.
    return False


def _signal(
    opp: Opportunity,
    side: str,
    ask: float,
    bid: float,
    spread: Optional[float],
    liquidity: float,
    raw_probability: float,
    calibration: CalibrationResult,
    threshold: float,
    reason: str,
    clob_source: str,
    is_maker: bool = False,
) -> Optional[Signal]:
    if not (0 < ask < 1):
        return None
    if bid is not None and not (0 <= bid < 1):
        return None
    if spread is not None and spread > CONFIG.max_spread:
        return None
    entry_cost = _entry_cost(ask, is_maker=is_maker)
    net_ev = calibration.fair_probability - ask - entry_cost
    if net_ev < threshold:
        return None
    liq_score = _liquidity_score(liquidity)
    regime_score = 1.0
    confidence = calibration.confidence_score
    score = net_ev * confidence * liq_score * regime_score
    return Signal(
        asset=opp.asset,
        horizon=opp.horizon,
        slug=opp.slug,
        event_key=event_key_for_opportunity(opp),
        market_url=opp.polymarket_url,
        market_question=_question_from(opp),
        condition_id=opp.condition_id,
        side=side,
        raw_synth_probability=raw_probability,
        fair_probability=calibration.fair_probability,
        calibration_method=calibration.calibration_method,
        calibration_sample_count=calibration.sample_count,
        confidence_score=confidence,
        calibration_error_estimate=calibration.calibration_error_estimate,
        entry_price=ask,
        exit_price=bid,
        synth_probability=raw_probability,
        calibrated_probability=calibration.fair_probability,
        execution_price=ask,
        counterparty_bid=bid,
        raw_edge=raw_probability - ask,
        calibrated_edge=calibration.fair_probability - ask,
        net_edge=net_ev,
        model_confidence=confidence,
        expected_value_score=score,
        score=score,
        liquidity_score=liq_score,
        regime_score=regime_score,
        entry_cost=entry_cost,
        spread=spread,
        liquidity=liquidity,
        hours_to_resolution=opp.hours_to_event_end,
        event_age_sec=opp.event_age_sec,
        seconds_to_event_end=opp.seconds_to_event_end,
        threshold=threshold,
        reason=reason,
        clob_source=clob_source,
        is_stale=_is_stale(opp),
        execution_mode="maker" if is_maker else "taker",
    )


def _apply_fill_model(
    sig: Signal,
    fill_model: str,
    best_ask: Optional[float],
    maker_px: float,
) -> Optional[Signal]:
    """Return sig if the fill model allows a fill; None to discard the signal."""
    if fill_model == "optimistic":
        return sig
    if fill_model == "no_maker_fill":
        return None
    if fill_model == "touch":
        # Fill only if the current best ask is already at or below our limit
        # price — i.e., the market has traded through our order on this snapshot.
        if best_ask is None or best_ask > maker_px:
            return None
        return sig
    if fill_model == "next_tick":
        log.warning(
            "maker_fill_model=next_tick requires multi-tick data; "
            "falling back to optimistic fill assumption"
        )
        return sig
    if fill_model == "order_lifecycle":
        # Signal passes through; actual fill is deferred to order_lifecycle.py
        return sig
    log.warning("Unknown maker_fill_model=%r; defaulting to optimistic", fill_model)
    return sig



def evaluate(
    opportunities: Iterable[Opportunity],
    threshold: Optional[float] = None,
    calibrator: Optional[Calibrator] = None,
) -> List[Signal]:
    base_thr = CONFIG.min_entry_edge if threshold is None else threshold
    calibrator = calibrator or Calibrator()
    is_maker = CONFIG.execution_mode == "maker"
    maker_offset = CONFIG.maker_entry_offset  # already in decimal price units (0.001 = 0.1¢)
    fill_model = CONFIG.maker_fill_model if is_maker else "optimistic"
    signals: List[Signal] = []

    for opp in opportunities:
        if calibrator.segment_disabled(opp.asset, opp.horizon):
            continue
        if not event_key_for_opportunity(opp):
            continue
        thr = _effective_threshold(opp.asset, base_thr)

        yes_ask_check = opp.yes_ask_price if opp.yes_ask_price is not None else opp.best_ask_price
        if yes_ask_check is not None and opp.no_ask_price is not None:
            ask_sum = yes_ask_check + opp.no_ask_price
            if ask_sum < 0.90 or ask_sum > 1.15:
                continue

        # --- Up leg ---
        if "UP" in CONFIG.scan_sides:
            yes_bid = opp.yes_bid_price if opp.yes_bid_price is not None else opp.best_bid_price
            yes_ask = opp.yes_ask_price if opp.yes_ask_price is not None else opp.best_ask_price
            yes_spread = _spread(yes_bid, yes_ask)
            yes_liq = (opp.yes_ask_size or opp.best_ask_size) * (yes_ask or 0.0)
            if yes_bid is not None and (yes_ask is not None or is_maker):
                cal = calibrator.calibrate_side(opp.asset, opp.horizon, "UP", opp.synth_probability_up)
                if is_maker and yes_bid is not None:
                    # Post limit order just above best bid; cap below ask to remain non-crossing.
                    maker_px = yes_bid + maker_offset
                    if yes_ask is not None:
                        maker_px = min(maker_px, yes_ask - 0.001)
                    maker_px = round(min(0.999, max(0.001, maker_px)), 4)
                    sig = _signal(opp, "UP", maker_px, yes_bid, yes_spread, yes_liq,
                                  opp.synth_probability_up, cal, thr, "maker_yes_clob", "real_clob",
                                  is_maker=True)
                    if sig:
                        sig = _apply_fill_model(sig, fill_model, yes_ask, maker_px)
                else:
                    sig = _signal(opp, "UP", yes_ask, yes_bid, yes_spread, yes_liq,
                                  opp.synth_probability_up, cal, thr, "real_yes_clob", "real_clob")
                if sig:
                    signals.append(sig)

        # --- Down leg (real CLOB book if available; otherwise complementary book) ---
        if "DOWN" not in CONFIG.scan_sides:
            continue
        if opp.no_ask_price is not None:
            ask_down = opp.no_ask_price
            bid_down = opp.no_bid_price or 0.0
            spread_down = _spread(opp.no_bid_price, opp.no_ask_price)
            liquidity_down = opp.no_ask_size * ask_down
            reason_down = "real_down_clob"
            clob_source_down = "real_clob"
        elif CONFIG.allow_complementary_book_fallback and opp.best_bid_price is not None:
            implied_down_ask = max(0.0, min(1.0, 1.0 - opp.best_bid_price))
            ask_down = implied_down_ask
            bid_down = max(0.0, 1.0 - (opp.best_ask_price or 1.0))
            spread_down = _spread(opp.best_bid_price, opp.best_ask_price)
            liquidity_down = opp.best_bid_size * opp.best_bid_price
            reason_down = "complementary_book"
            clob_source_down = "complementary_book"
        else:
            ask_down = None
            bid_down = 0.0
            spread_down = None
            liquidity_down = 0.0
            reason_down = ""
            clob_source_down = ""

        if ask_down is not None:
            synth_down = opp.synth_probability_down
            cal = calibrator.calibrate_side(opp.asset, opp.horizon, "DOWN", synth_down)
            if is_maker and bid_down > 0:
                maker_px_down = bid_down + maker_offset
                maker_px_down = min(maker_px_down, ask_down - 0.001)
                maker_px_down = round(min(0.999, max(0.001, maker_px_down)), 4)
                sig = _signal(opp, "DOWN", maker_px_down, bid_down, spread_down, liquidity_down,
                              synth_down, cal, thr, "maker_down_clob", clob_source_down,
                              is_maker=True)
                if sig:
                    sig = _apply_fill_model(sig, fill_model, opp.no_ask_price, maker_px_down)
            else:
                sig = _signal(opp, "DOWN", ask_down, bid_down, spread_down, liquidity_down,
                              synth_down, cal, thr, reason_down, clob_source_down)
            if sig:
                signals.append(sig)

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
