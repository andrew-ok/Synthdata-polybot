"""Synth Insights client.

Hits Synth's pre-baked Polymarket endpoints:
    /insights/polymarket/up-down/15min
    /insights/polymarket/up-down/hourly

Each call returns Synth's fair probability AND the live Polymarket orderbook
for the same contract. That removes the need for separate market discovery,
matcher, or CLOB calls.

Auth: header `Authorization: Apikey <key>` (not Bearer).
"""
from __future__ import annotations

import json
import logging
import os
import hashlib
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from .config import CONFIG

log = logging.getLogger(__name__)


# ---------------- token / usage meter ----------------

_USAGE_LOCK = threading.Lock()
_USAGE = {
    "calls": 0,
    "ok": 0,
    "err": 0,
    "cache_hits": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}


def usage_snapshot() -> Dict[str, Any]:
    with _USAGE_LOCK:
        return dict(_USAGE)


def _record_call(ok: bool, path: str, params: Optional[Dict[str, Any]] = None, cache_hit: bool = False) -> None:
    with _USAGE_LOCK:
        if cache_hit:
            _USAGE["cache_hits"] += 1
        else:
            _USAGE["calls"] += 1
            _USAGE["ok" if ok else "err"] += 1
        _persist_usage(api_path=path, params=params, ok=ok, cache_hit=cache_hit)


