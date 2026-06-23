"""Risk manager.

Hard gates first, then size. Returns a decision per signal explaining why it
was accepted or rejected — the dashboard surfaces these reasons verbatim.
"""
from __future__ import annotations

import logging
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from .config import CONFIG
from .position_manager import exposure_summary, has_open_event, recently_closed_or_opened, open_positions
from .signal_engine import Signal

log = logging.getLogger(__name__)


@dataclass
class Decision:
    signal: Signal
    accepted: bool
    reason: str
    position_size_usd: float = 0.0
    contracts: float = 0.0


# Very coarse correlation key — group by the dominant proper noun / ticker in
# the question. Two BTC markets or two "Trump 2024" markets won't both fire.
_CORR_STOPWORDS = {
    "will", "the", "a", "an", "be", "in", "on", "by", "at", "to", "for",
    "of", "and", "or", "vs", "before", "after", "above", "below", "have",
    "has", "is", "are", "this", "that", "year", "month", "day",
}


def _correlation_key(question: str) -> str:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]+", question)
    primary = next((t for t in tokens if t.lower() not in _CORR_STOPWORDS and (t[0].isupper() or t.isupper())), "")
    return primary.lower()


def _kelly_size(prob: float, price: float, fraction: float) -> float:
    """Fractional Kelly stake fraction of bankroll for a YES-style binary bet at `price`."""
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price) / price       # net odds
    q = 1.0 - prob
    k = (b * prob - q) / b
    return max(0.0, min(CONFIG.max_kelly_fraction, fraction * k))


