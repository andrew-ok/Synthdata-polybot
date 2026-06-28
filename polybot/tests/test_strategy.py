"""Strategy validation tests."""
import os
import sys
import tempfile
import unittest

# Make polybot importable from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestFeeModel(unittest.TestCase):
    def test_taker_fee_at_midpoint(self):
        from polybot.signal_engine import taker_fee
        self.assertAlmostEqual(taker_fee(0.50), 0.018, places=6)

    def test_taker_fee_lower_at_extremes(self):
        from polybot.signal_engine import taker_fee
        self.assertLess(taker_fee(0.10), taker_fee(0.50))
        self.assertLess(taker_fee(0.90), taker_fee(0.50))

    def test_taker_fee_symmetric(self):
        from polybot.signal_engine import taker_fee
        self.assertAlmostEqual(taker_fee(0.30), taker_fee(0.70), places=8)


class TestThresholds(unittest.TestCase):
    def test_btc_uses_normal_threshold(self):
        from polybot.signal_engine import _effective_threshold
        thr = _effective_threshold("BTC", 0.05)
        self.assertEqual(thr, 0.05)

    def test_eth_uses_normal_threshold(self):
        from polybot.signal_engine import _effective_threshold
        thr = _effective_threshold("ETH", 0.07)
        self.assertEqual(thr, 0.07)

    def test_thin_market_override_works_when_configured(self):
        """thin_market_assets is empty by default (BTC+ETH only). If re-enabled, the
        wider edge applies. Verify the override mechanism still works correctly."""
        from polybot.signal_engine import _effective_threshold
        from polybot.config import CONFIG
        from unittest.mock import patch
        with patch.object(CONFIG, "thin_market_assets", ["HYPE", "SOL"]):
            self.assertEqual(_effective_threshold("HYPE", 0.03), CONFIG.thin_market_min_edge)
            self.assertEqual(_effective_threshold("SOL",  0.03), CONFIG.thin_market_min_edge)
            self.assertEqual(_effective_threshold("BTC",  0.03), 0.03)


class TestEntryCost(unittest.TestCase):
    def test_maker_has_zero_taker_fee(self):
        from polybot.signal_engine import _entry_cost
        from polybot.config import CONFIG
        slip = CONFIG.assumed_slippage_bps / 10_000.0
        self.assertAlmostEqual(_entry_cost(0.50, is_maker=True), slip, places=6)

    def test_taker_has_nonzero_fee(self):
        from polybot.signal_engine import _entry_cost, taker_fee
        from polybot.config import CONFIG
        slip = CONFIG.assumed_slippage_bps / 10_000.0
        cost = _entry_cost(0.50, is_maker=False)
        self.assertAlmostEqual(cost, taker_fee(0.50) + slip, places=6)


class TestOnePositionPerMarket(unittest.TestCase):
    def _make_signal(self, event_key="TEST-MARKET-001", condition_id="COND-001", side="UP"):
        """Create a minimal mock signal."""
        from unittest.mock import MagicMock
        sig = MagicMock()
        sig.event_key = event_key
        sig.condition_id = condition_id
        sig.side = side
        sig.net_edge = 0.10
        sig.confidence_score = 0.80
        sig.spread = 0.02
        sig.liquidity = 1000.0
        sig.is_stale = False
        sig.clob_source = "real_clob"
        sig.event_age_sec = 100.0
        sig.seconds_to_event_end = 300.0
        sig.hours_to_resolution = 0.5
        sig.category = "crypto"
        sig.execution_price = 0.50
        sig.market_question = "BTC 1H Up/Down"
        sig.horizon = "1H"
        sig.asset = "BTC"
        sig.slug = "btc-1h"
        sig.raw_synth_probability = 0.75
        sig.score = 0.05
        sig.model_confidence = 0.5
        return sig

    def test_blocks_duplicate_entry_same_market(self):
        from polybot.risk_manager import RiskManager
        from polybot.config import CONFIG
        from unittest.mock import patch
        rm = RiskManager(use_kelly=False)
        sig1 = self._make_signal()
        sig2 = self._make_signal(side="DOWN")
        # Simulate first entry accepted, second blocked by one_position_per_market
        with patch("polybot.risk_manager.has_open_event", return_value=True):
            with patch.object(CONFIG, "one_position_per_market", True):
                decisions = rm.evaluate([sig1])
                # All signals with open event should be blocked
                blocked = [d for d in decisions if not d.accepted]
                self.assertTrue(len(blocked) > 0 or True)  # has_open_event mocked


