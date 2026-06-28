"""Live CLOB enrichment for current opportunities.

Synth's insight payload includes Synth's Up probability and, in the current
normalizer, an Up-token book. For live/paper scans, resolve the Polymarket
slug and fetch both YES/NO token books so both sides use executable CLOB asks.

Each opportunity requires a Gamma slug lookup + 2 CLOB book fetches.
These are run concurrently (one thread per opportunity) to minimise wall-clock
latency across a full scan cycle.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from .config import CONFIG
from .polymarket_client import PolymarketClient
from .synth_client import Opportunity

log = logging.getLogger(__name__)


def _enrich_one(opp: Opportunity) -> Tuple[Opportunity, int, int]:
    """Fetch Gamma metadata + YES/NO CLOB books for a single opportunity.

    Creates its own PolymarketClient (and therefore its own requests.Session)
    so concurrent calls don't share a non-thread-safe Session.
    Returns (opp, enriched_yes, enriched_no).
    """
    client = PolymarketClient()
    market = client.market_by_slug(opp.slug)
    if market is None or not market.token_id_yes or not market.token_id_no:
        return opp, 0, 0

    opp.condition_id = market.condition_id or opp.condition_id
    yes_book = client.fetch_book(market.token_id_yes)
    no_book = client.fetch_book(market.token_id_no)
    opp.clob_snapshot_time = datetime.now(timezone.utc)

    enriched_yes = enriched_no = 0
    if yes_book:
        yb, ya, _, ybs, yas = client._best_levels(yes_book)
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
            enriched_yes = 1
    if no_book:
        nb, na, _, nbs, nas = client._best_levels(no_book)
        opp.no_bid_price = nb
        opp.no_ask_price = na
        opp.no_bid_size = nbs
        opp.no_ask_size = nas
        if na is not None:
            enriched_no = 1

    return opp, enriched_yes, enriched_no


def enrich_real_clob(opportunities: Iterable[Opportunity]) -> List[Opportunity]:
    opps = list(opportunities)
    if not CONFIG.use_real_two_sided_clob or not opps:
        return opps

    enriched_yes = 0
    enriched_no = 0
    max_workers = min(len(opps), 8)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_enrich_one, opp): opp for opp in opps}
        for future in as_completed(futures):
            try:
                _, ey, en = future.result()
                enriched_yes += ey
                enriched_no += en
            except Exception as exc:
                opp = futures[future]
                log.warning("CLOB enrichment failed for %s: %s", opp.slug, exc)

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
