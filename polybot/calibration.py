"""Probability calibration and segment performance.

The calibration store is intentionally file-backed JSONL. Each observation is
one historical prediction for an asset/horizon and the final realized outcome.

Live vs backtest split
----------------------
Historical backtests flood the observation file with stale-archive data that
shows ~50% win rate at every conviction level (Synth signal is already repriced
before archive queries). Loading those observations into the live Calibrator
shrinks every DOWN edge toward 50%, destroying apparent alpha.

``Calibrator(live_only=True)`` (the default) filters out all backtest-sourced
rows and falls back to ``raw_low_sample`` (fair = raw Synth probability) until
live fills accumulate. This is correct: we're in a cold-start regime.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import CONFIG

# Sources produced by offline backtesting — these observations use stale
# archive data where Synth's signal is already repriced into the market,
# making them uninformative for live calibration.
_BACKTEST_SOURCES: frozenset = frozenset({
    "backtest",
    "backtest_signal",
    "historical_synth",
})


@dataclass
class CalibrationObservation:
    timestamp: str
    asset: str
    horizon: str
    predicted_probability: float
    realized_outcome: str
    ask_price: Optional[float] = None
    side: str = "UP"
    source: str = "historical_synth"
    # RL feature: seconds into the market window when the position was entered.
    # Early-window entries (120–300s) may have better calibration than late entries.
    entry_window_age_sec: Optional[float] = None


@dataclass
class CalibrationResult:
    raw_synth_probability: float
    fair_probability: float
    calibration_method: str
    sample_count: int
    confidence_score: float
    calibration_error_estimate: float


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _is_win(side: str, realized_outcome: str) -> bool:
    side = side.upper()
    outcome = realized_outcome.upper()
    return (side == "UP" and outcome == "UP") or (side == "DOWN" and outcome == "DOWN")


def append_observation(obs: CalibrationObservation) -> None:
    os.makedirs(os.path.dirname(CONFIG.calibration_db_path), exist_ok=True)
    with open(CONFIG.calibration_db_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(obs), sort_keys=True) + "\n")


def load_observations() -> List[CalibrationObservation]:
    path = CONFIG.calibration_db_path
    if not os.path.exists(path):
        return []
    rows: List[CalibrationObservation] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rows.append(CalibrationObservation(
                    timestamp=str(data.get("timestamp") or ""),
                    asset=str(data.get("asset") or "").upper(),
                    horizon=str(data.get("horizon") or ""),
                    predicted_probability=float(data.get("predicted_probability")),
                    realized_outcome=str(data.get("realized_outcome") or "").upper(),
                    ask_price=_optfloat(data.get("ask_price")),
                    side=str(data.get("side") or "UP").upper(),
                    source=str(data.get("source") or "historical_synth"),
                ))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return rows


def _optfloat(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _segment_key(asset: str, horizon: str) -> Tuple[str, str]:
    return asset.upper(), horizon


def _bin_index(probability: float) -> int:
    n_bins = max(2, CONFIG.calibration_bins)
    return min(n_bins - 1, max(0, int(_clamp(probability) * n_bins)))


def _bin_members(rows: List[CalibrationObservation], probability: float) -> List[CalibrationObservation]:
    idx = _bin_index(probability)
    n_bins = max(2, CONFIG.calibration_bins)
    lo = idx / n_bins
    hi = (idx + 1) / n_bins
    last_bin = idx == n_bins - 1
    return [
        r for r in rows
        if lo <= r.predicted_probability < hi or (last_bin and r.predicted_probability >= lo)
    ]


def _bin_members_adaptive(
    rows: List[CalibrationObservation],
    probability: float,
    min_samples: int,
) -> Tuple[List[CalibrationObservation], str]:
    """Expand outward from the exact bin until min_samples are found.

    Returns (members, suffix) where suffix is:
      "bin"          — exact target bin already has enough samples
      "adaptive_bin" — reached min_samples by expanding to neighboring bins
      "all"          — full expansion exhausted; returned all rows regardless
    """
    n_bins = max(2, CONFIG.calibration_bins)
    idx = _bin_index(probability)

    for radius in range(n_bins):
        lo_idx = max(0, idx - radius)
        hi_idx = min(n_bins - 1, idx + radius)
        lo = lo_idx / n_bins
        hi = (hi_idx + 1) / n_bins
        last_bin = hi_idx == n_bins - 1
        members = [
            r for r in rows
            if lo <= r.predicted_probability < hi or (last_bin and r.predicted_probability >= lo)
        ]
        if len(members) >= min_samples:
            return members, ("bin" if radius == 0 else "adaptive_bin")

    return rows, "all"


def _returns(rows: Iterable[CalibrationObservation]) -> List[float]:
    out: List[float] = []
    for row in rows:
        if row.ask_price is None or row.ask_price <= 0 or row.ask_price >= 1:
            continue
        won = _is_win(row.side, row.realized_outcome)
        out.append((1.0 - row.ask_price) if won else -row.ask_price)
    return out


def _sharpe(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    std = math.sqrt(var)
    return (mean / std) * math.sqrt(len(values)) if std > 1e-9 else 0.0


def _brier(rows: List[CalibrationObservation]) -> float:
    if not rows:
        return 0.0
    err = 0.0
    for row in rows:
        y = 1.0 if row.realized_outcome == "UP" else 0.0
        err += (row.predicted_probability - y) ** 2
    return err / len(rows)


def _bins(rows: List[CalibrationObservation]) -> List[Dict[str, float]]:
    n_bins = max(2, CONFIG.calibration_bins)
    bins: List[Dict[str, float]] = []
    for idx in range(n_bins):
        lo = idx / n_bins
        hi = (idx + 1) / n_bins
        is_last = idx == n_bins - 1
        members = [
            r for r in rows
            if lo <= r.predicted_probability < hi or (is_last and r.predicted_probability >= lo)
        ]
        obs = sum(1 for r in members if r.realized_outcome == "UP")
        count = len(members)
        bins.append({
            "bin_low": round(lo, 4),
            "bin_high": round(hi, 4),
            "count": count,
            "mean_predicted": round(sum(r.predicted_probability for r in members) / count, 4) if count else 0.0,
            "observed_frequency": round(obs / count, 4) if count else 0.0,
        })
    return bins


def segment_metrics(rows: Optional[List[CalibrationObservation]] = None) -> Dict[str, Dict[str, Any]]:
    rows = rows if rows is not None else load_observations()
    grouped: Dict[Tuple[str, str], List[CalibrationObservation]] = defaultdict(list)
    for row in rows:
        if row.realized_outcome not in ("UP", "DOWN"):
            continue
        grouped[_segment_key(row.asset, row.horizon)].append(row)

    metrics: Dict[str, Dict[str, Any]] = {}
    for (asset, horizon), segment_rows in sorted(grouped.items()):
        wins = sum(1 for row in segment_rows if _is_win(row.side, row.realized_outcome))
        returns = _returns(segment_rows)
        sharpe = _sharpe(returns)
        key = f"{asset}-{horizon}"
        metrics[key] = {
            "asset": asset,
            "horizon": horizon,
            "samples": len(segment_rows),
            "win_rate": round(wins / len(segment_rows), 4) if segment_rows else 0.0,
            "brier_score": round(_brier(segment_rows), 6),
            "sharpe": round(sharpe, 4),
            "pnl": round(sum(returns), 4),
            "reliability_curve": _bins(segment_rows),
            "disabled": (
                len(segment_rows) >= CONFIG.segment_disable_min_samples
                and sharpe < CONFIG.segment_disable_sharpe_below
            ),
        }
    return metrics


class Calibrator:
    def __init__(
        self,
        observations: Optional[List[CalibrationObservation]] = None,
        live_only: bool = True,
    ):
        all_obs = observations if observations is not None else load_observations()
        self._all_count = len(all_obs)
        if live_only:
            self.observations = [r for r in all_obs if r.source not in _BACKTEST_SOURCES]
        else:
            self.observations = all_obs
        self._live_only = live_only
        self._by_segment: Dict[Tuple[str, str], List[CalibrationObservation]] = defaultdict(list)
        self._by_segment_side: Dict[Tuple[str, str, str], List[CalibrationObservation]] = defaultdict(list)
        self._global_by_side: Dict[str, List[CalibrationObservation]] = defaultdict(list)
        for row in self.observations:
            if row.realized_outcome in ("UP", "DOWN"):
                self._by_segment[_segment_key(row.asset, row.horizon)].append(row)
                self._by_segment_side[(row.asset.upper(), row.horizon, row.side.upper())].append(row)
                self._global_by_side[row.side.upper()].append(row)
        self._metrics = segment_metrics(self.observations)

    @property
    def live_count(self) -> int:
        return len(self.observations)

    @property
    def calibration_mode(self) -> str:
        n = self.live_count
        if n == 0:
            return "cold_start"
        if n < CONFIG.min_calibration_samples:
            return f"warming_up({n})"
        return f"live({n})"

    def calibrate_side(self, asset: str, horizon: str, side: str, predicted_probability: float) -> CalibrationResult:
        p = _clamp(predicted_probability)
        side = side.upper()
        # All fallback tiers filter to the same side so UP/DOWN observations are
        # never mixed. A DOWN predicted_probability of 0.65 means something
        # different from an UP predicted_probability of 0.65 (the equivalent
        # DOWN probability would be 1 - 0.65 = 0.35), so cross-side rows cannot
        # be compared directly in the same bin without transformation.
        seg_same_side = [
            r for r in self._by_segment.get(_segment_key(asset, horizon), [])
            if r.side.upper() == side
        ]
        # tier: (method_base, rows, total_gate)
        # total_gate: minimum total same-side rows needed before this tier is used.
        # min_calibration_samples_segment and min_calibration_samples_global are
        # segment-level gates, NOT per-bin requirements. Per-bin/local requirements
        # use min_calibration_bin_samples, which is much lower.
        candidates = [
            ("asset_horizon_side", self._by_segment_side.get((asset.upper(), horizon, side), []), CONFIG.min_calibration_samples_segment),
            ("asset_horizon_same_side", seg_same_side, CONFIG.min_calibration_samples_segment),
            ("global_same_side", self._global_by_side.get(side, []), CONFIG.min_calibration_samples_global),
        ]
        bin_min = CONFIG.min_calibration_bin_samples
        for tier_base, rows, total_gate in candidates:
            if len(rows) < total_gate:
                continue
            # Try exact bin first, then expand to neighboring bins.
            local_members, bin_suffix = _bin_members_adaptive(rows, p, bin_min)
            local_satisfied = len(local_members) >= bin_min
            if local_satisfied:
                members = local_members
                method = f"{tier_base}_{bin_suffix}"
            else:
                # Enough segment-wide data but no local samples in range;
                # use all same-side segment rows as a broad fallback.
                members = rows
                method = f"{tier_base}_all"
            observed = sum(1 for r in members if _is_win(side, r.realized_outcome)) / len(members)
            shrink = len(members) / (len(members) + max(CONFIG.calibration_shrinkage_k, 1.0))
            fair = _clamp((observed * shrink) + (p * (1.0 - shrink)))
            error = abs(fair - observed)
            # Confidence rises above the floor only when both conditions are met:
            # 1. Segment-level total clears total_gate (seg_score = 1.0 when satisfied).
            # 2. Local/adaptive sample count clears bin_min (local_score = 1.0 when satisfied).
            # The "_all" fallback reduces local_score proportionally since local_members
            # did not reach bin_min.
            seg_score = min(1.0, len(rows) / max(total_gate, 1))
            local_score = min(1.0, len(local_members) / max(bin_min, 1))
            error_score = 1.0 - min(1.0, error * 2.0)
            confidence = _clamp(
                max(CONFIG.model_confidence_floor, seg_score * local_score * error_score),
                CONFIG.model_confidence_floor,
            )
            return CalibrationResult(
                raw_synth_probability=p,
                fair_probability=fair,
                calibration_method=method,
                sample_count=len(members),
                confidence_score=confidence,
                calibration_error_estimate=error,
            )
        return CalibrationResult(
            raw_synth_probability=p,
            fair_probability=p,
            calibration_method="raw_low_sample",
            sample_count=0,
            confidence_score=CONFIG.model_confidence_floor,
            calibration_error_estimate=1.0,
        )

    def calibrate(self, asset: str, horizon: str, predicted_probability: float) -> float:
        return self.calibrate_side(asset, horizon, "UP", predicted_probability).fair_probability

    def model_confidence(self, asset: str, horizon: str) -> float:
        key = f"{asset.upper()}-{horizon}"
        m = self._metrics.get(key)
        if not m:
            return CONFIG.model_confidence_floor
        samples = float(m["samples"])
        sample_score = min(1.0, samples / max(CONFIG.confidence_min_samples, 1))
        sharpe_score = _clamp((float(m["sharpe"]) + 1.0) / 2.0)
        return _clamp(
            CONFIG.model_confidence_floor + sample_score * sharpe_score,
            CONFIG.model_confidence_floor,
            CONFIG.max_confidence_position_multiplier,
        )

    def segment_disabled(self, asset: str, horizon: str) -> bool:
        key = f"{asset.upper()}-{horizon}"
        return bool(self._metrics.get(key, {}).get("disabled", False))

    @property
    def metrics(self) -> Dict[str, Dict[str, Any]]:
        return self._metrics


def write_calibration_report() -> Dict[str, str]:
    rows = load_observations()
    metrics = segment_metrics(rows)
    os.makedirs(CONFIG.calibration_report_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = os.path.join(CONFIG.calibration_report_dir, f"calibration-{ts}.json")
    md_path = os.path.join(CONFIG.calibration_report_dir, f"calibration-{ts}.md")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observations": len(rows),
        "segments": metrics,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")

    lines = [
        f"# Calibration Report - {payload['generated_at']}",
        "",
        f"Observations: {len(rows)}",
        "",
        "| Segment | Samples | Win rate | Brier | Sharpe | PnL | Disabled |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for segment, m in metrics.items():
        lines.append(
            f"| {segment} | {m['samples']} | {m['win_rate']:.2%} | "
            f"{m['brier_score']:.4f} | {m['sharpe']:.3f} | {m['pnl']:.2f} | {m['disabled']} |"
        )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"markdown": md_path, "json": json_path}