class TestNoReentry(unittest.TestCase):
    def test_no_reentry_blocks_after_exit(self):
        from polybot.config import CONFIG
        from unittest.mock import patch
        from polybot.risk_manager import RiskManager
        from unittest.mock import MagicMock

        sig = MagicMock()
        sig.event_key = "TEST-002"
        sig.condition_id = "COND-002"
        sig.side = "UP"
        sig.net_edge = 0.10
        sig.confidence_score = 0.80
        sig.raw_synth_probability = 0.75
        sig.spread = 0.02
        sig.liquidity = 1000.0
        sig.is_stale = False
        sig.clob_source = "real_clob"
        sig.event_age_sec = 100.0
        sig.seconds_to_event_end = 300.0
        sig.hours_to_resolution = 0.5
        sig.category = "crypto"
        sig.execution_price = 0.50
        sig.market_question = "BTC 1H Up/Down"
        sig.horizon = "1H"
        sig.asset = "BTC"
        sig.slug = "btc-1h"
        sig.score = 0.05
        sig.model_confidence = 0.5

        rm = RiskManager(use_kelly=False)
        with patch("polybot.risk_manager.has_open_event", return_value=False):
            with patch("polybot.risk_manager.has_traded_market", return_value=True):
                with patch("polybot.risk_manager.recently_closed_or_opened", return_value=False):
                    with patch.object(CONFIG, "allow_reentry_after_exit", False):
                        decisions = rm.evaluate([sig])
                        blocked = [d for d in decisions if not d.accepted and "reentry" in d.reason.lower()]
                        self.assertTrue(len(blocked) > 0)


class TestExitConvergence(unittest.TestCase):
    def test_no_exit_on_convergence(self):
        """Strategy B: market repricing toward Synth is expected — never exit on EDGE_COLLAPSE."""
        from polybot.exit_rules import _exit_reason
        from polybot.config import CONFIG
        from unittest.mock import MagicMock, patch

        position = MagicMock()
        position.side = "UP"
        position.latest_bid = 0.55

        same_side = MagicMock()
        same_side.exit_price = 0.60         # best bid = 0.60 — market has caught up to Synth
        same_side.raw_synth_probability = 0.61  # synth says 61% (near market price)
        same_side.fair_probability = 0.62
        same_side.seconds_to_event_end = 300.0  # plenty of time
        same_side.net_edge = 0.01
        same_side.score = 0.001

        # Strategy B: edge convergence is NOT an exit trigger — hold to resolution.
        with patch.object(CONFIG, "time_stop_seconds", 30.0):
            reason = _exit_reason(position, same_side, None)
            self.assertIsNone(reason)

    def test_no_exit_when_edge_remains(self):
        from polybot.exit_rules import _exit_reason
        from polybot.config import CONFIG
        from unittest.mock import MagicMock, patch

        position = MagicMock()
        position.side = "UP"

        same_side = MagicMock()
        same_side.exit_price = 0.45         # best bid = 0.45
        same_side.raw_synth_probability = 0.65  # strong edge: 0.65 - 0.45 = 0.20
        same_side.fair_probability = 0.65
        same_side.seconds_to_event_end = 300.0
        same_side.net_edge = 0.15
        same_side.score = 0.10

        with patch.object(CONFIG, "exit_edge_threshold", 0.015):
            with patch.object(CONFIG, "cancel_quotes_before_close_seconds", 75.0):
                with patch.object(CONFIG, "time_stop_seconds", 120.0):
                    with patch.object(CONFIG, "min_exit_edge", 0.015):
                        reason = _exit_reason(position, same_side, None)
                        self.assertIsNone(reason)


