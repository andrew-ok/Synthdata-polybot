"""Configuration for the Polymarket x Synth paper-trading bot.

All secrets come from environment variables. Never hardcode keys.
Paper-trade mode is ON by default. Live trading requires an explicit override.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_bool_any(names: tuple[str, ...], default: bool) -> bool:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None:
            return raw.strip().lower() in ("1", "true", "yes", "on")
    return default


@dataclass
class Config:
    # --- API keys ---
    synth_api_key: str = field(default_factory=lambda: _env("SYNTH_API_KEY", "") or "")
    synth_base_url: str = field(default_factory=lambda: _env("SYNTH_BASE_URL", "https://api.synthdata.co") or "https://api.synthdata.co")
    polymarket_gamma_url: str = field(default_factory=lambda: _env("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com") or "https://gamma-api.polymarket.com")
    polymarket_clob_url: str = field(default_factory=lambda: _env("POLYMARKET_CLOB_URL", "https://clob.polymarket.com") or "https://clob.polymarket.com")

    # --- Trading mode ---
    paper_trade_mode: bool = field(default_factory=lambda: _env_bool("PAPER_TRADE_MODE", True))
    enable_live_trading: bool = field(default_factory=lambda: _env_bool("ENABLE_LIVE_TRADING", False))

    # --- Bankroll / sizing ---
    bankroll_usd: float = field(default_factory=lambda: _env_float("BANKROLL_USD", 1000.0))
    max_position_size: float = field(default_factory=lambda: _env_float("MAX_POSITION_SIZE", 0.05))  # 5% of bankroll (Kelly cap)
    max_total_exposure: float = field(default_factory=lambda: _env_float("MAX_TOTAL_EXPOSURE", 0.25))  # 25% of bankroll deployed
    max_asset_exposure: float = field(default_factory=lambda: _env_float("MAX_ASSET_EXPOSURE", 0.10))
    max_horizon_exposure: float = field(default_factory=lambda: _env_float("MAX_HORIZON_EXPOSURE", 0.15))
    max_trades_per_scan: int = field(default_factory=lambda: int(_env("MAX_TRADES_PER_SCAN", "2") or "2"))
    max_open_positions: int = field(default_factory=lambda: int(_env("MAX_OPEN_POSITIONS", "5") or "5"))
    max_open_positions_per_asset: int = field(default_factory=lambda: int(_env("MAX_OPEN_POSITIONS_PER_ASSET", "2") or "2"))
    max_open_positions_per_horizon: int = field(default_factory=lambda: int(_env("MAX_OPEN_POSITIONS_PER_HORIZON", "3") or "3"))
    position_cooldown_seconds: float = field(default_factory=lambda: _env_float("POSITION_COOLDOWN_SECONDS", 300.0))
    kelly_fraction: float = field(default_factory=lambda: _env_float("KELLY_FRACTION", 0.25))  # fractional Kelly when enabled
    max_kelly_fraction: float = field(default_factory=lambda: _env_float("MAX_KELLY_FRACTION", 0.25))

    # --- Edge / signal ---
    # Net edge (calibrated_prob - ask - fees - slippage) must meet this floor.
    min_entry_edge: float = field(default_factory=lambda: _env_float("MIN_ENTRY_EDGE", _env_float("MIN_EDGE_THRESHOLD", 0.08)))
    min_exit_edge: float = field(default_factory=lambda: _env_float("MIN_EXIT_EDGE", 0.015))
    min_confidence_score: float = field(default_factory=lambda: _env_float("MIN_CONFIDENCE_SCORE", 0.65))
    # Synth must be at least this far from 50% to enter. Filters weak signals where
    # Synth is only slightly above the coin-flip line. Set 0.0 to disable.
    # Recommended: 0.68 (only trade when Synth says >68% or <32%).
    min_synth_conviction: float = field(default_factory=lambda: _env_float("MIN_SYNTH_CONVICTION", 0.68))
    backtest_thresholds: tuple = (0.03, 0.05, 0.07, 0.10, 0.15, 0.20)
    # Per-asset edge overrides: thinner books (HYPE, SOL) require wider net edge.
    thin_market_assets: List[str] = field(default_factory=lambda: [
        a.strip().upper() for a in (_env("THIN_MARKET_ASSETS", "HYPE,SOL") or "").split(",") if a.strip()
    ])
    thin_market_min_edge: float = field(default_factory=lambda: _env_float("THIN_MARKET_MIN_EDGE", 0.08))

    # Liquidity-vs-size rule: required book depth >= position_size_usd * this multiple,
    # so we can enter without materially moving the price.
    liquidity_size_multiple: float = field(default_factory=lambda: _env_float("LIQUIDITY_SIZE_MULTIPLE", 5.0))

    # --- Market quality filters ---
    max_spread: float = field(default_factory=lambda: _env_float("MAX_SPREAD", 0.05))            # 5 cents
    min_liquidity: float = field(default_factory=lambda: _env_float("MIN_LIQUIDITY_USD", _env_float("MIN_LIQUIDITY", 50.0)))      # USD at best ask (top-of-book); live profile raises to $500
    min_volume: float = field(default_factory=lambda: _env_float("MIN_VOLUME", 1000.0))          # USD lifetime
    min_hours_to_resolution: float = field(default_factory=lambda: _env_float("MIN_HOURS_TO_RES", 0.0))
    max_entry_age_15m_sec: float = field(default_factory=lambda: _env_float("MAX_ENTRY_AGE_15M_SEC", 750.0))
    max_entry_age_1h_sec: float = field(default_factory=lambda: _env_float("MAX_ENTRY_AGE_1H_SEC", 3300.0))
    min_seconds_to_event_end: float = field(default_factory=lambda: _env_float("MIN_SECONDS_TO_EVENT_END", 180.0))

    # --- Execution mode ---
    # "maker"  → post limit orders (zero taker fee, 20% rebate share, adverse-selection risk)
    # "taker"  → take best ask (1.80% peak fee at 50¢, guaranteed fill)
    # Maker limit price offset from best bid — direct decimal units on $0–$1 contract.
    # 0.001 = one tenth of a cent above best bid.
    maker_entry_offset: float = field(default_factory=lambda: _env_float("MAKER_ENTRY_OFFSET", 0.001))
    # Polymarket pays makers 20% of taker fees collected in the same market (crypto category).
    maker_rebate_share: float = field(default_factory=lambda: _env_float("MAKER_REBATE_SHARE", 0.20))
    # Maker fill realism model for paper trading / backtesting.
    #   "optimistic"   — always assume fill (current default; overstates fill rate)
    #   "touch"        — fill only if best_ask <= limit_price at signal time (tight spread required)
    #   "next_tick"    — not implementable without multi-tick data; falls back to optimistic with warning
    #   "no_maker_fill"— maker orders never fill (control test; use to measure signal quality vs fills)
    maker_fill_model: str = field(default_factory=lambda: _env("MAKER_FILL_MODEL", "optimistic") or "optimistic")

    # --- Market-level entry guards ---
    one_position_per_market: bool = field(default_factory=lambda: _env_bool("ONE_POSITION_PER_MARKET", True))
    allow_reentry_after_exit: bool = field(default_factory=lambda: _env_bool("ALLOW_REENTRY_AFTER_EXIT", False))

    # --- Exit thresholds ---
    # Exit when raw remaining_edge = synth_prob − best_bid falls at or below this.
    exit_edge_threshold: float = field(default_factory=lambda: _env_float("EXIT_EDGE_THRESHOLD", 0.015))
    # Cancel/close maker positions when this few seconds remain before resolution.
    cancel_quotes_before_close_seconds: float = field(default_factory=lambda: _env_float("CANCEL_QUOTES_BEFORE_CLOSE_SECONDS", 75.0))
    # Minimum seconds to resolution required before opening a new entry.
    min_seconds_to_enter: float = field(default_factory=lambda: _env_float("MIN_SECONDS_TO_ENTER", _env_float("MIN_SECONDS_TO_EVENT_END", 90.0)))

    # --- Fee model ---
    # Crypto taker fee is price-dependent: fee = fee_rate × p × (1−p), peaks at 1.80% at 50¢.
    # fee_rate 0.072 gives max fee = 0.072 × 0.5 × 0.5 = 0.018 (1.80%).
    crypto_taker_fee_rate: float = field(default_factory=lambda: _env_float("CRYPTO_TAKER_FEE_RATE", 0.072))

    # --- Slippage model (paper trading) ---
    assumed_slippage_bps: float = field(default_factory=lambda: _env_float("ASSUMED_SLIPPAGE_BPS", 50.0))  # 0.5 cents on a $1 contract
    maker_fee_bps: float = field(default_factory=lambda: _env_float("MAKER_FEE_BPS", 0.0))
    taker_fee_bps: float = field(default_factory=lambda: _env_float("TAKER_FEE_BPS", 0.0))
    backtest_partial_fill_ratio: float = field(default_factory=lambda: _env_float("BACKTEST_PARTIAL_FILL_RATIO", 1.0))

    # --- Categories ---
    allowed_categories: List[str] = field(default_factory=lambda: [
        c.strip().lower() for c in (_env("ALLOWED_CATEGORIES", "crypto,sports,economics,politics,tech") or "").split(",") if c.strip()
    ])

    # --- Synth assets to trade ---
    synth_assets: List[str] = field(default_factory=lambda: [
        a.strip().upper() for a in (_env("SYNTH_ASSETS", "BTC,ETH,SOL,HYPE") or "").split(",") if a.strip()
    ])
    # Horizons (seconds). Supported insight endpoints: 900=15M, 3600=1H.
    synth_horizons_sec: List[int] = field(default_factory=lambda: [
        int(x) for x in (_env("SYNTH_HORIZONS_SEC", "900,3600") or "").split(",") if x.strip().isdigit()
    ])

    # --- API spend controls ---
    synth_cache_enabled: bool = field(default_factory=lambda: _env_bool("SYNTH_CACHE_ENABLED", True))
    synth_live_cache_ttl_sec: float = field(default_factory=lambda: _env_float("SYNTH_LIVE_CACHE_TTL_SEC", 60.0))
    synth_historical_cache_ttl_sec: float = field(default_factory=lambda: _env_float("SYNTH_HISTORICAL_CACHE_TTL_SEC", 604800.0))
    max_backtest_calls_without_confirm: int = field(default_factory=lambda: int(_env("MAX_BACKTEST_CALLS_WITHOUT_CONFIRM", "500") or "500"))
    use_real_two_sided_clob: bool = field(default_factory=lambda: _env_bool_any(
        ("REQUIRE_REAL_TWO_SIDED_CLOB", "USE_REAL_TWO_SIDED_CLOB", "USE_REAL_DOWN_CLOB"),
        True,
    ))
    require_real_two_sided_clob: bool = field(default_factory=lambda: _env_bool("REQUIRE_REAL_TWO_SIDED_CLOB", True))
    allow_complementary_book_fallback: bool = field(default_factory=lambda: _env_bool("ALLOW_COMPLEMENTARY_BOOK_FALLBACK", False))
    max_synth_staleness_sec: float = field(default_factory=lambda: _env_float("MAX_SYNTH_STALENESS_SEC", 90.0))
    max_clob_staleness_sec: float = field(default_factory=lambda: _env_float("MAX_CLOB_STALENESS_SEC", 30.0))
    max_backtest_snapshot_lag_sec: float = field(default_factory=lambda: _env_float("MAX_BACKTEST_SNAPSHOT_LAG_SEC", 90.0))
    allow_current_outcome_backtest_label: bool = field(default_factory=lambda: _env_bool("ALLOW_CURRENT_OUTCOME_BACKTEST_LABEL", False))

    # --- Calibration / segment risk ---
    calibration_db_path: str = field(default_factory=lambda: _env("CALIBRATION_DB_PATH", "polybot/data/calibration/observations.jsonl") or "polybot/data/calibration/observations.jsonl")
    calibration_report_dir: str = field(default_factory=lambda: _env("CALIBRATION_REPORT_DIR", "polybot/logs/calibration") or "polybot/logs/calibration")
    calibration_bins: int = field(default_factory=lambda: int(_env("CALIBRATION_BINS", "10") or "10"))
    min_calibration_samples_segment: int = field(default_factory=lambda: int(_env("MIN_CALIBRATION_SAMPLES_SEGMENT", "100") or "100"))
    min_calibration_samples_global: int = field(default_factory=lambda: int(_env("MIN_CALIBRATION_SAMPLES_GLOBAL", "300") or "300"))
    min_calibration_bin_samples: int = field(default_factory=lambda: int(_env("MIN_CALIBRATION_BIN_SAMPLES", "20") or "20"))
    calibration_shrinkage_k: float = field(default_factory=lambda: _env_float("CALIBRATION_SHRINKAGE_K", 50.0))
    min_calibration_samples: int = field(default_factory=lambda: int(_env("MIN_CALIBRATION_SAMPLES", "30") or "30"))
    confidence_min_samples: int = field(default_factory=lambda: int(_env("CONFIDENCE_MIN_SAMPLES", "50") or "50"))
    model_confidence_floor: float = field(default_factory=lambda: _env_float("MODEL_CONFIDENCE_FLOOR", 0.65))
    max_confidence_position_multiplier: float = field(default_factory=lambda: _env_float("MAX_CONFIDENCE_POSITION_MULTIPLIER", 1.5))
    segment_disable_min_samples: int = field(default_factory=lambda: int(_env("SEGMENT_DISABLE_MIN_SAMPLES", "50") or "50"))
    segment_disable_sharpe_below: float = field(default_factory=lambda: _env_float("SEGMENT_DISABLE_SHARPE_BELOW", 0.0))

    # --- Exit rules ---
    # Strategy B: only TIME_STOP (30s before resolution) and MODEL_REVERSAL fire.
    # SYNTH_EV_COLLAPSE is handled in evaluate_synth_updates().
    # EDGE_COLLAPSE and RANK_DECAY are disabled — market repricing toward Synth confirms the bet.
    time_stop_seconds: float = field(default_factory=lambda: _env_float("TIME_STOP_SECONDS", 30.0))
    # If a Synth P update causes position EV to fall to or below this level, exit immediately.
    min_ev_to_hold: float = field(default_factory=lambda: _env_float("MIN_EV_TO_HOLD", 0.00))
    # Order execution style: "taker" = aggressive fill (guaranteed, higher fee);
    # "maker" = post-only limit orders (zero taker fee, no fill guarantee).
    execution_mode: str = field(default_factory=lambda: _env("EXECUTION_MODE", "taker") or "taker")

    # --- Live trading safety ---
    allow_marketable_orders: bool = field(default_factory=lambda: _env_bool("ALLOW_MARKETABLE_ORDERS", False))

    # --- Local reporting ---
    report_timezone: str = field(default_factory=lambda: _env("REPORT_TIMEZONE", "America/New_York") or "America/New_York")
    report_dir: str = field(default_factory=lambda: _env("REPORT_DIR", "polybot/logs/reports") or "polybot/logs/reports")

    # --- Paths ---
    log_dir: str = field(default_factory=lambda: _env("LOG_DIR", "polybot/logs") or "polybot/logs")
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "polybot/data") or "polybot/data")
    snapshot_db_path: str = field(default_factory=lambda: _env("SNAPSHOT_DB_PATH", "polybot/data/snapshots.sqlite3") or "polybot/data/snapshots.sqlite3")
    positions_path: str = field(default_factory=lambda: _env("POSITIONS_PATH", "polybot/logs/positions.jsonl") or "polybot/logs/positions.jsonl")
    paper_position_size_usd: float = field(default_factory=lambda: _env_float("PAPER_POSITION_SIZE_USD", 10.0))
    orders_path: str = field(default_factory=lambda: _env("ORDERS_PATH", "polybot/logs/orders.jsonl") or "polybot/logs/orders.jsonl")
    adverse_selection_log_path: str = field(default_factory=lambda: _env("ADVERSE_SELECTION_LOG_PATH", "polybot/logs/adverse_selection.jsonl") or "polybot/logs/adverse_selection.jsonl")

    def assert_paper_only(self) -> None:
        if not self.paper_trade_mode or self.enable_live_trading:
            raise RuntimeError("Live trading is disabled. Require PAPER_TRADE_MODE=true and ENABLE_LIVE_TRADING=false.")

    def assert_live_allowed(self) -> None:
        if self.paper_trade_mode or not self.enable_live_trading:
            raise RuntimeError("Live trading requires PAPER_TRADE_MODE=false and ENABLE_LIVE_TRADING=true, and is not implemented yet.")


PAPER_PROFILE: Dict[str, Any] = {
    # Strategy B paper trading: taker fills, high conviction threshold, hold to resolution.
    "min_entry_edge": 0.08,
    "min_synth_conviction": 0.68,
    "min_liquidity": 50.0,
    "bankroll_usd": 1000.0,
    "max_position_size": 0.05,
    "execution_mode": "taker",
    "maker_fill_model": "optimistic",
    "one_position_per_market": True,
    "allow_reentry_after_exit": False,
    "time_stop_seconds": 30.0,
    "min_seconds_to_enter": 90.0,
    "max_entry_age_15m_sec": 750.0,
    "model_confidence_floor": 0.65,
    "calibration_shrinkage_k": 50.0,
}

LIVE_SAFE_PROFILE: Dict[str, Any] = {
    # Strategy B live: tighter thresholds for real-money caution.
    "min_entry_edge": 0.10,
    "min_synth_conviction": 0.70,
    "thin_market_min_edge": 0.12,
    "min_liquidity": 500.0,
    "max_position_size": 0.03,
    "kelly_fraction": 0.15,
    "bankroll_usd": 500.0,
    "execution_mode": "taker",
    "one_position_per_market": True,
    "allow_reentry_after_exit": False,
    "time_stop_seconds": 30.0,
    "min_seconds_to_enter": 90.0,
    "max_entry_age_15m_sec": 60.0,
    "model_confidence_floor": 0.65,
    "calibration_shrinkage_k": 50.0,
    "max_synth_staleness_sec": 45.0,
    "max_clob_staleness_sec": 15.0,
}

_PROFILES: Dict[str, Dict[str, Any]] = {
    "paper": PAPER_PROFILE,
    "live_safe": LIVE_SAFE_PROFILE,
}


def apply_profile(cfg: "Config", profile_name: str) -> None:
    """Apply named profile overrides to a Config instance.

    Env vars set before process start always win — profile fills in defaults
    for fields that were not explicitly overridden via env.
    """
    profile = _PROFILES.get(profile_name.lower().replace("-", "_"))
    if not profile:
        raise ValueError(
            f"Unknown profile {profile_name!r}. Available: {sorted(_PROFILES)}"
        )
    for field_name, value in profile.items():
        if hasattr(cfg, field_name):
            setattr(cfg, field_name, value)


CONFIG = Config()
