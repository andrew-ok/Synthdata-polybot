"""Polymarket client.

- Gamma API for market discovery (metadata, categories, status).
- CLOB API for orderbooks (best bid/ask, spread, depth).

Never trust the Gamma 'lastTradePrice' as your execution price — always use the
CLOB best ask to buy and best bid to sell.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import CONFIG

log = logging.getLogger(__name__)


@dataclass
class PolyMarket:
    condition_id: str
    token_id_yes: Optional[str]
    token_id_no: Optional[str]
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    spread: Optional[float] = None
    volume: float = 0.0
    liquidity: float = 0.0          # USD available within a reasonable price band
    resolution_date: Optional[datetime] = None
    market_question: str = ""
    rules: str = ""
    category: str = ""
    closed: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_orderbook(self) -> bool:
        return self.yes_ask is not None and self.yes_bid is not None

    @property
    def hours_to_resolution(self) -> Optional[float]:
        if not self.resolution_date:
            return None
        delta = self.resolution_date - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600.0


class PolymarketClient:
    def __init__(
        self,
        gamma_url: Optional[str] = None,
        clob_url: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.gamma_url = (gamma_url or CONFIG.polymarket_gamma_url).rstrip("/")
        self.clob_url = (clob_url or CONFIG.polymarket_clob_url).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "polybot/0.1",
        })

    # ---------- Gamma ----------

    def list_active_markets(self, limit: int = 200, offset: int = 0) -> List[PolyMarket]:
        url = f"{self.gamma_url}/markets"
        params = {"active": "true", "closed": "false", "limit": limit, "offset": offset}
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        items = resp.json()
        if isinstance(items, dict):
            items = items.get("data") or items.get("markets") or []
        out: List[PolyMarket] = []
        for it in items:
            m = self._parse_gamma_market(it)
            if m is not None:
                out.append(m)
        log.info("Gamma: %d active markets", len(out))
        return out

    def market_by_slug(self, slug: str) -> Optional[PolyMarket]:
        """Resolve a Gamma market by slug, if the market is still discoverable."""
        if not slug:
            return None
        candidates = [
            (f"{self.gamma_url}/markets", {"slug": slug}),
            (f"{self.gamma_url}/markets", {"slug": slug, "active": "true", "closed": "false"}),
            (f"{self.gamma_url}/markets", {"slug": slug, "closed": "true"}),
            (f"{self.gamma_url}/markets", {"slug": slug, "active": "false", "closed": "true"}),
            (f"{self.gamma_url}/markets/{slug}", None),
        ]
        for url, params in candidates:
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if not resp.ok:
                    continue
                data = resp.json()
            except (requests.RequestException, ValueError):
                continue

            items = data
            if isinstance(data, dict):
                if data.get("slug") == slug or data.get("conditionId") or data.get("condition_id"):
                    items = [data]
                else:
                    items = data.get("data") or data.get("markets") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("slug") or "") != slug:
                    continue
                market = self._parse_gamma_market(item)
                if market is not None:
                    return market
        return None

    @staticmethod
    def _parse_gamma_market(it: Dict[str, Any]) -> Optional[PolyMarket]:
        condition_id = it.get("conditionId") or it.get("condition_id")
        if not condition_id:
            return None

        # Polymarket's binary markets expose clobTokenIds as a JSON-encoded list ordered [YES, NO].
        token_yes = token_no = None
        ids_field = it.get("clobTokenIds") or it.get("clob_token_ids")
        if isinstance(ids_field, str):
            import json
            try:
                ids = json.loads(ids_field)
            except Exception:
                ids = []
        else:
            ids = ids_field or []
        if isinstance(ids, list) and len(ids) >= 2:
            token_yes, token_no = str(ids[0]), str(ids[1])

        end_raw = it.get("endDate") or it.get("end_date_iso") or it.get("end_date")
        end_dt: Optional[datetime] = None
        if isinstance(end_raw, str):
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except Exception:
                end_dt = None

        return PolyMarket(
            condition_id=str(condition_id),
            token_id_yes=token_yes,
            token_id_no=token_no,
            volume=float(it.get("volumeNum") or it.get("volume") or 0.0),
            liquidity=float(it.get("liquidityNum") or it.get("liquidity") or 0.0),
            resolution_date=end_dt,
            market_question=str(it.get("question") or it.get("title") or ""),
            rules=str(it.get("description") or it.get("resolutionSource") or ""),
            category=str(it.get("category") or it.get("categoryName") or "").lower(),
            closed=bool(it.get("closed") or False),
            raw=it,
        )

    # ---------- CLOB ----------

    def fetch_book(self, token_id: str) -> Optional[Dict[str, Any]]:
        url = f"{self.clob_url}/book"
        try:
            resp = self.session.get(url, params={"token_id": token_id}, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.debug("CLOB book(%s) failed: %s", token_id, exc)
            return None

    @staticmethod
    def _best_levels(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], float, float, float]:
        """Return (best_bid, best_ask, near-top liquidity USD, top_bid_size, top_ask_size)."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def _top(levels: List[Dict[str, Any]], side: str) -> Optional[Dict[str, Any]]:
            if not levels:
                return None
            try:
                key = float if side == "ask" else (lambda x: -float(x))
                return sorted(levels, key=lambda lv: key(lv.get("price", 0)))[0]
            except Exception:
                return levels[0]

        top_bid = _top(bids, "bid")
        top_ask = _top(asks, "ask")
        best_bid = float(top_bid["price"]) if top_bid and "price" in top_bid else None
        best_ask = float(top_ask["price"]) if top_ask and "price" in top_ask else None
        top_bid_size = float(top_bid.get("size", 0.0)) if top_bid else 0.0
        top_ask_size = float(top_ask.get("size", 0.0)) if top_ask else 0.0

        # Crude liquidity: USD notional sitting within 2 cents of the top of the ask.
        liq = 0.0
        if best_ask is not None:
            for lv in asks:
                try:
                    px = float(lv["price"])
                    sz = float(lv["size"])
                    if px <= best_ask + 0.02:
                        liq += px * sz
                except Exception:
                    continue
        return best_bid, best_ask, liq, top_bid_size, top_ask_size

    def enrich_with_orderbook(self, market: PolyMarket) -> PolyMarket:
        if not market.token_id_yes or not market.token_id_no:
            return market
        yes_book = self.fetch_book(market.token_id_yes)
        no_book = self.fetch_book(market.token_id_no)
        if yes_book:
            yb, ya, yliq, _, _ = self._best_levels(yes_book)
            market.yes_bid, market.yes_ask = yb, ya
            market.liquidity = max(market.liquidity, yliq)
        if no_book:
            nb, na, nliq, _, _ = self._best_levels(no_book)
            market.no_bid, market.no_ask = nb, na
            market.liquidity = max(market.liquidity, nliq)
        if market.yes_bid is not None and market.yes_ask is not None:
            market.spread = round(market.yes_ask - market.yes_bid, 4)
        return market