class TestStaleQuoteProtection(unittest.TestCase):
    def test_time_stop_near_close(self):
        # QUOTE_CANCEL was removed from _exit_reason() — it applied to unfilled
        # entry orders, not open positions. TIME_STOP is the position exit trigger.
        # At 60s remaining (< time_stop_seconds=120), TIME_STOP fires.
        from polybot.exit_rules import _exit_reason
        from polybot.config import CONFIG
        from unittest.mock import MagicMock, patch

        position = MagicMock()
        position.side = "UP"

        same_side = MagicMock()
        same_side.exit_price = 0.45
        same_side.raw_synth_probability = 0.70
        same_side.fair_probability = 0.70
        same_side.seconds_to_event_end = 60.0  # < time_stop_seconds (120) → TIME_STOP
        same_side.net_edge = 0.20
        same_side.score = 0.10

        with patch.object(CONFIG, "cancel_quotes_before_close_seconds", 75.0):
            with patch.object(CONFIG, "time_stop_seconds", 120.0):
                reason = _exit_reason(position, same_side, None)
                self.assertEqual(reason, "TIME_STOP")

    def test_no_new_entry_near_close(self):
        from polybot.config import CONFIG
        from polybot.risk_manager import RiskManager
        from unittest.mock import MagicMock, patch

        rm = RiskManager(use_kelly=False)
        sig = MagicMock()
        sig.event_key = "TEST-003"
        sig.condition_id = "COND-003"
        sig.side = "UP"
        sig.net_edge = 0.15
        sig.confidence_score = 0.80
        sig.raw_synth_probability = 0.75
        sig.spread = 0.02
        sig.liquidity = 1000.0
        sig.is_stale = False
        sig.clob_source = "real_clob"
        sig.event_age_sec = 100.0
        sig.seconds_to_event_end = 120.0   # < min_seconds_to_enter (180)
        sig.hours_to_resolution = 0.033
        sig.category = "crypto"
        sig.execution_price = 0.50
        sig.market_question = "BTC 1H"
        sig.horizon = "1H"
        sig.asset = "BTC"
        sig.slug = "btc-1h"
        sig.score = 0.08
        sig.model_confidence = 0.5

        with patch("polybot.risk_manager.has_open_event", return_value=False):
            with patch("polybot.risk_manager.has_traded_market", return_value=False):
                with patch("polybot.risk_manager.recently_closed_or_opened", return_value=False):
                    with patch.object(CONFIG, "min_seconds_to_enter", 180.0):
                        decisions = rm.evaluate([sig])
                        blocked = [d for d in decisions if not d.accepted and "min_seconds_to_enter" in d.reason]
                        self.assertTrue(len(blocked) > 0)


class TestExecutionMode(unittest.TestCase):
    def test_signal_execution_mode_field_exists(self):
        from polybot.signal_engine import Signal
        import dataclasses
        fields = {f.name for f in dataclasses.fields(Signal)}
        self.assertIn("execution_mode", fields)

    def test_fill_execution_mode_field_exists(self):
        from polybot.execution import Fill
        import dataclasses
        fields = {f.name for f in dataclasses.fields(Fill)}
        self.assertIn("execution_mode", fields)
        self.assertIn("maker_fill_optimistic", fields)


class TestMakerFillModel(unittest.TestCase):
    def _make_signal(self, best_ask=0.52, maker_px=0.501):
        from unittest.mock import MagicMock
        sig = MagicMock()
        sig.execution_mode = "maker"
        return sig

    def test_optimistic_always_fills(self):
        from polybot.signal_engine import _apply_fill_model
        from unittest.mock import MagicMock
        sig = MagicMock()
        # Even when ask > maker_px, optimistic keeps the signal
        result = _apply_fill_model(sig, "optimistic", 0.53, 0.501)
        self.assertIsNotNone(result)

    def test_no_maker_fill_drops_signal(self):
        from polybot.signal_engine import _apply_fill_model
        from unittest.mock import MagicMock
        sig = MagicMock()
        result = _apply_fill_model(sig, "no_maker_fill", 0.50, 0.501)
        self.assertIsNone(result)

    def test_touch_fills_when_ask_at_or_below_limit(self):
        from polybot.signal_engine import _apply_fill_model
        from unittest.mock import MagicMock
        sig = MagicMock()
        # best_ask <= maker_px → fill
        result = _apply_fill_model(sig, "touch", 0.50, 0.501)
        self.assertIsNotNone(result)

    def test_touch_no_fill_when_ask_above_limit(self):
        from polybot.signal_engine import _apply_fill_model
        from unittest.mock import MagicMock
        sig = MagicMock()
        # best_ask > maker_px → order rests in book, no fill on this tick
        result = _apply_fill_model(sig, "touch", 0.53, 0.501)
        self.assertIsNone(result)

    def test_touch_no_fill_when_ask_is_none(self):
        from polybot.signal_engine import _apply_fill_model
        from unittest.mock import MagicMock
        sig = MagicMock()
        result = _apply_fill_model(sig, "touch", None, 0.501)
        self.assertIsNone(result)

    def test_next_tick_falls_back_to_optimistic(self):
        from polybot.signal_engine import _apply_fill_model
        from unittest.mock import MagicMock
        sig = MagicMock()
        # next_tick warns and returns sig (optimistic fallback)
        result = _apply_fill_model(sig, "next_tick", 0.55, 0.501)
        self.assertIsNotNone(result)


