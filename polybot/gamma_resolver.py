"""Resolve final UP/DOWN labels for Polymarket events via the Gamma API.

Uses outcomePrices (index 0 = YES/UP, index 1 = NO/DOWN) with a 0.95/0.05
threshold as the primary signal, then falls back to winner text fields.
Results are file-cached under polybot/data/resolution_cache/ so repeated
backtest runs avoid redundant API calls.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from .config import CONFIG

log = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.dirname(CONFIG.log_dir), "data", "resolution_cache")

# outcomePrices index 0 = YES token = UP; index 1 = NO token = DOWN
_OUTCOME_PRICES_FIELDS = ("outcomePrices", "outcome_prices", "event_outcome_prices")
_WINNER_FIELDS = (
    "winningOutcome",
    "winning_outcome",
    "resolvedOutcome",
    "resolved_outcome",
    "winner",
    "outcome",
)
_RESOLVE_THRESHOLD = 0.95


def _cache_path(slug: str) -> str:
    safe = slug.replace("/", "_").replace("\\", "_")
    return os.path.join(_CACHE_DIR, f"{safe}.json")


def _load_cache(slug: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(slug)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(slug: str, label: Optional[str], source: str, reason: str) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = _cache_path(slug)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"slug": slug, "label": label, "source": source, "reason": reason}, f)
    except OSError as exc:
        log.debug("gamma_resolver: cache write failed for %s: %s", slug, exc)


def _try_outcome_prices(raw: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (label, reason) from outcomePrices if conclusive, else (None, reason)."""
    for field_name in _OUTCOME_PRICES_FIELDS:
        val = raw.get(field_name)
        if val is None:
            continue
        prices: Any = val
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(prices, list) or len(prices) < 2:
            continue
        try:
            p_yes = float(prices[0])
            p_no = float(prices[1])
        except (TypeError, ValueError):
            continue
        if p_yes >= _RESOLVE_THRESHOLD:
            return "UP", f"{field_name}[0]={p_yes:.3f} >= {_RESOLVE_THRESHOLD}"
        if p_no >= _RESOLVE_THRESHOLD:
            return "DOWN", f"{field_name}[1]={p_no:.3f} >= {_RESOLVE_THRESHOLD}"
        return None, f"{field_name} found but inconclusive: p_yes={p_yes:.3f} p_no={p_no:.3f}"
    return None, "no outcomePrices field found"


def _try_winner_fields(raw: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (label, reason) from text winner fields if conclusive, else (None, reason)."""
    for field_name in _WINNER_FIELDS:
        val = raw.get(field_name)
        if val is None:
            continue
        text = str(val).strip().lower()
        if text in ("up", "yes"):
            return "UP", f"{field_name}={val!r}"
        if text in ("down", "no"):
            return "DOWN", f"{field_name}={val!r}"
    return None, "no conclusive winner field found"


def _resolve_from_raw(raw: Dict[str, Any], slug: str) -> Tuple[Optional[str], str, str]:
    """Try both resolution strategies on a raw Gamma market dict."""
    label, reason = _try_outcome_prices(raw)
    if label is not None:
        _save_cache(slug, label, "gamma_prices", reason)
        return label, "gamma_prices", reason

    prices_reason = reason

    label, reason = _try_winner_fields(raw)
    if label is not None:
        _save_cache(slug, label, "gamma_winner", reason)
        return label, "gamma_winner", reason

    full_reason = f"outcomePrices: {prices_reason}; winner fields: {reason}"
    return None, "unresolved", full_reason


def resolve_final_outcome_from_gamma(
    slug: str,
    condition_id: Optional[str] = None,
    client: Any = None,
) -> Tuple[Optional[str], str, str]:
    """Return (label, source, reason) for a Polymarket market.

    Looks up by slug first; falls back to condition_id if slug lookup
    returns nothing (resolved markets are sometimes purged from Gamma's
    active index but remain queryable by condition_id).

    label:  "UP", "DOWN", or None if unresolved.
    source: "gamma_prices", "gamma_winner",
            "cache:gamma_prices", "cache:gamma_winner",
            "unresolved", "not_found", "no_slug", "gamma_error"
    reason: human-readable detail for logging/debugging.
    """
    if not slug and not condition_id:
        return None, "no_slug", "empty slug and condition_id"

    cache_key = slug or condition_id or ""
    cached = _load_cache(cache_key)
    if cached is not None:
        orig_src = cached.get("source", "gamma")
        return cached.get("label"), f"cache:{orig_src}", cached.get("reason", "")

    if client is None:
        from .polymarket_client import PolymarketClient
        client = PolymarketClient()

    # --- Primary: slug lookup (tries active, closed, and direct endpoints) ---
    if slug:
        try:
            market = client.market_by_slug(slug)
        except Exception as exc:
            log.debug("gamma_resolver: slug lookup error for %s: %s", slug, exc)
            market = None

        if market is not None:
            return _resolve_from_raw(market.raw or {}, cache_key)

    # --- Fallback: condition_id lookup for resolved markets purged from slug index ---
    if condition_id:
        gamma_url = client.gamma_url
        for params in (
            {"conditionId": condition_id},
            {"conditionId": condition_id, "closed": "true"},
        ):
            try:
                resp = client.session.get(
                    f"{gamma_url}/markets", params=params, timeout=client.timeout
                )
                if not resp.ok:
                    continue
                data = resp.json()
                items = data if isinstance(data, list) else (
                    data.get("data") or data.get("markets") or
                    ([data] if isinstance(data, dict) and data.get("conditionId") else [])
                )
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    cid = item.get("conditionId") or item.get("condition_id") or ""
                    if cid != condition_id:
                        continue
                    label, src, reason = _resolve_from_raw(item, cache_key)
                    if label is not None:
                        return label, src, reason
            except Exception as exc:
                log.debug("gamma_resolver: condition_id lookup error %s: %s", condition_id, exc)

    return None, "not_found", f"not found in Gamma: slug={slug!r} condition_id={condition_id!r}"
