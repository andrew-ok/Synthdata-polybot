"""Evaluate pending maker orders against current market data each scan."""
from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .config import CONFIG
from .order_log import log_order_event
from .order_manager import PendingOrder, cancel_order, fill_order, get_pending_orders
from .position_manager import close_position, has_open_event, load_positions, open_position_from_fill
from .signal_engine import Signal, taker_fee

log = logging.getLogger(__name__)


def _fill_price_for_order(order: PendingOrder, current_ask: float) -> float:
    """Maker fills at our limit price (not the current ask)."""
    return min(0.999, order.limit_price)


def _close_position_from_exit_order(order: PendingOrder, sig: Signal, fill_px: float) -> None:
    """Close the position when its post-only exit limit order fills and log the trade."""
    if not order.position_id:
        log.warning("Exit order %s has no position_id; cannot close", order.order_id[:8])
        return

    open_pos = [p for p in load_positions("open") if p.position_id == order.position_id]
    if not open_pos:
        log.warning(
            "Exit fill: no open position for position_id=%s (already closed?)",
            order.position_id,
        )
        return
    position = open_pos[0]

    total_shares = sum(
        float(layer.get("shares", 0))
        for layer in position.layers
        if layer.get("filled", False)
    )
    gross_pnl = round((fill_px - position.avg_entry_price) * total_shares, 4)

    close_position(position, fill_px, "EXIT_ORDER_FILLED")

    log.info(
        "EXIT FILL | asset=%s side=%s avg_entry=%.4f exit_price=%.4f "
        "total_shares=%.4f gross_pnl=%.4f synth_p_at_entry=%.4f synth_p_at_exit=%.4f",
        position.asset, position.side,
        position.avg_entry_price, fill_px,
        total_shares, gross_pnl,
        position.synth_p_at_entry, position.synth_p_current,
    )