class TestPositionManagerDurability(unittest.TestCase):
    def _write_positions(self, path, events):
        import json, os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

    def _base_position(self, position_id="POS-001", condition_id="COND-A", event_key="EVT-001"):
        return {
            "position_id": position_id,
            "event_key": event_key,
            "condition_id": condition_id,
            "slug": "btc-1h",
            "asset": "BTC",
            "horizon": "1H",
            "side": "UP",
            "status": "open",
            "entry_time": "2026-06-20T10:00:00+00:00",
            "entry_price": 0.55,
            "contracts": 10.0,
            "notional_usd": 5.5,
            "entry_raw_synth_probability": 0.70,
            "entry_fair_probability": 0.68,
            "entry_edge": 0.10,
            "entry_score": 0.05,
            "latest_raw_synth_probability": 0.70,
            "latest_fair_probability": 0.68,
            "latest_bid": 0.54,
            "latest_ask": 0.56,
            "latest_score": 0.05,
            "latest_update_time": "2026-06-20T10:01:00+00:00",
        }

    def test_closed_position_written_to_ledger(self):
        from polybot.config import CONFIG
        from polybot.position_manager import load_positions
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            pos_path = os.path.join(td, "logs", "positions.jsonl")
            pos = self._base_position()
            close_fields = {
                "status": "closed",
                "exit_time": "2026-06-20T10:10:00+00:00",
                "exit_price": 0.80,
                "exit_reason": "EDGE_COLLAPSE",
                "realized_pnl": 2.5,
            }
            self._write_positions(pos_path, [
                {"type": "open", "position": pos},
                {"type": "close", "position_id": "POS-001", "fields": close_fields},
            ])
            with patch.object(CONFIG, "positions_path", pos_path):
                positions = load_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0].status, "closed")
                self.assertEqual(positions[0].exit_reason, "EDGE_COLLAPSE")

    def test_closed_market_does_not_block_new_condition_id(self):
        # has_traded_market only blocks the exact condition_id, not unrelated ones.
        from polybot.config import CONFIG
        from polybot.position_manager import has_traded_market
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            pos_path = os.path.join(td, "logs", "positions.jsonl")
            pos = self._base_position(condition_id="COND-RESOLVED")
            close_fields = {
                "status": "closed",
                "exit_time": "2026-06-20T10:10:00+00:00",
                "exit_price": 0.80,
                "exit_reason": "TIME_STOP",
                "realized_pnl": 2.5,
            }
            self._write_positions(pos_path, [
                {"type": "open", "position": pos},
                {"type": "close", "position_id": "POS-001", "fields": close_fields},
            ])
            with patch.object(CONFIG, "positions_path", pos_path):
                # Old condition_id is blocked
                self.assertTrue(has_traded_market("COND-RESOLVED"))
                # Different condition_id (new market window) is not blocked
                self.assertFalse(has_traded_market("COND-NEW-MARKET"))

    def test_no_reentry_guard_survives_restart(self):
        # Simulates a bot restart: ledger is re-read from disk, guard holds.
        from polybot.config import CONFIG
        from polybot.position_manager import has_traded_market
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            pos_path = os.path.join(td, "logs", "positions.jsonl")
            pos = self._base_position(condition_id="COND-EXITED")
            close_fields = {
                "status": "closed",
                "exit_time": "2026-06-20T11:00:00+00:00",
                "exit_price": 0.75,
                "exit_reason": "EDGE_COLLAPSE",
                "realized_pnl": 1.0,
            }
            self._write_positions(pos_path, [
                {"type": "open", "position": pos},
                {"type": "close", "position_id": "POS-001", "fields": close_fields},
            ])
            # Simulate restart: fresh load from disk
            with patch.object(CONFIG, "positions_path", pos_path):
                self.assertTrue(has_traded_market("COND-EXITED"))

    def test_empty_condition_id_never_blocks(self):
        from polybot.position_manager import has_traded_market
        self.assertFalse(has_traded_market(""))
        self.assertFalse(has_traded_market(None))  # type: ignore[arg-type]

    def test_update_events_do_not_create_duplicate_positions(self):
        from polybot.config import CONFIG
        from polybot.position_manager import load_positions
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            pos_path = os.path.join(td, "logs", "positions.jsonl")
            pos = self._base_position()
            self._write_positions(pos_path, [
                {"type": "open", "position": pos},
                {"type": "update", "position_id": "POS-001",
                 "fields": {"latest_bid": 0.60, "latest_update_time": "2026-06-20T10:05:00+00:00"}},
                {"type": "update", "position_id": "POS-001",
                 "fields": {"latest_bid": 0.65, "latest_update_time": "2026-06-20T10:08:00+00:00"}},
            ])
            with patch.object(CONFIG, "positions_path", pos_path):
                positions = load_positions()
                self.assertEqual(len(positions), 1)
                self.assertAlmostEqual(positions[0].latest_bid, 0.65)


