import json
import tempfile
import unittest
from pathlib import Path

from trading_config import TradingConfig, load_config


class TradingConfigTests(unittest.TestCase):
    def _write(self, tmp, obj):
        p = Path(tmp) / "trading_config.json"
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def test_missing_file_gives_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "nope.json")
        self.assertEqual((cfg.mode, cfg.broker, cfg.live), ("notify", "paper", False))
        self.assertFalse(cfg.live_enabled({"LIVE_TRADING": "1"}))

    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, {"mode": "notify", "max_order_notinal": 1})  # 綴り違い
            with self.assertRaises(ValueError):
                load_config(p)

    def test_underscore_comment_keys_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(self._write(tmp, {"_comment": "memo", "mode": "approve"}))
        self.assertEqual(cfg.mode, "approve")

    def test_invalid_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_config(self._write(tmp, {"mode": "yolo"}))

    def test_non_positive_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_config(self._write(tmp, {"max_order_notional": 0}))

    def test_plain_tse_is_rejected_as_order_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_config(self._write(tmp, {"kabu_order_exchange": 1}))

    def test_default_exchanges_are_tse_plus_for_orders_and_tse_for_board(self):
        cfg = TradingConfig()
        self.assertEqual((cfg.kabu_order_exchange, cfg.kabu_board_exchange), (27, 1))

    def test_live_requires_both_config_flag_and_env(self):
        self.assertFalse(TradingConfig(live=True).live_enabled({}))
        self.assertFalse(TradingConfig(live=True).live_enabled({"LIVE_TRADING": "0"}))
        self.assertFalse(TradingConfig(live=False).live_enabled({"LIVE_TRADING": "1"}))
        self.assertTrue(TradingConfig(live=True).live_enabled({"LIVE_TRADING": "1"}))

    def test_production_endpoint_detection(self):
        self.assertTrue(TradingConfig(kabu_base_url="http://localhost:18080/kabusapi").is_production_endpoint)
        self.assertFalse(TradingConfig().is_production_endpoint)  # 既定は検証環境18081


if __name__ == "__main__":
    unittest.main()