def evaluate_pending_orders(
    signal_map: Dict[Tuple[str, str], Signal],
) -> Tuple[List[PendingOrder], List[PendingOrder]]:
    """Evaluate all PENDING orders against the current scan's signal map.

    Returns (filled_orders, cancelled_orders).
    A pending order fills when best_ask <= limit_price (market offered at or
    below our resting bid). It is cancelled when edge decays, time runs out,
    data is stale, or a position is already open.
    """
    filled: List[PendingOrder] = []
    cancelled: List[PendingOrder] = []

    for order in get_pending_orders():
        # --- EXIT orders: post-only sell limit (YES/NO scale-in positions) ---
        if getattr(order, "order_type", "ENTRY") == "EXIT":
            # Map YES/NO side to the UP/DOWN signal_map keys.
            sig_side = "UP" if order.side == "YES" else ("DOWN" if order.side == "NO" else order.side)
            sig = signal_map.get((order.event_key, sig_side))

            # GTD expiry: cancel if past the resting deadline.
            if order.expires_at:
                try:
                    exp_dt = datetime.fromisoformat(order.expires_at)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) >= exp_dt:
                        c = cancel_order(order, "EXPIRED")
                        log_order_event("ORDER_CANCELLED", c, reason="EXPIRED")
                        cancelled.append(c)
                        continue
                except (ValueError, TypeError):
                    pass

            if sig is None:
                continue  # no market data — keep the order resting

            current_bid = sig.exit_price
            current_ask = sig.entry_price
            secs = sig.seconds_to_event_end

            # Cancel if market has resolved (would fill at 0 or 1 at settlement, not our limit).
            resolved = getattr(sig, "resolved_outcome", None)
            if resolved in ("UP", "DOWN"):
                c = cancel_order(order, "MARKET_RESOLVED")
                log_order_event(
                    "ORDER_CANCELLED", c,
                    current_bid=current_bid, current_ask=current_ask,
                    time_to_resolution=secs,
                    reason="MARKET_RESOLVED",
                )
                cancelled.append(c)
                continue

            # Maker sell fills when best_bid >= our limit price.
            if current_bid >= order.limit_price:
                fill_px = min(0.999, order.limit_price)
                filled_order = fill_order(order, fill_px, best_bid=current_bid, best_ask=current_ask)
                log_order_event(
                    "ORDER_FILLED", filled_order,
                    current_bid=current_bid, current_ask=current_ask,
                    current_synth_prob=sig.raw_synth_probability,
                    current_calibrated_prob=sig.fair_probability,
                    current_edge=sig.net_edge,
                    current_liquidity=sig.liquidity,
                    time_to_resolution=secs,
                    reason=f"best_bid {current_bid:.4f} >= exit_limit {order.limit_price:.4f}",
                )
                _close_position_from_exit_order(filled_order, sig, fill_px)
                filled.append(filled_order)
                log.info(
                    "Exit order FILLED: %s %s/%s/%s fill=%.4f",
                    filled_order.order_id[:8], order.asset, order.horizon, order.side, fill_px,
                )

            continue  # don't fall through to ENTRY logic

        # --- ENTRY orders ---
        sig = signal_map.get((order.event_key, order.side))

        # --- Guard: position already open for this event ---
        if has_open_event(order.event_key):
            c = cancel_order(order, "POSITION_ALREADY_OPEN")
            log_order_event(
                "ORDER_CANCELLED", c,
                reason="POSITION_ALREADY_OPEN",
                time_to_resolution=sig.seconds_to_event_end if sig else None,
            )
            cancelled.append(c)
            continue

        # --- No current market data ---
        if sig is None:
            c = cancel_order(order, "STALE_CLOB")
            log_order_event("ORDER_CANCELLED", c, reason="STALE_CLOB")
            cancelled.append(c)
            continue

        current_ask = sig.entry_price          # best ask for our side
        current_bid = sig.exit_price           # best bid for our side
        secs = sig.seconds_to_event_end
        current_edge = sig.net_edge
        current_liq = sig.liquidity

        # --- Near close: cancel unfilled entry order ---
        if secs is not None and secs < CONFIG.cancel_quotes_before_close_seconds:
            c = cancel_order(order, "NEAR_CLOSE")
            log_order_event(
                "ORDER_CANCELLED", c,
                current_bid=current_bid, current_ask=current_ask,
                current_synth_prob=sig.raw_synth_probability,
                current_calibrated_prob=sig.fair_probability,
                current_edge=current_edge,
                current_liquidity=current_liq,
                time_to_resolution=secs,
                reason="NEAR_CLOSE",
            )
            cancelled.append(c)
            continue

        # --- Stale Synth data ---
        if sig.is_stale:
            c = cancel_order(order, "STALE_SYNTH")
            log_order_event(
                "ORDER_CANCELLED", c,
                current_bid=current_bid, current_ask=current_ask,
                time_to_resolution=secs,
                reason="STALE_SYNTH",
            )
            cancelled.append(c)
            continue

        # --- Illiquid ---
        if current_liq < CONFIG.min_liquidity:
            c = cancel_order(order, "ILLIQUID")
            log_order_event(
                "ORDER_CANCELLED", c,
                current_bid=current_bid, current_ask=current_ask,
                current_liquidity=current_liq,
                time_to_resolution=secs,
                reason="ILLIQUID",
            )
            cancelled.append(c)
            continue

        # --- Edge decay: original thesis no longer valid ---
        if current_edge < CONFIG.min_entry_edge:
            c = cancel_order(order, "EDGE_DECAY")
            log_order_event(
                "ORDER_CANCELLED", c,
                current_bid=current_bid, current_ask=current_ask,
                current_synth_prob=sig.raw_synth_probability,
                current_calibrated_prob=sig.fair_probability,
                current_edge=current_edge,
                time_to_resolution=secs,
                reason="EDGE_DECAY",
            )
            cancelled.append(c)
            continue

        # --- Market resolved ---
        resolved = getattr(sig, "resolved_outcome", None)
        if resolved in ("UP", "DOWN"):
            c = cancel_order(order, "MARKET_RESOLVED")
            log_order_event(
                "ORDER_CANCELLED", c,
                current_bid=current_bid, current_ask=current_ask,
                time_to_resolution=secs,
                reason="MARKET_RESOLVED",
            )
            cancelled.append(c)
            continue

        # --- Fill check: maker buy fills if best_ask <= limit_price ---
        if current_ask <= order.limit_price:
            fill_px = _fill_price_for_order(order, current_ask)
            filled_order = fill_order(order, fill_px, best_bid=current_bid, best_ask=current_ask)
            log_order_event(
                "ORDER_FILLED", filled_order,
                current_bid=current_bid, current_ask=current_ask,
                current_synth_prob=sig.raw_synth_probability,
                current_calibrated_prob=sig.fair_probability,
                current_edge=current_edge,
                current_liquidity=current_liq,
                time_to_resolution=secs,
                reason=f"best_ask {current_ask:.4f} <= limit {order.limit_price:.4f}",
            )

            # Create the position from this fill
            pos_id = str(uuid.uuid4())
            _create_position_from_order(filled_order, sig, fill_px, pos_id)
            filled.append(filled_order)
            log.info(
                "Order FILLED: %s %s/%s/%s fill=%.4f",
                filled_order.order_id[:8], order.asset, order.horizon, order.side, fill_px,
            )
        # else: still PENDING — order rests in book, no action

    return filled, cancelled