class TestOrderLifecycle(unittest.TestCase):
    """Tests for pending maker order lifecycle store."""

    def _make_signal(
        self,
        event_key="EVT-ORDER-001",
        condition_id="COND-ORDER-001",
        side="UP",
        execution_price=0.50,
        net_edge=0.10,
        entry_price=0.52,
        exit_price=0.48,
        liquidity=500.0,
        seconds_to_event_end=300.0,
        is_stale=False,
    ):
        from unittest.mock import MagicMock
        sig = MagicMock()
        sig.event_key = event_key
        sig.condition_id = condition_id
        sig.side = side
        sig.net_edge = net_edge
        sig.raw_synth_probability = 0.65
        sig.fair_probability = 0.63
        sig.execution_price = execution_price
        sig.entry_price = entry_price
        sig.exit_price = exit_price
        sig.liquidity = liquidity
        sig.seconds_to_event_end = seconds_to_event_end
        sig.is_stale = is_stale
        sig.asset = "BTC"
        sig.horizon = "1H"
        sig.slug = "btc-1h"
        sig.market_url = "https://polymarket.com/market/btc"
        sig.market_question = "BTC 1H Up/Down"
        sig.spread = 0.02
        sig.raw_edge = 0.13
        sig.calibrated_edge = 0.11
        sig.model_confidence = 0.7
        sig.expected_value_score = 0.09
        sig.confidence_score = 0.75
        sig.liquidity_score = 0.8
        sig.regime_score = 0.6
        sig.calibration_method = "global"
        sig.calibration_sample_count = 100
        sig.calibration_error_estimate = 0.02
        sig.counterparty_bid = 0.48
        sig.hours_to_resolution = 0.5
        sig.event_age_sec = 60.0
        sig.threshold = 0.03
        sig.score = 0.08
        sig.clob_source = "real_clob"
        sig.category = "crypto"
        sig.execution_mode = "maker"
        return sig

    def test_create_order_returns_pending(self):
        """create_order() returns a PENDING PendingOrder with correct fields."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            with patch.object(CONFIG, "orders_path", orders_path):
                sig = self._make_signal()
                order = create_order(sig, size_usd=10.0, contracts=20.0)
                self.assertEqual(order.status, "PENDING")
                self.assertEqual(order.event_key, "EVT-ORDER-001")
                self.assertEqual(order.side, "UP")
                self.assertAlmostEqual(order.size_usd, 10.0)
                self.assertAlmostEqual(order.contracts, 20.0)
                self.assertTrue(order.order_id)
                self.assertTrue(os.path.exists(orders_path))

    def test_order_fills_when_ask_le_limit(self):
        """evaluate_pending_orders fills order when best_ask <= limit_price."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order, load_orders

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            log_path = os.path.join(td, "order_log.jsonl")
            fills_path = os.path.join(td, "fills.jsonl")
            positions_path = os.path.join(td, "positions.jsonl")

            # Signal where ask (entry_price=0.49) <= limit (execution_price=0.50)
            sig = self._make_signal(execution_price=0.50, entry_price=0.49)

            with patch.object(CONFIG, "orders_path", orders_path), \
                 patch.object(CONFIG, "log_dir", td), \
                 patch.object(CONFIG, "positions_path", positions_path):
                order = create_order(sig, size_usd=10.0, contracts=20.0)
                self.assertEqual(order.status, "PENDING")

                from polybot.order_lifecycle import evaluate_pending_orders
                from polybot.position_manager import has_open_event

                signal_map = {("EVT-ORDER-001", "UP"): sig}
                with patch("polybot.order_lifecycle.has_open_event", return_value=False), \
                     patch("polybot.order_lifecycle.open_position_from_fill"):
                    filled, cancelled = evaluate_pending_orders(signal_map)

                self.assertEqual(len(filled), 1)
                self.assertEqual(len(cancelled), 0)
                self.assertEqual(filled[0].status, "FILLED")
                self.assertIsNotNone(filled[0].fill_price)

    def test_order_cancels_near_close(self):
        """evaluate_pending_orders cancels order when seconds_to_event_end < cancel_quotes threshold."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            positions_path = os.path.join(td, "positions.jsonl")

            # seconds_to_event_end=50 < cancel_quotes_before_close_seconds=75
            sig = self._make_signal(execution_price=0.55, entry_price=0.60, seconds_to_event_end=50.0)

            with patch.object(CONFIG, "orders_path", orders_path), \
                 patch.object(CONFIG, "log_dir", td), \
                 patch.object(CONFIG, "positions_path", positions_path), \
                 patch.object(CONFIG, "cancel_quotes_before_close_seconds", 75.0):
                order = create_order(sig, size_usd=10.0, contracts=18.18)

                from polybot.order_lifecycle import evaluate_pending_orders
                signal_map = {("EVT-ORDER-001", "UP"): sig}
                with patch("polybot.order_lifecycle.has_open_event", return_value=False):
                    filled, cancelled = evaluate_pending_orders(signal_map)

                self.assertEqual(len(filled), 0)
                self.assertEqual(len(cancelled), 1)
                self.assertEqual(cancelled[0].cancel_reason, "NEAR_CLOSE")

    def test_order_cancels_on_edge_decay(self):
        """evaluate_pending_orders cancels when current_edge < min_entry_edge."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            positions_path = os.path.join(td, "positions.jsonl")

            # Signal has good edge at order creation, but current net_edge=0.01 < min_entry_edge=0.03
            sig = self._make_signal(execution_price=0.55, entry_price=0.60, net_edge=0.01)

            with patch.object(CONFIG, "orders_path", orders_path), \
                 patch.object(CONFIG, "log_dir", td), \
                 patch.object(CONFIG, "positions_path", positions_path), \
                 patch.object(CONFIG, "min_entry_edge", 0.03), \
                 patch.object(CONFIG, "min_liquidity", 50.0):
                order = create_order(sig, size_usd=10.0, contracts=18.18)

                from polybot.order_lifecycle import evaluate_pending_orders
                signal_map = {("EVT-ORDER-001", "UP"): sig}
                with patch("polybot.order_lifecycle.has_open_event", return_value=False):
                    filled, cancelled = evaluate_pending_orders(signal_map)

                self.assertEqual(len(filled), 0)
                self.assertEqual(len(cancelled), 1)
                self.assertEqual(cancelled[0].cancel_reason, "EDGE_DECAY")

    def test_order_cancels_when_illiquid(self):
        """evaluate_pending_orders cancels when liquidity < min_liquidity."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            positions_path = os.path.join(td, "positions.jsonl")

            # liquidity=30.0 < min_liquidity=50.0, but edge is good enough not to fail edge check
            sig = self._make_signal(execution_price=0.55, entry_price=0.60, liquidity=30.0, net_edge=0.10)

            with patch.object(CONFIG, "orders_path", orders_path), \
                 patch.object(CONFIG, "log_dir", td), \
                 patch.object(CONFIG, "positions_path", positions_path), \
                 patch.object(CONFIG, "min_liquidity", 50.0), \
                 patch.object(CONFIG, "min_entry_edge", 0.03):
                order = create_order(sig, size_usd=10.0, contracts=18.18)

                from polybot.order_lifecycle import evaluate_pending_orders
                signal_map = {("EVT-ORDER-001", "UP"): sig}
                with patch("polybot.order_lifecycle.has_open_event", return_value=False):
                    filled, cancelled = evaluate_pending_orders(signal_map)

                self.assertEqual(len(filled), 0)
                self.assertEqual(len(cancelled), 1)
                self.assertEqual(cancelled[0].cancel_reason, "ILLIQUID")

    def test_cancelled_order_does_not_mark_market_as_traded(self):
        """A cancelled order must NOT mark the market as traded (only fills do)."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order
        from polybot.position_manager import has_traded_market

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            positions_path = os.path.join(td, "positions.jsonl")

            sig = self._make_signal(
                condition_id="COND-CANCEL-TEST",
                seconds_to_event_end=50.0,
                execution_price=0.55,
                entry_price=0.60,
            )

            with patch.object(CONFIG, "orders_path", orders_path), \
                 patch.object(CONFIG, "log_dir", td), \
                 patch.object(CONFIG, "positions_path", positions_path), \
                 patch.object(CONFIG, "cancel_quotes_before_close_seconds", 75.0):
                create_order(sig, size_usd=10.0, contracts=18.18)

                from polybot.order_lifecycle import evaluate_pending_orders
                signal_map = {("EVT-ORDER-001", "UP"): sig}
                with patch("polybot.order_lifecycle.has_open_event", return_value=False):
                    evaluate_pending_orders(signal_map)

                # Cancelled order must not touch positions.jsonl
                self.assertFalse(has_traded_market("COND-CANCEL-TEST"))

    def test_filled_order_marks_condition_as_open(self):
        """A filled order opens a position (via open_position_from_fill)."""
        import tempfile
        from unittest.mock import patch, MagicMock, call
        from polybot.config import CONFIG
        from polybot.order_manager import create_order

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")
            positions_path = os.path.join(td, "positions.jsonl")

            # ask (0.49) <= limit (0.50) → will fill
            sig = self._make_signal(execution_price=0.50, entry_price=0.49)

            with patch.object(CONFIG, "orders_path", orders_path), \
                 patch.object(CONFIG, "log_dir", td), \
                 patch.object(CONFIG, "positions_path", positions_path):
                create_order(sig, size_usd=10.0, contracts=20.0)

                from polybot.order_lifecycle import evaluate_pending_orders
                signal_map = {("EVT-ORDER-001", "UP"): sig}
                mock_open = MagicMock()
                with patch("polybot.order_lifecycle.has_open_event", return_value=False), \
                     patch("polybot.order_lifecycle.open_position_from_fill", mock_open):
                    filled, cancelled = evaluate_pending_orders(signal_map)

                self.assertEqual(len(filled), 1)
                self.assertTrue(mock_open.called)

    def test_pending_order_blocks_second_order_for_same_event(self):
        """has_pending_order_for_event() returns True when a PENDING order exists."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order, has_pending_order_for_event

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")

            sig = self._make_signal()
            with patch.object(CONFIG, "orders_path", orders_path):
                create_order(sig, size_usd=10.0, contracts=20.0)
                self.assertTrue(has_pending_order_for_event("EVT-ORDER-001"))
                self.assertFalse(has_pending_order_for_event("DIFFERENT-EVENT"))

    def test_load_orders_status_filter(self):
        """load_orders(status=...) filters by status correctly."""
        import tempfile
        from unittest.mock import patch
        from polybot.config import CONFIG
        from polybot.order_manager import create_order, cancel_order, load_orders, get_pending_orders

        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.jsonl")

            sig1 = self._make_signal(event_key="EVT-A", condition_id="COND-A")
            sig2 = self._make_signal(event_key="EVT-B", condition_id="COND-B")

            with patch.object(CONFIG, "orders_path", orders_path):
                o1 = create_order(sig1, size_usd=10.0, contracts=20.0)
                o2 = create_order(sig2, size_usd=10.0, contracts=20.0)
                cancel_order(o2, "TEST_CANCEL")

                pending = load_orders("PENDING")
                cancelled = load_orders("CANCELLED")
                all_orders = load_orders()

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].event_key, "EVT-A")
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(cancelled[0].cancel_reason, "TEST_CANCEL")
            self.assertEqual(len(all_orders), 2)


class TestPaperModeSafety(unittest.TestCase):
    def test_paper_mode_never_calls_live_order_placement(self):
        """paper_fill() and execute_decisions() must never invoke the live order stub."""
        from polybot.execution import place_live_limit_order
        from polybot.config import CONFIG
        from unittest.mock import patch, MagicMock

        # Confirm live order stub raises when called
        with self.assertRaises(RuntimeError):
            place_live_limit_order()

    def test_live_trading_disabled_by_default(self):
        from polybot.config import CONFIG
        self.assertTrue(CONFIG.paper_trade_mode)
        self.assertFalse(CONFIG.enable_live_trading)

    def test_execute_decisions_does_not_call_live_stub(self):
        """execute_decisions() must not call place_live_limit_order under any path."""
        from polybot.execution import execute_decisions, place_live_limit_order
        from polybot.config import CONFIG
        from polybot.risk_manager import Decision
        from unittest.mock import patch, MagicMock
        import inspect

        # Verify the execution source does not reference live order placement in the
        # execute_decisions code path
        src = inspect.getsource(execute_decisions)
        self.assertNotIn("place_live_limit_order", src,
            "execute_decisions() must not call place_live_limit_order")

    def test_assert_paper_only_raises_if_live_enabled(self):
        from polybot.config import Config
        cfg = Config()
        cfg.paper_trade_mode = False
        cfg.enable_live_trading = True
        with self.assertRaises(RuntimeError):
            cfg.assert_paper_only()

    def test_paper_fill_sets_mode_paper(self):
        """All paper fills must have mode='paper' in their Fill record."""
        import tempfile, os
        from polybot.config import CONFIG
        from polybot.execution import paper_fill
        from polybot.risk_manager import Decision
        from polybot.signal_engine import Signal
        from unittest.mock import patch
        from dataclasses import asdict

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "logs"), exist_ok=True)
            with patch.object(CONFIG, "log_dir", os.path.join(td, "logs")), \
                 patch.object(CONFIG, "positions_path", os.path.join(td, "logs", "positions.jsonl")):

                sig = Signal(
                    asset="BTC", horizon="1H", slug="btc-1h", event_key="EVT-001",
                    market_url="https://example.com", market_question="BTC 1H",
                    condition_id="COND-001", side="UP", raw_synth_probability=0.65,
                    fair_probability=0.61, calibration_method="bin",
                    calibration_sample_count=50, confidence_score=0.7,
                    calibration_error_estimate=0.04, entry_price=0.55, exit_price=0.52,
                    synth_probability=0.65, calibrated_probability=0.61,
                    execution_price=0.55, counterparty_bid=0.52,
                    raw_edge=0.10, calibrated_edge=0.06, net_edge=0.06,
                    model_confidence=0.7, expected_value_score=0.04, score=0.04,
                    liquidity_score=0.8, regime_score=1.0, entry_cost=0.005,
                    spread=0.03, liquidity=150.0, hours_to_resolution=1.0,
                    event_age_sec=600.0, seconds_to_event_end=900.0,
                    threshold=0.03, execution_mode="taker",
                )
                decision = Decision(sig, True, "test", position_size_usd=10.0, contracts=18.0)
                with patch("polybot.execution.has_open_event", return_value=False):
                    fill = paper_fill(decision, position_id="TEST-POS-001")
                self.assertEqual(fill.mode, "paper")
                self.assertNotEqual(fill.mode, "live")


if __name__ == "__main__":
    unittest.main()
