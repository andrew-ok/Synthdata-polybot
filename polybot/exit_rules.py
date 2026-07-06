"""Position-based paper exit rules."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .calibration import CalibrationObservation, append_observation
from .config import CONFIG
from .order_log import log_order_event
from .order_manager import cancel_order, create_exit_order, get_order_by_id, get_pending_orders
from .position_manager import Position, close_position, open_positions, update_position
from .signal_engine import Signal, taker_fee
from .trade_log import log_exit

from datetime import timedelta

log = logging.getLogger(__name__)


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


def _exit_cost(bid: float = 0.5) -> float:
    """Exit cost: taker fee at exit price plus slippage."""
    slip = CONFIG.assumed_slippage_bps / 10_000.0
    return taker_fee(bid) + slip


def _exit_fill_price(bid: float) -> float:
    return max(0.001, bid - _exit_cost(bid))


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
        "estimated_fee": round(position.contracts * taker_fee(decision.exit_bid), 6),
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
    """Return an exit reason for an already-open position, or None to hold.

    Strategy B: hold-to-resolution conviction betting. Positions ride to the
    window close unless Synth itself reverses. Market repricing toward Synth's
    probability is confirmation Synth was right — not a reason to exit early.

    Exit triggers (in priority order):
    1. STALE_DATA    — Synth dropped this market from its feed. Caller will
                       attempt Gamma resolution before using this reason.
    2. TIME_STOP     — near-resolution simulation: at < time_stop_seconds (30s),
                       the market price is a reliable proxy for the resolution
                       payout. Exit here to book PnL in paper mode.
    3. MODEL_REVERSAL — Synth now predicts the opposite side with genuine positive
                        net edge that exceeds our side's edge. Only fires when
                        opposite.net_edge > 0 (prevents spurious exits when both
                        sides are below threshold).

    SYNTH_EV_COLLAPSE is handled separately by evaluate_synth_updates(), which
    fires an emergency taker exit when Synth's updated probability drops below
    the position's avg entry price (EV goes negative).
    """
    if same_side is None:
        return "STALE_DATA"

    # TIME_STOP: paper-trading resolution simulation. At 30s to close the bid
    # is a reliable proxy for the binary payout — exit to book PnL.
    secs = same_side.seconds_to_event_end
    if secs is not None and secs < CONFIG.time_stop_seconds:
        return "TIME_STOP"

    # Model reversal: Synth now predicts the opposite side with genuine positive
    # net edge. Require opposite.net_edge > 0 so we never exit when both sides
    # are below the entry threshold (market repricing ≠ Synth reversal).
    if opposite is not None and opposite.net_edge > 0 and opposite.net_edge > same_side.net_edge:
        return "MODEL_REVERSAL"

    return None


# Hours past market_end_time before we force-close an unresolvable position.
_FORCE_EXIT_TIMEOUT_HOURS = 4.0


def _resolve_stale_position(position: Position) -> tuple:
    """Query Gamma for the final outcome of a position whose Synth data has gone stale.

    Returns (reason, bid, fill_px) where:
      - reason is "RESOLVED_WIN", "RESOLVED_LOSS", "FORCE_EXIT_TIMEOUT", or "STALE_DATA"
      - bid is the notional exit bid (1.0, 0.0, or position.latest_bid)
      - fill_px is the net fill price after fees

    Resolution logic:
      1. Ask Gamma for the market outcome by slug, with condition_id as fallback.
      2. If resolved: book WIN at $1.00/contract or LOSS at $0.00/contract (exact
         resolution payout — no taker fee applies to Polymarket redemptions).
      3. If unresolved but market_end_time + 4h has elapsed: force-close at $0.00
         (conservative — treats voided/disputed markets as a full loss).
      4. Otherwise: fall back to STALE_DATA with the last known bid (legacy behavior).
    """
    from .gamma_resolver import resolve_final_outcome_from_gamma

    outcome, source, detail = resolve_final_outcome_from_gamma(
        position.slug, condition_id=position.condition_id
    )

    if outcome is not None:
        position_won = (outcome == position.side)
        reason = "RESOLVED_WIN" if position_won else "RESOLVED_LOSS"
        bid = 1.0 if position_won else 0.0
        fill_px = 1.0 if position_won else 0.0
        log.info(
            "Resolution: %s %s/%s → %s via %s  fill=%.3f",
            position.position_id[:8], position.asset, position.side,
            reason, source, fill_px,
        )
        return reason, bid, fill_px

    # Gamma could not confirm resolution. Check for force-exit timeout.
    if position.market_end_time:
        try:
            end_dt = datetime.fromisoformat(position.market_end_time)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            hours_past = (datetime.now(timezone.utc) - end_dt).total_seconds() / 3600.0
            if hours_past >= _FORCE_EXIT_TIMEOUT_HOURS:
                log.warning(
                    "Force-exit timeout: %s %s/%s — %.1fh past market_end_time, "
                    "Gamma source=%s (%s). Closing at $0 (conservative).",
                    position.position_id[:8], position.asset, position.side,
                    hours_past, source, detail,
                )
                return "FORCE_EXIT_TIMEOUT", 0.0, 0.0
        except (TypeError, ValueError):
            pass

    # RL exit policy: if the market still has meaningful time remaining, hold instead
    # of exiting at a depressed stale bid. EV(hold) = p_win×(1−entry) > EV(stale_exit)
    # whenever p_win > entry, which is guaranteed by our entry gate.
    if position.market_end_time:
        try:
            end_dt = datetime.fromisoformat(position.market_end_time)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            secs_remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
            if secs_remaining > CONFIG.stale_hold_min_secs:
                log.info(
                    "STALE_HOLD: %s %s/%s — %.0fs remaining, holding (EV > stale exit)",
                    position.position_id[:8], position.asset, position.side, secs_remaining,
                )
                return None, None, None
        except (TypeError, ValueError):
            pass

    # Gamma unresolved and market expired (or no end time) — fall back to stale bid.
    bid = position.latest_bid
    fill_px = _exit_fill_price(bid)
    log.debug(
        "STALE_DATA fallback: %s %s/%s  gamma=%s (%s)  stale_bid=%.4f",
        position.position_id[:8], position.asset, position.side,
        source, detail, bid,
    )
    return "STALE_DATA", bid, fill_px


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

        # When STALE_DATA fires, Synth dropped this market from its feed.
        # _resolve_stale_position() queries Gamma first; if market hasn't resolved
        # and has time remaining, returns None (STALE_HOLD — keep the position).
        if reason == "STALE_DATA":
            stale_reason, stale_bid, stale_fill = _resolve_stale_position(position)
            if stale_reason is None:
                # RL hold: positive EV to ride out remaining window time.
                continue
            reason, bid, fill_px = stale_reason, stale_bid, stale_fill
            fair = position.latest_fair_probability
            score = position.latest_score
        else:
            bid = same_side.exit_price if same_side is not None else position.latest_bid
            fair = same_side.fair_probability if same_side is not None else position.latest_fair_probability
            score = same_side.score if same_side is not None else position.latest_score
            fill_px = _exit_fill_price(bid)

        closed = close_position(position, fill_px, reason)
        if closed is None:
            # Another scanner process closed this position first — skip all
            # follow-up logging so exit signals/fills/calibration obs aren't duplicated.
            continue
        decision = ExitDecision(
            timestamp=datetime.now(timezone.utc).isoformat(),
            position_id=position.position_id,
            event_key=position.event_key,
            side=position.side,
            reason=reason,
            fair_probability=fair,
            exit_bid=bid,
            exit_fill_price=fill_px,
            hold_edge=0.0 if reason in ("RESOLVED_WIN", "RESOLVED_LOSS", "FORCE_EXIT_TIMEOUT") else fair - bid - _exit_cost(bid),
            score=score,
            realized_pnl=closed.realized_pnl or 0.0,
        )
        _append_exit(decision)
        _append_fill(position, decision)
        log_exit(position=position, decision=decision, signal=same_side)
        decisions.append(decision)
        # Record live calibration observation for definitive resolution outcomes.
        # RESOLVED_WIN/RESOLVED_LOSS come from Gamma's actual binary payout — ground truth.
        # These feed the Calibrator and will replace the stale backtest data over time.
        if reason in ("RESOLVED_WIN", "RESOLVED_LOSS"):
            position_won = (reason == "RESOLVED_WIN")
            realized_outcome = position.side if position_won else (
                "DOWN" if position.side == "UP" else "UP"
            )
            # Compute entry_window_age_sec from slug timestamp (btc-updown-15m-<unix>).
            entry_window_age_sec = None
            try:
                parts = (position.slug or "").rsplit("-", 1)
                if len(parts) == 2:
                    market_start_ts = int(parts[1])
                    entry_dt = datetime.fromisoformat(position.entry_time)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    market_start_dt = datetime.fromtimestamp(market_start_ts, tz=timezone.utc)
                    entry_window_age_sec = (entry_dt - market_start_dt).total_seconds()
            except Exception:
                pass
            try:
                append_observation(CalibrationObservation(
                    timestamp=decision.timestamp,
                    asset=position.asset,
                    horizon=position.horizon,
                    predicted_probability=position.entry_raw_synth_probability,
                    realized_outcome=realized_outcome,
                    ask_price=position.entry_price,
                    side=position.side,
                    source="live",
                    entry_window_age_sec=entry_window_age_sec,
                ))
            except Exception as exc:
                log.warning("Failed to record live calibration obs: %s", exc)
    return decisions


# ---------------------------------------------------------------------------
# Scale-in strategy: Synth-update handler
# ---------------------------------------------------------------------------

def on_synth_update(position: Position, new_synth_p: float) -> str:
    """
    Called whenever Synth refreshes its probability for a position's asset.
    Updates the position's target and returns an action string:
      "hold"          — still +EV, do nothing
      "update_target" — still +EV but target price changed; update exit limit order
      "exit"          — no longer +EV; trigger emergency taker exit

    Handles both YES/NO (scale-in convention) and UP/DOWN (signal engine convention).
    YES and UP are equivalent (buying the up-leg token); NO and DOWN are equivalent.
    """
    position.synth_p_current = new_synth_p
    position.target_exit_price = new_synth_p

    if position.side in ("YES", "UP"):
        ev = new_synth_p - position.avg_entry_price
    else:  # NO or DOWN: fair value for the down leg is 1 - synth_p_up
        ev = (1 - new_synth_p) - position.avg_entry_price

    position.is_ev_positive = ev > CONFIG.min_ev_to_hold

    if not position.is_ev_positive:
        position.emergency_exit = True
        return "exit"

    # Only replace the resting exit order when target moves by more than 1¢ to avoid churn.
    if abs(position.target_exit_price - position.layers[-1].get("exit_order_price", 0)) > 0.01:
        return "update_target"

    return "hold"


def place_exit_order(position: Position) -> str:
    """
    Cancels any existing exit order for this position, then places a new
    post-only limit order to sell the full filled position at target_exit_price.
    Returns the new order_id.

    Total shares to sell = sum of shares across all filled layers.
    Accepts both YES/NO (scale-in convention) and UP/DOWN (signal-engine convention).
    Price: position.target_exit_price
    """
    if position.side not in ("YES", "NO", "UP", "DOWN"):
        raise ValueError(
            f"place_exit_order expects a YES/NO/UP/DOWN position, got side={position.side!r}"
        )
    if position.target_exit_price <= 0.0:
        raise ValueError(
            f"place_exit_order: invalid target_exit_price={position.target_exit_price} "
            f"for position {position.position_id}"
        )

    # Always cancel the old exit order before placing a new one to avoid duplicate fills.
    if position.exit_order_id:
        old = get_order_by_id(position.exit_order_id)
        if old is not None and old.status == "PENDING":
            done = cancel_order(old, "EXIT_ORDER_REPLACED")
            log_order_event("ORDER_CANCELLED", done, reason="EXIT_ORDER_REPLACED")

    total_shares = sum(
        float(layer.get("shares", 0))
        for layer in position.layers
        if layer.get("filled", False)
    )
    if total_shares <= 0:
        raise ValueError(
            f"place_exit_order: no filled shares for position {position.position_id}"
        )

    # GTD expiry: market resolution time minus 30 seconds.
    expires_at: Optional[str] = None
    if position.market_end_time:
        try:
            end_dt = datetime.fromisoformat(position.market_end_time)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            expires_at = (end_dt - timedelta(seconds=30)).isoformat()
        except (ValueError, TypeError):
            pass

    order = create_exit_order(
        position=position,
        shares=total_shares,
        limit_price=position.target_exit_price,
        expires_at=expires_at,
    )

    # Persist the new exit order_id back onto the position.
    update_position(position.position_id, exit_order_id=order.order_id)

    log.info(
        "Exit order PLACED: %s %s/%s/%s shares=%.4f limit=%.4f expiry=%s",
        order.order_id[:8], position.asset, position.side,
        position.event_key[:8], total_shares, position.target_exit_price, expires_at,
    )
    return order.order_id


def _cancel_position_orders(position: Position) -> int:
    """Cancel all PENDING entry orders associated with this position's condition_id."""
    cancelled = 0
    for order in get_pending_orders():
        if order.condition_id == position.condition_id:
            done = cancel_order(order, "SYNTH_EV_EXIT")
            log_order_event("ORDER_CANCELLED", done, reason="SYNTH_EV_EXIT")
            cancelled += 1
    return cancelled