def _persist_usage(
    api_path: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    ok: Optional[bool] = None,
    cache_hit: bool = False,
) -> None:
    os.makedirs(CONFIG.log_dir, exist_ok=True)
    usage_path = os.path.join(CONFIG.log_dir, "usage.jsonl")
    with open(usage_path, "a", encoding="utf-8") as f:
        row = {**_USAGE, "ts": datetime.now(timezone.utc).isoformat()}
        if api_path:
            row["path"] = api_path
        if params:
            row["params"] = params
        if ok is not None:
            row["ok_last"] = ok
        row["cache_hit_last"] = cache_hit
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _cache_key(path: str, params: Optional[Dict[str, Any]]) -> str:
    payload = json.dumps({"path": path, "params": params or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(path: str, params: Optional[Dict[str, Any]]) -> str:
    return os.path.join(CONFIG.data_dir, "synth_cache", _cache_key(path, params) + ".json")


def _cache_ttl(params: Optional[Dict[str, Any]]) -> float:
    if params and params.get("start_time"):
        return CONFIG.synth_historical_cache_ttl_sec
    return CONFIG.synth_live_cache_ttl_sec


def _read_cache(path: str, params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not CONFIG.synth_cache_enabled:
        return None
    cache_file = _cache_path(path, params)
    try:
        age = time.time() - os.path.getmtime(cache_file)
    except OSError:
        return None
    if age > _cache_ttl(params):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(path: str, params: Optional[Dict[str, Any]], data: Dict[str, Any]) -> None:
    if not CONFIG.synth_cache_enabled:
        return
    cache_file = _cache_path(path, params)
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    tmp = cache_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, cache_file)


INSIGHTS_PATHS = {
    "15M": "/insights/polymarket/up-down/15min",
    "1H":  "/insights/polymarket/up-down/hourly",
}

HORIZON_SECONDS_TO_LABEL = {
    900: "15M",
    3600: "1H",
}

# Asset coverage per Synth swagger as of 2026-06-14.
INSIGHT_ASSETS_BY_HORIZON = {
    "15M": ["BTC", "ETH", "SOL", "HYPE"],
    "1H":  ["BTC", "ETH", "SOL", "HYPE"],
}


def configured_horizons() -> List[str]:
    """Return configured Synth horizon labels supported by this client."""
    labels: List[str] = []
    for seconds in CONFIG.synth_horizons_sec:
        label = HORIZON_SECONDS_TO_LABEL.get(seconds)
        if label is None:
            log.warning("Ignoring unsupported SYNTH_HORIZONS_SEC value: %s", seconds)
            continue
        if label not in labels:
            labels.append(label)
    return labels or list(INSIGHTS_PATHS.keys())


@dataclass
class Opportunity:
    """A single (asset, horizon) snapshot: Synth prob + live Polymarket book."""
    asset: str
    horizon: str              # "15M" or "1H"
    slug: str
    event_start_time: datetime
    event_end_time: datetime
    forecast_start_time: Optional[datetime]
    current_time: datetime

    synth_probability_up: float
    synth_outcome: str        # "Up" or "Down"

    polymarket_probability_up: float    # market-implied (midpoint)
    polymarket_outcome: str             # which side market is currently pricing as more likely

    # Polymarket CLOB book — these are the Up token's bid/ask per Synth's docs.
    best_bid_price: Optional[float]
    best_ask_price: Optional[float]
    best_bid_size: float
    best_ask_size: float
    last_trade_price: Optional[float]

    # Reference prices
    current_price: float
    start_price: float
    current_outcome: str               # realized so far (Up/Down)
    resolved_outcome: Optional[str] = None   # populated for historical snapshots

    # Optional real two-token CLOB data, populated by Polymarket enrichment.
    yes_bid_price: Optional[float] = None
    yes_ask_price: Optional[float] = None
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    no_bid_price: Optional[float] = None
    no_ask_price: Optional[float] = None
    no_bid_size: float = 0.0
    no_ask_size: float = 0.0
    # USD depth within 2c of the best ask (from CLOB enrichment) — truer
    # executable liquidity than top-of-book size alone.
    yes_ask_liquidity_usd: Optional[float] = None
    no_ask_liquidity_usd: Optional[float] = None

    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def synth_probability_down(self) -> float:
        return 1.0 - self.synth_probability_up

    @property
    def hours_to_event_end(self) -> float:
        return (self.event_end_time - datetime.now(timezone.utc)).total_seconds() / 3600.0

    @property
    def event_age_sec(self) -> float:
        return (self.current_time - self.event_start_time).total_seconds()

    @property
    def seconds_to_event_end(self) -> float:
        return (self.event_end_time - self.current_time).total_seconds()

    @property
    def polymarket_url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}" if self.slug else ""


class SynthInsightsClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key or CONFIG.synth_api_key
        self.base_url = (base_url or CONFIG.synth_base_url).rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            log.warning("SYNTH_API_KEY is not set — Synth calls will fail.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Apikey {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "polybot/0.2",
        })

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        cached = _read_cache(path, params)
        if cached is not None:
            _record_call(True, path, params, cache_hit=True)
            return cached

        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 429:
                # one retry after a short backoff — a missed :47 scan costs a
                # late-window strategy its only shot at that hour
                time.sleep(2.5)
                resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("Synth GET %s failed: %s", path, exc)
            _record_call(False, path, params)
            return None
        if resp.status_code == 401:
            log.error("Synth 401 on %s — check SYNTH_API_KEY (expects 'Apikey ...').", path)
            _record_call(False, path, params)
            return None
        if resp.status_code == 404:
            _record_call(False, path, params)
            return None
        if not resp.ok:
            log.warning("Synth %s -> HTTP %s  %s", path, resp.status_code, resp.text[:200])
            _record_call(False, path, params)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("Synth %s returned non-JSON", path)
            _record_call(False, path, params)
            return None
        if isinstance(data, dict):
            _write_cache(path, params, data)
        _record_call(True, path, params)
        return data

    @staticmethod
    def _parse_dt(s: Optional[str]) -> Optional[datetime]:
        if not s or not isinstance(s, str):
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    @classmethod
    def _to_opportunity(cls, asset: str, horizon: str, data: Dict[str, Any]) -> Optional[Opportunity]:
        try:
            return Opportunity(
                asset=asset,
                horizon=horizon,
                slug=str(data.get("slug") or ""),
                event_start_time=cls._parse_dt(data.get("event_start_time")) or datetime.now(timezone.utc),
                event_end_time=cls._parse_dt(data.get("event_end_time")) or datetime.now(timezone.utc),
                forecast_start_time=cls._parse_dt(data.get("forecast_start_time")),
                current_time=cls._parse_dt(data.get("current_time")) or datetime.now(timezone.utc),
                synth_probability_up=float(data.get("synth_probability_up") or 0.0),
                synth_outcome=str(data.get("synth_outcome") or ""),
                polymarket_probability_up=float(data.get("polymarket_probability_up")
                                                or data.get("market_probability_up") or 0.0),
                polymarket_outcome=str(data.get("polymarket_outcome") or data.get("market_outcome") or ""),
                best_bid_price=_optfloat(data.get("best_bid_price")),
                best_ask_price=_optfloat(data.get("best_ask_price")),
                best_bid_size=float(data.get("best_bid_size") or 0.0),
                best_ask_size=float(data.get("best_ask_size") or 0.0),
                last_trade_price=_optfloat(data.get("polymarket_last_trade_price")),
                yes_bid_price=_optfloat(data.get("best_bid_price")),
                yes_ask_price=_optfloat(data.get("best_ask_price")),
                yes_bid_size=float(data.get("best_bid_size") or 0.0),
                yes_ask_size=float(data.get("best_ask_size") or 0.0),
                current_price=float(data.get("current_price") or 0.0),
                start_price=float(data.get("start_price") or 0.0),
                current_outcome=str(data.get("current_outcome") or ""),
                resolved_outcome=_normalize_outcome(
                    data.get("resolved_outcome")
                    or data.get("final_outcome")
                    or data.get("event_outcome")
                    or data.get("actual_outcome")
                ),
                raw=data,
            )
        except (TypeError, ValueError) as exc:
            log.debug("Failed to normalize Opportunity for %s/%s: %s", asset, horizon, exc)
            return None

    def fetch(self, asset: str, horizon: str, start_time: Optional[str] = None) -> Optional[Opportunity]:
        path = INSIGHTS_PATHS.get(horizon)
        if not path:
            raise ValueError(f"Unknown horizon {horizon!r}; expected one of {list(INSIGHTS_PATHS)}")
        params: Dict[str, Any] = {"asset": asset}
        if start_time:
            params["start_time"] = start_time
        data = self._get(path, params)
        if not isinstance(data, dict):
            return None
        return self._to_opportunity(asset, horizon, data)

    def fetch_all(self, assets: Optional[List[str]] = None, horizons: Optional[List[str]] = None) -> List[Opportunity]:
        out: List[Opportunity] = []
        for horizon in horizons or list(INSIGHTS_PATHS.keys()):
            for asset in (assets or INSIGHT_ASSETS_BY_HORIZON.get(horizon, [])):
                opp = self.fetch(asset, horizon)
                if opp is not None:
                    out.append(opp)
        log.info("Insights: pulled %d opportunities", len(out))
        return out


def _optfloat(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_outcome(x: Any) -> Optional[str]:
    raw = str(x or "").strip().lower()
    if raw in ("up", "yes", "true", "1"):
        return "UP"
    if raw in ("down", "no", "false", "0"):
        return "DOWN"
    return None
