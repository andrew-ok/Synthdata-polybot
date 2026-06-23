"""Probability calibration and segment performance.

The calibration store is intentionally file-backed JSONL. Each observation is
one historical prediction for an asset/horizon and the final realized outcome.
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
        members = [
            r for r in rows
            if lo <= r.predicted_probability < hi or (idx == n_bins - 1 and r.predicted_probability <= hi)
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
    def __init__(self, observations: Optional[List[CalibrationObservation]] = None):
        self.observations = observations if observations is not None else load_observations()
        self._by_segment: Dict[Tuple[str, str], List[CalibrationObservation]] = defaultdict(list)
        for row in self.observations:
            if row.realized_outcome in ("UP", "DOWN"):
                self._by_segment[_segment_key(row.asset, row.horizon)].append(row)
        self._metrics = segment_metrics(self.observations)

    def calibrate(self, asset: str, horizon: str, predicted_probability: float) -> float:
        p = _clamp(predicted_probability)
        rows = self._by_segment.get(_segment_key(asset, horizon), [])
        if len(rows) < CONFIG.min_calibration_samples:
            return p

        n_bins = max(2, CONFIG.calibration_bins)
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        lo = idx / n_bins
        hi = (idx + 1) / n_bins
        members = [
            r for r in rows
            if lo <= r.predicted_probability < hi or (idx == n_bins - 1 and r.predicted_probability <= hi)
        ]
        if len(members) < CONFIG.min_calibration_samples:
            return p

        observed = sum(1 for r in members if r.realized_outcome == "UP") / len(members)
        shrink = min(1.0, len(members) / max(CONFIG.min_calibration_samples * 3, 1))
        return _clamp((observed * shrink) + (p * (1.0 - shrink)))

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