def _create_position_from_order(
    order: PendingOrder,
    sig: Signal,
    fill_px: float,
    pos_id: str,
) -> None:
    """Write a synthetic Fill record and open a position from a filled order."""
    from .execution import Fill

    notional = round(fill_px * order.contracts, 4)
    now = datetime.now(timezone.utc).isoformat()

    fill = Fill(
        timestamp=now,
        asset=order.asset,
        horizon=order.horizon,
        slug=sig.slug,
        event_key=order.event_key,
        market_url=sig.market_url,
        condition_id=order.condition_id,
        side=order.side,
        synth_probability=order.synth_prob_at_order,
        calibrated_probability=order.calibrated_prob_at_order,
        intended_price=order.limit_price,
        fill_price=fill_px,
        estimated_slippage=0.0,
        estimated_fee=0.0,  # maker: no taker fee
        contracts=order.contracts,
        notional_usd=notional,
        raw_edge=sig.raw_edge,
        calibrated_edge=sig.calibrated_edge,
        net_edge=order.edge_at_order,
        model_confidence=sig.model_confidence,
        expected_value_score=sig.expected_value_score,
        confidence_score=sig.confidence_score,
        liquidity_score=sig.liquidity_score,
        regime_score=sig.regime_score,
        best_bid=sig.exit_price,
        best_ask=sig.entry_price,
        spread=sig.spread,
        entry_or_exit="entry",
        entry_reason=f"order_lifecycle order_id={order.order_id}",
        exit_reason=None,
        market_question=sig.market_question,
        mode="paper",
        execution_mode="maker",
        maker_fill_optimistic=False,  # "touch" fill: ask actually reached our limit
        position_id=pos_id,
    )

    os.makedirs(CONFIG.log_dir, exist_ok=True)
    fills_path = os.path.join(CONFIG.log_dir, "fills.jsonl")
    with open(fills_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(fill), sort_keys=True) + "\n")

    csv_path = os.path.join(CONFIG.log_dir, "fills.csv")
    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(fill).keys()))
        if new_file:
            w.writeheader()
        w.writerow(asdict(fill))

    position = open_position_from_fill(fill, sig, position_id=pos_id)

    # For scale-in YES/NO positions, immediately place the resting exit limit order.
    if position.side in ("YES", "NO"):
        try:
            from .exit_rules import place_exit_order
            place_exit_order(position)
        except Exception as exc:
            log.warning(
                "place_exit_order failed after fill for %s: %s", pos_id[:8], exc
            )
