import json
import tempfile
import unittest
from pathlib import Path

import order_ledger as ol


class OrderLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = ol.OrderLedger(self.dir / "logs" / "orders.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_key_can_only_be_reserved_once(self):
        key = self.ledger.make_key("2026-09-17", "7203.T", "buy")
        self.assertTrue(self.ledger.reserve(key, notional=1000))
        self.assertFalse(self.ledger.reserve(key, notional=1000))

    def test_reservation_survives_reload(self):
        key = self.ledger.make_key("2026-09-17", "7203.T", "buy")
        self.ledger.reserve(key)
        self.assertTrue(ol.OrderLedger(self.ledger.path).has(key))

    def test_buy_and_sell_of_same_ticker_are_distinct_keys(self):
        self.assertTrue(self.ledger.reserve(self.ledger.make_key("2026-09-17", "7203.T", "buy")))
        self.assertTrue(self.ledger.reserve(self.ledger.make_key("2026-09-17", "7203.T", "sell")))

    def test_daily_notional_excludes_rejected_and_other_days(self):
        self.ledger.reserve("2026-09-17:A:buy", notional=1000)
        self.ledger.reserve("2026-09-17:B:buy", notional=2000)
        self.ledger.update("2026-09-17:B:buy", status="REJECTED")
        self.ledger.reserve("2026-09-16:C:buy", notional=5000)
        self.assertEqual(self.ledger.notional_on("2026-09-17"), 1000)
        self.assertEqual(self.ledger.count_on("2026-09-17"), 2)

    def test_recent_stop_losses_counts_only_filled_stop_losses_in_window(self):
        self.ledger.reserve("2026-09-15:A:sell", stop_loss=True)
        self.ledger.update("2026-09-15:A:sell", status="FILLED")
        self.ledger.reserve("2026-09-15:B:sell", stop_loss=True)          # RESERVEDのまま=数えない
        self.ledger.reserve("2026-09-15:C:sell", stop_loss=False)
        self.ledger.update("2026-09-15:C:sell", status="FILLED")           # 通常売却=数えない
        self.ledger.reserve("2026-09-01:D:sell", stop_loss=True)
        self.ledger.update("2026-09-01:D:sell", status="FILLED")           # 窓の外
        self.assertEqual(self.ledger.recent_stop_losses("2026-09-10"), 1)

    def test_audit_log_redacts_secrets_recursively(self):
        path = self.dir / "logs" / "audit.jsonl"
        ol.audit("POST", {"Password": "hunter2", "nested": {"APIPassword": "s3cret", "Token": "tok"}, "Symbol": "7203"}, path)
        text = path.read_text(encoding="utf-8")
        for secret in ("hunter2", "s3cret", "tok\""):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["data"]["Symbol"], "7203")

    def test_halt_set_read_and_clear(self):
        p = self.dir / "logs" / "halt.json"
        self.assertIsNone(ol.halt_reason(p))
        ol.set_halt("残高不整合", p)
        self.assertEqual(ol.halt_reason(p), "残高不整合")
        ol.clear_halt(p)
        self.assertIsNone(ol.halt_reason(p))

    def test_kill_switch_follows_file_presence(self):
        p = self.dir / "STOP_TRADING"
        self.assertFalse(ol.kill_switch_active(p))
        p.write_text("x")
        self.assertTrue(ol.kill_switch_active(p))


if __name__ == "__main__":
    unittest.main()
