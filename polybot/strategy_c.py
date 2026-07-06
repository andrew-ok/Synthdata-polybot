"""Strategy C — the Synth-X-post "late-window convergence" strategy.

Distinct trade population from A/B (so it CANNOT be an overlay on A's fills):

  - HOURLY contracts only (the post trades Polymarket hourly up/down)
  - entered in the LAST ~20 minutes of the window (converging but not yet
    fully repriced — "which 85c contracts are actually worth 92c")
  - lower ~10% edge threshold (vs A's 20c)
  - Kelly sizing on the edge

It generates and fills its OWN signals into a separate ledger (fills_C.jsonl),
settled by the same settlement pass. The scanner wraps run_c() in try/except
so a fault here can never touch the live A path.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from .config import CONFIG
from .signal_engine import evaluate, Signal
from .risk_manager import _kelly_size, _correlation_key
from .execution import _taker_fill_price
from .settlement import settle_positions
from .synth_client import Opportunity, SynthInsightsClient

log = logging.getLogger(__name__)

C_LEDGER = "fills_C.jsonl"   # legacy single-C ledger; still settled until empty

# --- Live C variants (each = own ledger, own book). Rules take (price, edge).
# Chosen from the 8-week per-week replay: RAMP (best cum$, curve-supported) plus
# the two strongest 5c buckets (6/8 weeks positive each).
C_VARIANTS = [
    ("C_RAMP",  "fills_C_ramp.jsonl",
     lambda p, e: 0.70 <= p <= 0.92 and e >= 0.015 + 0.22 * (p - 0.70)),
    ("C_B7075", "fills_C_b7075.jsonl",
     lambda p, e: 0.70 <= p < 0.75 and e >= 0.015),
    ("C_B8085", "fills_C_b8085.jsonl",
     lambda p, e: 0.80 <= p < 0.85 and e >= 0.03),
    # G2 SYNTH-VETO (strategy_gen, 8wk: +$665, 6/8 wks, 492 trades): buy ANY
    # late favorite unless Synth disagrees by >2pp. Synth as veto, not trigger —
    # pure-favorite with no Synth was -$125, so the veto IS the edge.
    ("C_VETO",  "fills_C_veto.jsonl",
     lambda p, e: 0.70 <= p <= 0.92 and e >= -0.02),
    # G7 RAMP+PAUSE (8wk: +$529, 6/8 wks): RAMP entries, but run_c stands this
    # book down for 24h after 3 resolved losses within 24h (loss clustering).
    ("C_RAMPP", "fills_C_rampp.jsonl",
     lambda p, e: 0.70 <= p <= 0.92 and e >= 0.015 + 0.22 * (p - 0.70)),
]
PAUSED_VARIANTS = {"C_RAMPP": (3, 24.0)}      # ledger -> (losses, window hours)
C_CANDIDATE_MIN_EDGE = -0.02  # admits veto-range candidates; variants tighten

# All tunable via env so C can be re-shaped without code changes.
C_MIN_EDGE = float(os.environ.get("C_MIN_EDGE", "0.07"))
C_MAX_SEC_TO_END = float(os.environ.get("C_MAX_SEC_TO_END", "1200"))  # last 20 min
C_MIN_SEC_TO_END = float(os.environ.get("C_MIN_SEC_TO_END", "120"))   # not the final 2 min
C_HORIZONS = {h.strip().upper() for h in os.environ.get("C_HORIZONS", "1H").split(",") if h.strip()}
# FAITHFUL to the Synth X post: only buy the HIGH-probability converging side
# ("which 85c contracts are actually worth 92c") — the favorite's ask must be
# in this band AND Synth must say that same side is worth >= ask + C_MIN_EDGE.
C_FAVORITE_MIN_PRICE = float(os.environ.get("C_FAVORITE_MIN_PRICE", "0.70"))
C_FAVORITE_MAX_PRICE = float(os.environ.get("C_FAVORITE_MAX_PRICE", "0.95"))
# C-specific forecast-age limit (default 900s vs the global 600s). Rationale:
# the global gate guards against stale-forecast FAKE edges after a price move,
# but C's favorite-agreement structure already self-protects — if the market
# flipped after the forecast, stale Synth now DISAGREES with the new favorite
# and no trade fires. At :47 scans Synth's forecast is often 10-14 min old, so
# 600s would silently kill many legitimate C entries.
C_MAX_FORECAST_AGE_SEC = float(os.environ.get("C_MAX_FORECAST_AGE_SEC", "900"))


def _ledger_path(name: str = C_LEDGER) -> str:
    return os.path.join(CONFIG.log_dir, name)


def _opened_condition_ids(ledger: str = C_LEDGER) -> Set[str]:
    path = _ledger_path(ledger)
    opened: Set[str] = set()
    if not os.path.exists(path):
        return opened
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = str(row.get("condition_id") or "").strip()
                if cid:
                    opened.add(cid)
    except OSError:
        pass
    return opened


def _wall_clock_sec_to_end(sig: Signal) -> Optional[float]:
    if not sig.event_end_time:
        return None
    try:
        end = datetime.fromisoformat(sig.event_end_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).total_seconds()


def _log_c_skip(sig: Signal, reason: str) -> None:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    path = os.path.join(CONFIG.log_dir, "skipped_C.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "condition_id": sig.condition_id, "side": sig.side,
            "price": sig.execution_price, "raw_edge": round(sig.raw_edge, 4),
            "liquidity": round(sig.liquidity, 2),
            "forecast_age_sec": sig.forecast_age_sec,
            "reason": reason,
        }, sort_keys=True) + "\n")


def evaluate_c(opps: List[Opportunity]) -> List[Signal]:
    """Shared C candidates: hourly, late-window, favorite side (wide 0.70-0.92
    band, edge >= C_CANDIDATE_MIN_EDGE), liquidity + forecast-age gates.
    Variant rules (price/edge) are applied per-ledger in run_c. Every rejection
    is logged to skipped_C.jsonl with a reason."""
    out: List[Signal] = []
    seen_corr: Set[str] = set()

    # The post's strategy uses RAW Synth probabilities. A live Calibrator loads
    # whatever is in the calibration DB and (a) de-biases strong favorites
    # toward coin flips and (b) can disable whole segments — either one makes C
    # untradeable. Identity calibrator = faithful C.
    from .calibration import Calibrator
    raw_cal = Calibrator(observations=[])

    for sig in evaluate(opps, threshold=C_CANDIDATE_MIN_EDGE, calibrator=raw_cal):
        if sig.horizon.upper() not in C_HORIZONS:
            continue  # 15m signals are out of scope by definition — not logged
        # late-window: measured on OUR clock (payload clocks can be stale)
        sec_to_end = _wall_clock_sec_to_end(sig)
        if sec_to_end is None or not (C_MIN_SEC_TO_END <= sec_to_end <= C_MAX_SEC_TO_END):
            _log_c_skip(sig, f"outside late window ({'?' if sec_to_end is None else int(sec_to_end)}s to end)")
            continue
        # FAITHFUL-C filter: only the high-probability (favorite) side; the
        # variant rules tighten price/edge within this envelope.
        if not (0.70 <= sig.execution_price <= 0.92):
            _log_c_skip(sig, f"not favorite band ({sig.execution_price:.3f} outside [0.70,0.92])")
            continue
        if sig.liquidity < CONFIG.min_liquidity:
            _log_c_skip(sig, f"liquidity ${sig.liquidity:.0f} < ${CONFIG.min_liquidity:.0f}")
            continue
        if (C_MAX_FORECAST_AGE_SEC > 0 and sig.forecast_age_sec is not None
                and sig.forecast_age_sec > C_MAX_FORECAST_AGE_SEC):
            _log_c_skip(sig, f"forecast {sig.forecast_age_sec:.0f}s old > {C_MAX_FORECAST_AGE_SEC:.0f}s")
            continue
        corr = _correlation_key(sig.market_question)
        if corr and corr in seen_corr:
            _log_c_skip(sig, f"correlated with prior pick '{corr}'")
            continue
        out.append(sig)
        if corr:
            seen_corr.add(corr)
    return out


def _fill_c(sig: Signal, flat: bool = False) -> Dict:
    """Size and paper-fill a C signal into a ledger row that settlement.py and
    the report understand. flat=True uses the position cap directly — required
    for veto-style books whose entries can have negative SYNTH edge (Kelly on
    Synth's prob would zero them out, but the mechanism prices off the MARKET
    probability; the backtest that validated G2 used flat stakes)."""
    if flat:
        frac = CONFIG.max_position_size
    else:
        frac = _kelly_size(sig.calibrated_probability, sig.execution_price, CONFIG.kelly_fraction)
        frac = min(CONFIG.max_position_size, frac)
    confidence = min(CONFIG.max_confidence_position_multiplier,
                     max(CONFIG.model_confidence_floor, sig.model_confidence))
    frac = min(CONFIG.max_position_size, frac * confidence)
    size_usd = round(CONFIG.bankroll_usd * frac, 2)

    fill_px = _taker_fill_price(sig.execution_price)
    if fill_px <= 0 or fill_px >= 1:
        return {}
    contracts = round(size_usd / fill_px, 4)
    max_pos_usd = CONFIG.max_position_size * CONFIG.bankroll_usd
    if fill_px * contracts > max_pos_usd:
        contracts = round(max_pos_usd / fill_px, 4)
    if contracts <= 0:
        return {}
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": "C",
        "asset": sig.asset,
        "horizon": sig.horizon,
        "slug": sig.slug,
        "market_url": sig.market_url,
        "condition_id": sig.condition_id,
        "side": sig.side,
        "synth_probability": sig.synth_probability,
        "calibrated_probability": sig.calibrated_probability,
        "intended_price": sig.execution_price,
        "fill_price": round(fill_px, 6),
        "contracts": contracts,
        "notional_usd": round(fill_px * contracts, 4),
        "raw_edge": sig.raw_edge,
        "calibrated_edge": sig.calibrated_edge,
        "net_edge": sig.net_edge,
        "model_confidence": sig.model_confidence,
        "market_question": sig.market_question,
        "mode": "paper",
        "entry_style": "late_window",
        "event_start_time": sig.event_start_time,
        "event_end_time": sig.event_end_time,
    }


def _loss_streak_paused(ledger: str, max_losses: int, window_hours: float) -> bool:
    """True if the ledger has >= max_losses resolved losses within the trailing
    window — the G7 stand-down: losses cluster in unfavorable regimes."""
    path = _ledger_path(ledger)
    if not os.path.exists(path):
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    losses = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pnl = row.get("realized_pnl")
                ts = row.get("resolution_timestamp") or row.get("exit_timestamp")
                if pnl is None or not ts:
                    continue
                try:
                    when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when >= cutoff and float(pnl) < 0:
                    losses += 1
    except OSError:
        return False
    return losses >= max_losses


def run_c(opps: List[Opportunity], client: Optional[SynthInsightsClient] = None) -> Dict[str, int]:
    """Full C cycle for ALL live variants: settle each variant's open positions,
    then route new candidates to every variant whose rule they satisfy (the same
    trade can legitimately land in multiple books — that's the comparison).
    Never raises to the caller path (scanner guards too)."""
    CONFIG.assert_paper_only()
    totals = {"fills": 0, "exit_at_fair": 0, "resolution": 0, "pending": 0}

    # settle the legacy ledger too until its last position closes
    ledgers = [(name, ledger) for name, ledger, _ in C_VARIANTS] + [("C_LEGACY", C_LEDGER)]
    for _, ledger in ledgers:
        if os.path.exists(_ledger_path(ledger)):
            s = settle_positions(opps, client=client, fills_name=ledger)
            for k in ("exit_at_fair", "resolution", "pending"):
                totals[k] += s[k]

    candidates = evaluate_c(opps)
    for name, ledger, rule in C_VARIANTS:
        if name in PAUSED_VARIANTS and _loss_streak_paused(ledger, *PAUSED_VARIANTS[name]):
            log.info("%s: paused (loss streak) — skipping entries this cycle", name)
            continue
        opened = _opened_condition_ids(ledger)
        path = _ledger_path(ledger)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for sig in candidates:
                if not rule(sig.execution_price, sig.raw_edge):
                    continue
                cid = (sig.condition_id or sig.slug or "").strip()
                if cid and cid in opened:
                    continue
                row = _fill_c(sig, flat=(name == "C_VETO"))
                if row:
                    row["strategy"] = name
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                    totals["fills"] += 1
                    if cid:
                        opened.add(cid)

    if totals["fills"] or totals["resolution"] or totals["exit_at_fair"]:
        log.info("Strategy C variants: %d new fills | settled %d resolved, %d exit, %d pending",
                 totals["fills"], totals["resolution"], totals["exit_at_fair"], totals["pending"])
    return totals
