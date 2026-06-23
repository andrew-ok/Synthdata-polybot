"""Configuration for the Polymarket x Synth paper-trading bot.

All secrets come from environment variables. Never hardcode keys.
Paper-trade mode is ON by default. Live trading requires an explicit override.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


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
    max_position_size: float = field(default_factory=lambda: _env_float("MAX_POSITION_SIZE", 0.01))  # 1% of bankroll
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
    # Hard rules:
    #   raw edge (synth_prob - ask) must be >= min_edge_threshold (20pp default)
    #   net edge (raw - fees - slippage)  must be >= min_net_edge_threshold (12pp default)
    min_entry_edge: float = field(default_factory=lambda: _env_float("MIN_ENTRY_EDGE", _env_float("MIN_EDGE_THRESHOLD", 0.06)))
    min_exit_edge: float = field(default_factory=lambda: _env_float("MIN_EXIT_EDGE", 0.015))
    min_confidence_score: float = field(default_factory=lambda: _env_float("MIN_CONFIDENCE_SCORE", 0.50))
    min_edge_threshold: float = field(default_factory=lambda: _env_float("MIN_EDGE_THRESHOLD", _env_float("MIN_ENTRY_EDGE", 0.06)))
    min_net_edge_threshold: float = field(default_factory=lambda: _env_float("MIN_NET_EDGE_THRESHOLD", _env_float("MIN_ENTRY_EDGE", 0.06)))
    backtest_thresholds: tuple = (0.10, 0.15, 0.20, 0.25, 0.30)

    # Liquidity-vs-size rule: required book depth >= position_size_usd * this multiple,
    # so we can enter without materially moving the price.
    liquidity_size_multiple: float = field(default_factory=lambda: _env_float("LIQUIDITY_SIZE_MULTIPLE", 5.0))

    # --- Market quality filters ---
    max_spread: float = field(default_factory=lambda: _env_float("MAX_SPREAD", 0.05))            # 5 cents
    min_liquidity: float = field(default_factory=lambda: _env_float("MIN_LIQUIDITY_USD", _env_float("MIN_LIQUIDITY", 500.0)))     # USD on the relevant side
    min_volume: float = field(default_factory=lambda: _env_float("MIN_VOLUME", 1000.0))          # USD lifetime
    min_hours_to_resolution: float = field(default_factory=lambda: _env_float("MIN_HOURS_TO_RES", 0.0))
    max_entry_age_15m_sec: float = field(default_factory=lambda: _env_float("MAX_ENTRY_AGE_15M_SEC", 180.0))
    max_entry_age_1h_sec: float = field(default_factory=lambda: _env_float("MAX_ENTRY_AGE_1H_SEC", 600.0))
    min_seconds_to_event_end: float = field(default_factory=lambda: _env_float("MIN_SECONDS_TO_EVENT_END", 180.0))

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
    calibration_shrinkage_k: float = field(default_factory=lambda: _env_float("CALIBRATION_SHRINKAGE_K", 200.0))
    min_calibration_samples: int = field(default_factory=lambda: int(_env("MIN_CALIBRATION_SAMPLES", "30") or "30"))
    confidence_min_samples: int = field(default_factory=lambda: int(_env("CONFIDENCE_MIN_SAMPLES", "50") or "50"))
    model_confidence_floor: float = field(default_factory=lambda: _env_float("MODEL_CONFIDENCE_FLOOR", 0.25))
    max_confidence_position_multiplier: float = field(default_factory=lambda: _env_float("MAX_CONFIDENCE_POSITION_MULTIPLIER", 1.5))
    segment_disable_min_samples: int = field(default_factory=lambda: int(_env("SEGMENT_DISABLE_MIN_SAMPLES", "50") or "50"))
    segment_disable_sharpe_below: float = field(default_factory=lambda: _env_float("SEGMENT_DISABLE_SHARPE_BELOW", 0.0))

    # --- Exit rules ---
    take_profit_edge_collapse: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_EDGE_COLLAPSE", 0.02))
    model_reversal_exit: bool = field(default_factory=lambda: _env_bool("MODEL_REVERSAL_EXIT", True))
    time_stop_seconds: float = field(default_factory=lambda: _env_float("TIME_STOP_SECONDS", 120.0))

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

    def assert_paper_only(self) -> None:
        if not self.paper_trade_mode or self.enable_live_trading:
            raise RuntimeError("Live trading is disabled. Require PAPER_TRADE_MODE=true and ENABLE_LIVE_TRADING=false.")

    def assert_live_allowed(self) -> None:
        if self.paper_trade_mode or not self.enable_live_trading:
            raise RuntimeError("Live trading requires PAPER_TRADE_MODE=false and ENABLE_LIVE_TRADING=true, and is not implemented yet.")


CONFIG = Config()