class RiskManager:
    def __init__(self, use_kelly: bool = False):
        self.use_kelly = use_kelly
        self.bankroll = CONFIG.bankroll_usd
        self.opened_condition_ids = _load_opened_condition_ids()
        self.exposure = exposure_summary()
        self.open_positions = open_positions()

    def evaluate(self, signals: Iterable[Signal]) -> List[Decision]:
        # Hard precondition: rule 7.
        CONFIG.assert_paper_only()

        decisions: List[Decision] = []
        seen_condition_ids: Set[str] = set()
        seen_event_keys: Set[str] = set()
        seen_corr: Dict[str, Decision] = {}
        deployed = float(self.exposure["total"])
        accepted_count = 0
        accepted_by_asset: Dict[str, int] = {}
        accepted_by_horizon: Dict[str, int] = {}

        for sig in signals:
            ok, reason = self._gates(sig)
            if not ok:
                decisions.append(Decision(sig, False, reason))
                continue

            event_key = sig.event_key
            condition_id = (sig.condition_id or sig.event_key or sig.slug or "").strip()
            if event_key and has_open_event(event_key):
                decisions.append(Decision(sig, False, "open position already exists for this event_key"))
                continue
            if event_key and recently_closed_or_opened(event_key):
                decisions.append(Decision(sig, False, "event_key in position cooldown"))
                continue
            if event_key and event_key in seen_event_keys:
                decisions.append(Decision(sig, False, "position already selected for this event_key in this scan"))
                continue
            if condition_id and condition_id in seen_condition_ids:
                decisions.append(Decision(sig, False, "position already selected for this event contract in this scan"))
                continue

            corr = _correlation_key(sig.market_question)
            if corr and corr in seen_corr:
                decisions.append(Decision(sig, False, f"correlated with prior trade on '{corr}'"))
                continue

            size_usd, contracts = self._size(sig)
            if size_usd <= 0:
                decisions.append(Decision(sig, False, "sizing produced zero notional"))
                continue

            if accepted_count >= CONFIG.max_trades_per_scan:
                decisions.append(Decision(sig, False, "max_trades_per_scan reached"))
                continue
            if self.exposure["count"] + accepted_count >= CONFIG.max_open_positions:
                decisions.append(Decision(sig, False, "max_open_positions reached"))
                continue
            if sum(1 for p in self.open_positions if p.asset == sig.asset) + accepted_by_asset.get(sig.asset, 0) >= CONFIG.max_open_positions_per_asset:
                decisions.append(Decision(sig, False, "max_open_positions_per_asset reached"))
                continue
            if sum(1 for p in self.open_positions if p.horizon == sig.horizon) + accepted_by_horizon.get(sig.horizon, 0) >= CONFIG.max_open_positions_per_horizon:
                decisions.append(Decision(sig, False, "max_open_positions_per_horizon reached"))
                continue

            # Rule 6: position size cap (1–2% of bankroll).
            max_pos_usd = CONFIG.max_position_size * self.bankroll
            if size_usd > max_pos_usd + 1e-9:
                size_usd = round(max_pos_usd, 2)
                contracts = round(size_usd / sig.execution_price, 4)

            # Rule 5: liquidity must comfortably cover the intended notional.
            need_liq = CONFIG.liquidity_size_multiple * size_usd
            if sig.liquidity < need_liq:
                decisions.append(Decision(
                    sig, False,
                    f"liquidity ${sig.liquidity:.0f} < ${need_liq:.0f} "
                    f"({CONFIG.liquidity_size_multiple:g}× size, would move price)",
                ))
                continue

            if deployed + size_usd > CONFIG.max_total_exposure * self.bankroll:
                decisions.append(Decision(sig, False, "max_total_exposure reached"))
                continue
            if self.exposure["by_asset"].get(sig.asset, 0.0) + size_usd > CONFIG.max_asset_exposure * self.bankroll:
                decisions.append(Decision(sig, False, "max_asset_exposure reached"))
                continue
            if self.exposure["by_horizon"].get(sig.horizon, 0.0) + size_usd > CONFIG.max_horizon_exposure * self.bankroll:
                decisions.append(Decision(sig, False, "max_horizon_exposure reached"))
                continue

            decision = Decision(sig, True, "accepted", size_usd, contracts)
            decisions.append(decision)
            if event_key:
                seen_event_keys.add(event_key)
            if condition_id:
                seen_condition_ids.add(condition_id)
            if corr:
                seen_corr[corr] = decision
            deployed += size_usd
            accepted_count += 1
            accepted_by_asset[sig.asset] = accepted_by_asset.get(sig.asset, 0) + 1
            accepted_by_horizon[sig.horizon] = accepted_by_horizon.get(sig.horizon, 0) + 1

        return decisions

    def _gates(self, sig: Signal) -> tuple[bool, str]:
        if sig.net_edge < CONFIG.min_entry_edge:
            return False, f"net entry edge {sig.net_edge:.3f} < {CONFIG.min_entry_edge:.3f}"
        if sig.confidence_score < CONFIG.min_confidence_score:
            return False, f"confidence {sig.confidence_score:.3f} < {CONFIG.min_confidence_score:.3f}"
        # Rule 4: spread <= 5pp.
        if sig.spread is not None and sig.spread > CONFIG.max_spread:
            return False, f"spread {sig.spread:.3f} > max_spread {CONFIG.max_spread:.3f}"
        # Rule 5 (floor — actual size-aware check happens after sizing).
        if sig.liquidity < CONFIG.min_liquidity:
            return False, f"liquidity ${sig.liquidity:.0f} < ${CONFIG.min_liquidity:.0f}"
        if sig.is_stale:
            return False, "stale Synth or CLOB data"
        if CONFIG.require_real_two_sided_clob and sig.clob_source != "real_clob":
            return False, "real two-sided CLOB required"

        max_age = CONFIG.max_entry_age_15m_sec if sig.horizon == "15M" else CONFIG.max_entry_age_1h_sec
        if sig.event_age_sec is not None and sig.event_age_sec > max_age:
            return False, f"event age {sig.event_age_sec:.0f}s > max entry age {max_age:.0f}s"
        if sig.seconds_to_event_end is not None and sig.seconds_to_event_end < CONFIG.min_seconds_to_event_end:
            return False, (
                f"only {sig.seconds_to_event_end:.0f}s to close "
                f"< min {CONFIG.min_seconds_to_event_end:.0f}s"
            )
        if (
            CONFIG.min_hours_to_resolution > 0
            and sig.hours_to_resolution is not None
            and sig.hours_to_resolution < CONFIG.min_hours_to_resolution
        ):
            return False, f"only {sig.hours_to_resolution:.1f}h to resolution"
        if CONFIG.allowed_categories and sig.category and sig.category not in CONFIG.allowed_categories:
            return False, f"category '{sig.category}' not in allowlist"
        if sig.execution_price <= 0 or sig.execution_price >= 1:
            return False, f"degenerate ask {sig.execution_price}"
        return True, ""

    def _size(self, sig: Signal) -> tuple[float, float]:
        if self.use_kelly:
            frac = _kelly_size(sig.calibrated_probability, sig.execution_price, CONFIG.kelly_fraction)
            frac = min(frac, CONFIG.max_position_size)
        else:
            frac = CONFIG.max_position_size
        confidence_multiplier = min(CONFIG.max_confidence_position_multiplier, max(CONFIG.model_confidence_floor, sig.model_confidence))
        frac = min(CONFIG.max_position_size, frac * confidence_multiplier)
        size_usd = round(self.bankroll * frac, 2)
        contracts = round(size_usd / sig.execution_price, 4) if sig.execution_price > 0 else 0.0
        return size_usd, contracts


def _load_opened_condition_ids() -> Set[str]:
    """Return event contracts that already have fills in the durable paper ledger."""
    path = os.path.join(CONFIG.log_dir, "fills.jsonl")
    opened: Set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                condition_id = str(row.get("condition_id") or "").strip()
                if condition_id:
                    opened.add(condition_id)
    except FileNotFoundError:
        return opened
    except OSError as exc:
        log.warning("Could not read fills ledger %s: %s", path, exc)
    return opened
