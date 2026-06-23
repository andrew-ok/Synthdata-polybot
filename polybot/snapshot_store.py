"""SQLite snapshot store for every scanned opportunity side."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from .config import CONFIG
from .event_identity import event_key_for_opportunity
from .synth_client import Opportunity


@dataclass
class SnapshotRow:
    timestamp_utc: str
    asset: str
    horizon: str
    slug: str
    event_key: str
    condition_id: str
    side: str
    market_question: str
    market_url: str
    event_start_time: str
    event_end_time: str
    event_age_sec: Optional[float]
    seconds_to_event_end: Optional[float]
    raw_synth_probability: float
    previous_raw_synth_probability: Optional[float]
    synth_probability_delta: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    bid_size: float
    ask_size: float
    near_top_liquidity_usd: float
    last_trade_price: Optional[float]
    current_price: float
    source_snapshot_time: str
    is_stale: bool
    current_outcome: str
    resolved_outcome: Optional[str]
    raw_payload_json: str


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(CONFIG.snapshot_db_path), exist_ok=True)
    conn = sqlite3.connect(CONFIG.snapshot_db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            asset TEXT NOT NULL,
            horizon TEXT NOT NULL,
            slug TEXT,
            event_key TEXT NOT NULL,
            condition_id TEXT,
            side TEXT NOT NULL,
            market_question TEXT,
            market_url TEXT,
            event_start_time TEXT,
            event_end_time TEXT,
            event_age_sec REAL,
            seconds_to_event_end REAL,
            raw_synth_probability REAL NOT NULL,
            previous_raw_synth_probability REAL,
            synth_probability_delta REAL,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            bid_size REAL,
            ask_size REAL,
            near_top_liquidity_usd REAL,
            last_trade_price REAL,
            current_price REAL,
            source_snapshot_time TEXT,
            is_stale INTEGER NOT NULL,
            current_outcome TEXT,
            resolved_outcome TEXT,
            raw_payload_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_event_side ON snapshots(event_key, side, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(timestamp_utc)")
    return conn


def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(dt or "")


def _spread(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    return round(ask - bid, 6)


def _prev_probability(conn: sqlite3.Connection, event_key: str, side: str) -> Optional[float]:
    row = conn.execute(
        "SELECT raw_synth_probability FROM snapshots WHERE event_key=? AND side=? ORDER BY id DESC LIMIT 1",
        (event_key, side),
    ).fetchone()
    return float(row[0]) if row else None


def _row_for_side(conn: sqlite3.Connection, opp: Opportunity, side: str, timestamp: str) -> SnapshotRow:
    event_key = event_key_for_opportunity(opp)
    if side == "UP":
        raw_prob = opp.synth_probability_up
        bid = opp.yes_bid_price if opp.yes_bid_price is not None else opp.best_bid_price
        ask = opp.yes_ask_price if opp.yes_ask_price is not None else opp.best_ask_price
        bid_size = opp.yes_bid_size or opp.best_bid_size
        ask_size = opp.yes_ask_size or opp.best_ask_size
    else:
        raw_prob = opp.synth_probability_down
        bid = opp.no_bid_price
        ask = opp.no_ask_price
        bid_size = opp.no_bid_size
        ask_size = opp.no_ask_size

    prev = _prev_probability(conn, event_key, side)
    delta = None if prev is None else raw_prob - prev
    now = datetime.now(timezone.utc)
    synth_age = (now - opp.current_time).total_seconds() if opp.current_time else 0.0
    clob_age = (now - opp.clob_snapshot_time).total_seconds() if opp.clob_snapshot_time else 999999.0
    return SnapshotRow(
        timestamp_utc=timestamp,
        asset=opp.asset,
        horizon=opp.horizon,
        slug=opp.slug,
        event_key=event_key,
        condition_id=opp.condition_id,
        side=side,
        market_question=f"{opp.asset} {opp.horizon} Up/Down - {opp.slug}",
        market_url=opp.polymarket_url,
        event_start_time=_iso(opp.event_start_time),
        event_end_time=_iso(opp.event_end_time),
        event_age_sec=opp.event_age_sec,
        seconds_to_event_end=opp.seconds_to_event_end,
        raw_synth_probability=raw_prob,
        previous_raw_synth_probability=prev,
        synth_probability_delta=delta,
        best_bid=bid,
        best_ask=ask,
        spread=_spread(bid, ask),
        bid_size=float(bid_size or 0.0),
        ask_size=float(ask_size or 0.0),
        near_top_liquidity_usd=float((ask or 0.0) * (ask_size or 0.0)),
        last_trade_price=opp.last_trade_price,
        current_price=opp.current_price,
        source_snapshot_time=_iso(opp.current_time),
        is_stale=synth_age > CONFIG.max_synth_staleness_sec or clob_age > CONFIG.max_clob_staleness_sec,
        current_outcome=opp.current_outcome,
        resolved_outcome=opp.resolved_outcome,
        raw_payload_json=json.dumps(opp.raw or {}, sort_keys=True, default=str),
    )


def write_snapshots(opportunities: Iterable[Opportunity]) -> List[SnapshotRow]:
    timestamp = datetime.now(timezone.utc).isoformat()
    rows: List[SnapshotRow] = []
    with _connect() as conn:
        for opp in opportunities:
            for side in ("UP", "DOWN"):
                row = _row_for_side(conn, opp, side, timestamp)
                rows.append(row)
                payload = asdict(row)
                payload["is_stale"] = int(row.is_stale)
                conn.execute(
                    """
                    INSERT INTO snapshots (
                        timestamp_utc, asset, horizon, slug, event_key, condition_id, side,
                        market_question, market_url, event_start_time, event_end_time,
                        event_age_sec, seconds_to_event_end, raw_synth_probability,
                        previous_raw_synth_probability, synth_probability_delta, best_bid,
                        best_ask, spread, bid_size, ask_size, near_top_liquidity_usd,
                        last_trade_price, current_price, source_snapshot_time, is_stale,
                        current_outcome, resolved_outcome, raw_payload_json
                    ) VALUES (
                        :timestamp_utc, :asset, :horizon, :slug, :event_key, :condition_id, :side,
                        :market_question, :market_url, :event_start_time, :event_end_time,
                        :event_age_sec, :seconds_to_event_end, :raw_synth_probability,
                        :previous_raw_synth_probability, :synth_probability_delta, :best_bid,
                        :best_ask, :spread, :bid_size, :ask_size, :near_top_liquidity_usd,
                        :last_trade_price, :current_price, :source_snapshot_time, :is_stale,
                        :current_outcome, :resolved_outcome, :raw_payload_json
                    )
                    """,
                    payload,
                )
    return rows
