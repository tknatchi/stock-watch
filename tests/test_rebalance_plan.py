import unittest

import rebalance as rb
import rebalance_auto as ra

from tests.helpers import snap

UNIVERSE = [("A", 67_700, 90), ("B", 5_800, 80), ("C", 3_000, 75), ("D", 1_500, 70),
            ("E", 2_000, 65), ("F", 700, 60), ("G", 25_000, 55), ("H", 400, 50)]


def universe():
    return [snap(t, p, s) for t, p, s in UNIVERSE]


def cand(price, current=0.0, cap=46_000.0, score=80, ticker="A"):
    return {"ticker": ticker, "price": price, "score": score, "current_value": current, "cap": cap}


class GreedyOverweightGuardTests(unittest.TestCase):
    def test_first_lot_far_over_the_cap_is_skipped(self):
        # 6.8万円の株を、目標4.6万円・許容超過6千円の枠で買うと2.2万円超過 → 翌日に売り戻される
        shares, cash = rb.greedy_lot_allocation([cand(67_700)], 300_000, 1, max_overweight_value=6_000)
        self.assertEqual(shares["A"], 0)
        self.assertEqual(cash, 300_000)

    def test_first_lot_slightly_over_the_cap_is_still_bought(self):
        shares, _ = rb.greedy_lot_allocation([cand(50_000)], 300_000, 1, max_overweight_value=6_000)
        self.assertEqual(shares["A"], 1)

    def test_without_the_guard_the_first_lot_is_bought_regardless(self):
        shares, _ = rb.greedy_lot_allocation([cand(67_700)], 300_000, 1)
        self.assertEqual(shares["A"], 1)

    def test_existing_position_is_not_topped_up_past_the_cap_by_the_first_lot_exception(self):
        # 保有1.2万円・目標1.4万円で1単元3千円(→1.5万円)は上限超過。例外は新規建ての最初の1単元だけ
        shares, cash = rb.greedy_lot_allocation([cand(3_000, current=12_000, cap=14_011)], 100_000, 1, max_overweight_value=2_000)
        self.assertEqual(shares["A"], 0)
        self.assertEqual(cash, 100_000)

    def test_existing_position_can_still_be_bought_up_to_the_cap(self):
        shares, _ = rb.greedy_lot_allocation([cand(3_000, current=6_000, cap=14_011)], 100_000, 1, max_overweight_value=2_000)
        self.assertEqual(shares["A"], 2)   # 6,000→12,000(3単元目は上限超過で止まる)

    def test_additional_lots_never_exceed_the_cap(self):
        shares, _ = rb.greedy_lot_allocation([cand(10_000, cap=25_000)], 1_000_000, 1, max_overweight_value=1e9)
        self.assertEqual(shares["A"], 2)


class ChurnTests(unittest.TestCase):
    def rebalance_once(self, cash, holdings, basis):
        plan = rb.compute_plan(cash, holdings, universe(), basis)
        ra.apply_trades(plan["rows"], holdings, basis, {}, {}, lock_runs=0)
        return plan["leftover_cash"]

    def test_rerun_at_unchanged_prices_trades_nothing(self):
        for capital in (100_000, 300_000, 1_000_000):
            holdings, basis = {}, {}
            cash = self.rebalance_once(capital, holdings, basis)
            again = rb.compute_plan(cash, holdings, universe(), basis)
            self.assertEqual([(r["ticker"], r["action_shares"]) for r in again["rows"] if r["action_shares"]], [],
                             f"資金{capital:,}円で直後の再実行が売買を出した")

    def test_expensive_stock_is_not_bought_when_it_would_be_sold_back(self):
        plan = rb.compute_plan(300_000, {}, universe(), {})
        bought = {r["ticker"]: r["action_shares"] for r in plan["rows"] if r["action_shares"] > 0}
        self.assertNotIn("A", bought)   # 6.8万円は資金30万円・上限15%(4.5万円)には大きすぎる


class RoundTripLockTests(unittest.TestCase):
    def plan(self, holdings, snaps, cash=0.0, basis=None, **kw):
        return rb.compute_plan(cash, holdings, snaps, basis or {}, **kw)

    def action(self, plan, ticker):
        return next(r for r in plan["rows"] if r["ticker"] == ticker)

    def test_no_sell_lock_blocks_selling_an_overweight_ticker(self):
        snaps = [snap("A", 1000, 90), snap("B", 1000, 10)]
        free = self.action(self.plan({"A": 100}, snaps), "A")
        locked = self.action(self.plan({"A": 100}, snaps, no_sell_tickers={"A"}), "A")
        self.assertLess(free["action_shares"], 0)          # 対照: ロックなしなら売る
        self.assertEqual((locked["action_shares"], locked["locked"]), (0, "sell"))

    def test_no_buy_lock_blocks_buying_an_underweight_ticker(self):
        snaps = [snap("A", 1000, 90), snap("B", 1000, 80)]
        free = self.action(self.plan({}, snaps, cash=100_000), "A")
        locked = self.action(self.plan({}, snaps, cash=100_000, no_buy_tickers={"A"}), "A")
        self.assertGreater(free["action_shares"], 0)       # 対照: ロックなしなら買う
        self.assertEqual((locked["action_shares"], locked["locked"]), (0, "buy"))

    def test_stop_loss_ignores_the_no_sell_lock(self):
        plan = self.plan({"A": 10}, [snap("A", 800, 90)], basis={"A": 1000.0}, no_sell_tickers={"A"})
        row = self.action(plan, "A")
        self.assertTrue(row["stop_loss"])
        self.assertEqual(row["action_shares"], -10)

    def test_lock_only_blocks_the_opposite_direction(self):
        # 直前に「買った」銘柄(no_sell)でも、買い増しは止めない
        snaps = [snap("A", 1000, 90), snap("B", 1000, 80)]
        row = self.action(self.plan({}, snaps, cash=100_000, no_sell_tickers={"A"}), "A")
        self.assertGreater(row["action_shares"], 0)


if __name__ == "__main__":
    unittest.main()
