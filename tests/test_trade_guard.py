import tempfile
import unittest
from pathlib import Path
from unittest import mock

import market_calendar
import order_ledger as ol
from broker import Quote
from trade_guard import build_orders, check_execution_allowed, round_to_tick
from trading_config import TradingConfig

from tests.helpers import NOW_CLOSED, NOW_OPEN

TODAY = "2026-09-17"


def row(ticker="A", shares=10, price=1000.0, score=80, stop_loss=False):
    return {"ticker": ticker, "action_shares": shares, "price": price, "score": score, "stop_loss": stop_loss}


def q(ticker="A", price=1000.0):
    return Quote(ticker, price, bid=price, ask=price)


class ExecutionAllowedTests(unittest.TestCase):
    def check(self, now=NOW_OPEN, cfg=None, **kw):
        base = dict(kill_switch=False, halt_reason=None, will_place_orders=True, environ={})
        base.update(kw)
        return check_execution_allowed(now, cfg or TradingConfig(), **base)

    def test_kill_switch_blocks(self):
        self.assertIn("kill switch", self.check(kill_switch=True))

    def test_halt_blocks_and_shows_reason(self):
        self.assertIn("残高不整合", self.check(halt_reason="残高不整合"))

    def test_holiday_blocks_even_when_only_proposing(self):
        import datetime as dt
        self.assertIn("休場", self.check(now=dt.datetime(2026, 9, 22, 10, 0), will_place_orders=False))

    def test_outside_market_hours_blocks_placing_but_not_proposing(self):
        self.assertIn("取引時間外", self.check(now=NOW_CLOSED))
        self.assertIsNone(self.check(now=NOW_CLOSED, will_place_orders=False))

    def test_kabu_needs_both_live_flag_and_env(self):
        cfg = TradingConfig(broker="kabu", live=True)
        self.assertIn("実発注が無効", self.check(cfg=cfg, environ={}))
        self.assertIsNone(self.check(cfg=cfg, environ={"LIVE_TRADING": "1"}))
        self.assertIn("実発注が無効", self.check(cfg=TradingConfig(broker="kabu", live=False), environ={"LIVE_TRADING": "1"}))

    def test_kabu_blocked_when_holidays_unknown(self):
        cfg = TradingConfig(broker="kabu", live=True)
        with mock.patch.object(market_calendar, "jpholiday", None):
            self.assertIn("jpholiday", self.check(cfg=cfg, environ={"LIVE_TRADING": "1"}))

    def test_normal_paper_run_in_hours_is_allowed(self):
        self.assertIsNone(self.check())


class BuildOrdersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = ol.OrderLedger(Path(self.tmp.name) / "orders.json")
        self.cfg = TradingConfig()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, rows, quotes=None, cash=1_000_000, holdings=None, total=1_000_000, cfg=None):
        return build_orders(rows, quotes=quotes if quotes is not None else {r["ticker"]: q(r["ticker"], r["price"]) for r in rows},
                            cash=cash, holdings=holdings or {}, total_value=total, cfg=cfg or self.cfg,
                            ledger=self.ledger, today=TODAY)

    def reasons(self, res):
        return " ".join(why for _, why in res.rejected)

    def test_tick_rounding_buy_up_sell_down(self):
        self.assertEqual(round_to_tick(1002.4, "buy"), 1003)
        self.assertEqual(round_to_tick(1002.6, "sell"), 1002)
        self.assertEqual(round_to_tick(3001, "buy"), 3005)
        self.assertEqual(round_to_tick(3001, "sell"), 3000)
        self.assertEqual(round_to_tick(5001, "buy"), 5010)

    def test_limit_prices_are_on_the_aggressive_side_of_the_quote(self):
        res = self.build([row("A", 10), row("B", -10, score=70)], holdings={"B": 10})
        buy = next(o for o in res.orders if o.side == "buy")
        sell = next(o for o in res.orders if o.side == "sell")
        self.assertGreater(buy.limit_price, 1000)
        self.assertLess(sell.limit_price, 1000)

    def test_order_over_per_order_cap_is_rejected(self):
        res = self.build([row(shares=200)])  # 約20万円 > 10万円
        self.assertEqual(res.orders, [])
        self.assertIn("1注文の上限", self.reasons(res))

    def test_daily_notional_cap_counts_orders_already_in_ledger(self):
        self.ledger.reserve("2026-09-17:X:buy", notional=250_000)
        res = self.build([row(shares=60)])  # +約6万円 → 31万円 > 30万円
        self.assertEqual(res.orders, [])
        self.assertIn("1日の発注上限", self.reasons(res))

    def test_daily_order_count_cap(self):
        cfg = TradingConfig(max_orders_per_day=2)
        rows = [row("A", 1, score=90), row("B", 1, score=80), row("C", 1, score=70)]
        res = self.build(rows, cfg=cfg)
        self.assertEqual(len(res.orders), 2)
        self.assertIn("回数上限", self.reasons(res))

    def test_price_deviation_beyond_limit_is_rejected(self):
        res = self.build([row(price=1000)], quotes={"A": q("A", 1050)})  # +5% > 3%
        self.assertEqual(res.orders, [])
        self.assertIn("乖離", self.reasons(res))

    def test_missing_quote_is_rejected(self):
        res = self.build([row()], quotes={})
        self.assertEqual(res.orders, [])
        self.assertIn("現在値", self.reasons(res))

    def test_selling_more_than_held_is_rejected(self):
        res = self.build([row(shares=-10)], holdings={"A": 5})
        self.assertEqual(res.orders, [])
        self.assertIn("保有", self.reasons(res))

    def test_buy_beyond_available_cash_is_rejected(self):
        res = self.build([row(shares=50)], cash=10_000)
        self.assertEqual(res.orders, [])
        self.assertIn("余力不足", self.reasons(res))

    def test_second_buy_sees_cash_already_committed_to_first(self):
        # 各約5万円の買い2本に対し現金6万円 → 1本目だけ通る
        res = self.build([row("A", 50, score=90), row("B", 50, score=80)], cash=60_000)
        self.assertEqual([o.ticker for o in res.orders], ["A"])

    def test_buy_pushing_weight_over_hard_cap_is_rejected(self):
        res = self.build([row(shares=50)], total=100_000, holdings={"A": 10})  # (10+50)*1000/10万=60%
        self.assertEqual(res.orders, [])
        self.assertIn("絶対上限", self.reasons(res))

    def test_same_day_same_side_duplicate_is_rejected(self):
        self.ledger.reserve(self.ledger.make_key(TODAY, "A", "buy"))
        res = self.build([row("A", 5)])
        self.assertEqual(res.orders, [])
        self.assertIn("二重発注", self.reasons(res))

    def test_sells_come_first_and_stop_loss_leads_the_sells(self):
        rows = [row("B", 5, score=95), row("S1", -5, score=50), row("S2", -5, score=10, stop_loss=True)]
        res = self.build(rows, holdings={"S1": 5, "S2": 5})
        self.assertEqual([o.ticker for o in res.orders], ["S2", "S1", "B"])

    def test_zero_action_rows_are_ignored(self):
        self.assertEqual(self.build([row(shares=0)]).orders, [])


if __name__ == "__main__":
    unittest.main()
