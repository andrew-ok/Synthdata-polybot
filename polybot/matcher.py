"""Match Synth forecasts to Polymarket markets.

Strict matching: a forecast is only paired with a market when we are confident
the Synth probability refers to the same resolvable event. Ambiguous markets
(multi-outcome, vague rules, qualitative criteria) are skipped — Synth's prob
must map cleanly onto the YES leg.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from .polymarket_client import PolyMarket

log = logging.getLogger(__name__)


@dataclass
class MatchedMarket:
    market: PolyMarket
    forecast: Any
    reason: str  # how the match was made (id, alias, fuzzy)


_AMBIGUITY_RED_FLAGS = (
    "subjective", "deemed", "at the discretion", "any time",
    "before the end of", "or any other", "or similar",
)


def _is_binary_yes_no(market: PolyMarket) -> bool:
    # Token IDs come in pairs for binary markets; multi-outcome markets won't have a clean YES/NO.
    return bool(market.token_id_yes and market.token_id_no)


def _looks_ambiguous(market: PolyMarket) -> bool:
    text = f"{market.market_question}\n{market.rules}".lower()
    return any(flag in text for flag in _AMBIGUITY_RED_FLAGS)


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class Matcher:
    def __init__(self, alias_map: Optional[Dict[str, str]] = None):
        # Optional: hand-curated map { synth_market_id -> polymarket condition_id }.
        self.alias_map = alias_map or {}

    def match(
        self,
        forecasts: Iterable[Any],
        markets: Iterable[PolyMarket],
    ) -> List[MatchedMarket]:
        market_by_cond = {m.condition_id: m for m in markets}
        # Pre-index by normalized question for fuzzy fallback.
        market_by_qnorm = {}
        for m in market_by_cond.values():
            market_by_qnorm.setdefault(_normalize(m.market_question), m)

        out: List[MatchedMarket] = []
        for fc in forecasts:
            market: Optional[PolyMarket] = None
            reason = ""

            # 1) Direct condition_id match (Synth returns the same id).
            if fc.market_id in market_by_cond:
                market = market_by_cond[fc.market_id]
                reason = "condition_id"

            # 2) Curated alias map.
            if market is None and fc.market_id in self.alias_map:
                cond = self.alias_map[fc.market_id]
                market = market_by_cond.get(cond)
                if market is not None:
                    reason = "alias_map"

            # 3) Question text fallback (only if Synth carries it through raw).
            if market is None:
                q = fc.raw.get("market_question") or fc.raw.get("question") or fc.raw.get("title")
                if isinstance(q, str):
                    market = market_by_qnorm.get(_normalize(q))
                    if market is not None:
                        reason = "question_match"

            if market is None:
                continue

            if not _is_binary_yes_no(market):
                log.debug("Skip %s: not binary YES/NO", market.condition_id)
                continue
            if _looks_ambiguous(market):
                log.debug("Skip %s: ambiguous resolution rules", market.condition_id)
                continue
            if market.closed:
                continue

            out.append(MatchedMarket(market=market, forecast=fc, reason=reason))

        log.info("Matcher: %d Synth forecasts paired to Polymarket binary markets", len(out))
        return out
