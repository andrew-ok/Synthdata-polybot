"""Live CLOB enrichment for current opportunities.

Synth's insight payload includes Synth's Up probability and, in the current
normalizer, an Up-token book. For live/paper scans, resolve the Polymarket
slug and fetch both YES/NO token books so both sides use executable CLOB asks.
"""
from __future__ import annotations

import logging
from typing import Iterable, List

from .config import CONFIG
from .polymarket_client import PolymarketClient
from .synth_client import Opportunity

log = logging.getLogger(__name__)


def enrich_real_clob(opportunities: Iterable[Opportunity]) -> List[Opportunity]:
    opps = list(opportunities)
    if not CONFIG.use_real_two_sided_clob:
        return opps

    client = PolymarketClient()
    enriched_yes = 0
    enriched_no = 0
    for opp in opps:
        market = client.market_by_slug(opp.slug)
        if market is None or not market.token_id_yes or not market.token_id_no:
            continue

        yes_book = client.fetch_book(market.token_id_yes)
        no_book = client.fetch_book(market.token_id_no)
        if yes_book:
            yb, ya, yliq, ybs, yas = client._best_levels(yes_book)
            opp.yes_ask_liquidity_usd = yliq
            opp.yes_bid_price = yb
            opp.yes_ask_price = ya
            opp.yes_bid_size = ybs
            opp.yes_ask_size = yas
            # Keep the legacy Up fields aligned for existing dashboard columns.
            opp.best_bid_price = yb
            opp.best_ask_price = ya
            opp.best_bid_size = ybs
            opp.best_ask_size = yas
            if ya is not None:
                enriched_yes += 1
        if no_book:
            nb, na, nliq, nbs, nas = client._best_levels(no_book)
            opp.no_ask_liquidity_usd = nliq
            opp.no_bid_price = nb
            opp.no_ask_price = na
            opp.no_bid_size = nbs
            opp.no_ask_size = nas
            if na is not None:
                enriched_no += 1

    log.info(
        "Polymarket CLOB: enriched YES asks for %d/%d and NO asks for %d/%d opportunities",
        enriched_yes,
        len(opps),
        enriched_no,
        len(opps),
    )
    return opps


def enrich_real_down_clob(opportunities: Iterable[Opportunity]) -> List[Opportunity]:
    """Backward-compatible wrapper for older imports."""
    return enrich_real_clob(opportunities)