def evaluate_synth_updates(signals: Iterable[Signal]) -> List[ExitDecision]:
    """
    Called every scan after fresh Synth data is fetched.
    Runs on_synth_update for each open scale-in position (side == "YES"/"NO")
    and applies the resulting action:
      - "exit":          cancel any pending entry orders; execute an immediate
                         taker (FAK) close at current bid minus fees.
      - "update_target": cancel the resting exit limit order and replace it with
                         a new post-only limit at target_exit_price.  In paper
                         mode this is persisted as a position-field update; the
                         next scan's evaluate_and_apply_exits will honour it.
      - "hold":          persist updated Synth probability; no order changes.

    Legacy UP/DOWN positions are handled by evaluate_and_apply_exits instead.
    """
    signal_map: Dict[tuple[str, str], Signal] = {(s.event_key, s.side): s for s in signals}
    exits: List[ExitDecision] = []

    for position in open_positions():
        # Positions must have at least one layer (always true for fills created by this bot).
        if not position.layers:
            continue
        # Both YES/NO (scale-in) and UP/DOWN (signal-engine) conventions are handled.
        # YES and UP are equivalent (up-leg token); NO and DOWN are equivalent.
        if position.side in ("YES", "UP"):
            signal_side = "UP"
        elif position.side in ("NO", "DOWN"):
            signal_side = "DOWN"
        else:
            continue
        sig = signal_map.get((position.event_key, signal_side))
        if sig is None:
            continue

        # on_synth_update expects Synth's UP probability. For DOWN/NO signals,
        # raw_synth_probability is the DOWN probability — convert to UP probability.
        synth_p_up = sig.raw_synth_probability if position.side in ("YES", "UP") else (1.0 - sig.raw_synth_probability)
        action = on_synth_update(position, synth_p_up)
        current_bid = sig.exit_price

        if action == "exit":
            n_cancelled = _cancel_position_orders(position)
            if n_cancelled:
                log.info(
                    "Cancelled %d pending order(s) for %s before emergency exit",
                    n_cancelled, position.position_id[:8],
                )
            fill_px = _exit_fill_price(current_bid)
            closed = close_position(position, fill_px, "SYNTH_EV_COLLAPSE")
            if closed is None:
                # Duplicate close from the other scanner process — skip logging.
                continue
            decision = ExitDecision(
                timestamp=datetime.now(timezone.utc).isoformat(),
                position_id=position.position_id,
                event_key=position.event_key,
                side=position.side,
                reason="SYNTH_EV_COLLAPSE",
                fair_probability=position.synth_p_current,
                exit_bid=current_bid,
                exit_fill_price=fill_px,
                hold_edge=position.synth_p_current - current_bid - _exit_cost(current_bid),
                score=position.latest_score,
                realized_pnl=closed.realized_pnl or 0.0,
            )
            _append_exit(decision)
            _append_fill(position, decision)
            log_exit(position=position, decision=decision, signal=sig)
            exits.append(decision)
            log.info(
                "Emergency taker exit: %s %s/%s fill=%.4f pnl=%.4f",
                position.position_id[:8], position.asset, position.side,
                fill_px, decision.realized_pnl,
            )

        elif action == "update_target":
            market_end_time = position.market_end_time
            if sig.seconds_to_event_end is not None:
                market_end_time = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=float(sig.seconds_to_event_end))
                ).isoformat()
            # UP/DOWN (Strategy B hold-to-resolution) positions don't use resting
            # exit orders — exits are handled by evaluate_and_apply_exits. Skip
            # place_exit_order to avoid wrong limit prices from the UP-probability
            # convention used by on_synth_update's target_exit_price field.
            if position.side in ("UP", "DOWN"):
                update_position(
                    position.position_id,
                    synth_p_current=position.synth_p_current,
                    is_ev_positive=position.is_ev_positive,
                    market_end_time=market_end_time,
                )
            else:
                # Scale-in YES/NO positions: stamp updated exit_order_price on the
                # last layer so on_synth_update's 1¢ churn guard sees the posted price.
                updated_layers = list(position.layers)
                updated_layers[-1] = {
                    **updated_layers[-1],
                    "exit_order_price": position.target_exit_price,
                }
                update_position(
                    position.position_id,
                    synth_p_current=position.synth_p_current,
                    target_exit_price=position.target_exit_price,
                    is_ev_positive=position.is_ev_positive,
                    layers=updated_layers,
                    market_end_time=market_end_time,
                )
                try:
                    place_exit_order(position)
                except Exception as exc:
                    log.warning(
                        "place_exit_order failed for %s: %s",
                        position.position_id[:8], exc,
                    )
            log.debug(
                "update_target: %s %s/%s synth_p_up=%.4f",
                position.position_id[:8], position.asset, position.side,
                position.synth_p_current,
            )

        else:  # "hold" — persist the refreshed Synth probability only
            market_end_time = position.market_end_time
            if sig.seconds_to_event_end is not None:
                market_end_time = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=float(sig.seconds_to_event_end))
                ).isoformat()
            update_position(
                position.position_id,
                synth_p_current=position.synth_p_current,
                target_exit_price=position.target_exit_price,
                is_ev_positive=position.is_ev_positive,
                market_end_time=market_end_time,
            )

    return exits
