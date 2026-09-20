import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import live_trade
import order_ledger as ol
from broker import OrderRequest, OrderResult, PaperBroker, Position
from trading_config import TradingConfig

from tests.helpers import NOW_CLOSED, NOW_OPEN, make_env, noop_sleep, snap, write_state

A, B = "7203.T", "9432.T"


class LyingBroker(PaperBroker):
    """発注後だけ、実際より1株少ない保有を報告する(約定反映後の照合が効くかの確認用)。"""

    def get_positions(self):
        ps = super().get_positions()
        return [Position(p.ticker, p.shares - 1) for p in ps] if self.sent else ps


class NeverFillBroker(PaperBroker):
    """発注は受け付けるが約定しない(未約定のまま残る注文)。"""

    def place_order(self, req):
        self.sent.append(req)
        return OrderResult("N1", "SUBMITTED")

    def get_order(self, order_id):
        return OrderResult(order_id, "SUBMITTED")


class SlowFillBroker(PaperBroker):
    """1回目の照会では未約定、2回目の照会で約定済みになる。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.polls = 0
        self._final: dict[str, OrderResult] = {}

    def place_order(self, req):
        real = super().place_order(req)
        self._final[real.order_id] = real
        return OrderResult(real.order_id, "SUBMITTED")

    def get_order(self, order_id):
        self.polls += 1
        return self._final[order_id] if self.polls >= 2 else OrderResult(order_id, "SUBMITTED")


class FillLaterBroker(PaperBroker):
    def get_order(self, order_id):
        return OrderResult(order_id, "FILLED", filled_qty=10, avg_price=1000.0)


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = make_env(Path(self.tmp.name))
        self.msgs: list[str] = []
        self.snaps = [snap(A, 1000, 90), snap(B, 200, 80)]
        write_state(self.env, last_cash=100_000)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **kw) -> TradingConfig:
        base = dict(mode="auto", broker="paper", capital_limit=100_000, fill_poll_seconds=0)
        base.update(kw)
        return TradingConfig(**base)

    def broker(self, cash=100_000, positions=None, cls=PaperBroker, **kw):
        return cls(cash, positions or {}, {}, **kw)

    def run_once(self, cfg, broker, now=NOW_OPEN, snaps=None):
        snaps = self.snaps if snaps is None else snaps
        return live_trade.run_once(cfg, broker, self.env, now, fetch_snapshots=lambda h: snaps,
                                   notify=self.msgs.append, environ={}, sleep=noop_sleep)

    def state(self):
        return live_trade.load_state(self.env)


class RunModeTests(LiveBase):
    def test_notify_mode_sends_proposal_and_places_nothing(self):
        b = self.broker()
        res = self.run_once(self.cfg(mode="notify"), b)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["orders"])
        self.assertEqual(b.sent, [])
        self.assertEqual(len(self.msgs), 1)
        self.assertIn(A, self.msgs[0])

    def test_approve_mode_saves_pending_plan_and_places_nothing(self):
        b = self.broker()
        res = self.run_once(self.cfg(mode="approve"), b)
        self.assertEqual(b.sent, [])
        self.assertTrue(self.env.pending_path.exists())
        self.assertIn(res["plan_id"], self.msgs[0])

    def test_auto_mode_places_orders_and_records_state_from_broker(self):
        b = self.broker()
        res = self.run_once(self.cfg(), b)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(b.sent), 2)
        self.assertEqual(self.state()["holdings"], b.positions)
        self.assertEqual(self.state()["lastBrokerCash"], b.cash)
        self.assertEqual(self.env.ledger.count_on("2026-09-17"), 2)
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_auto_mode_outside_market_hours_places_nothing(self):
        b = self.broker()
        res = self.run_once(self.cfg(), b, now=NOW_CLOSED)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(b.sent, [])

    def test_kill_switch_blocks_every_mode(self):
        self.env.kill_path.write_text("stop")
        for mode in ("notify", "approve", "auto"):
            b = self.broker()
            self.assertEqual(self.run_once(self.cfg(mode=mode), b)["status"], "blocked", mode)
            self.assertEqual(b.sent, [], mode)

    def test_halt_blocks_and_alerts_the_user(self):
        ol.set_halt("残高不整合", self.env.halt_path)
        b = self.broker()
        self.assertEqual(self.run_once(self.cfg(), b)["status"], "blocked")
        self.assertEqual(b.sent, [])
        self.assertIn("停止中", self.msgs[0])

    def test_missing_state_file_stops_before_any_order(self):
        self.env.state_path.unlink()
        b = self.broker()
        self.assertEqual(self.run_once(self.cfg(), b)["status"], "no_state")
        self.assertEqual(b.sent, [])

    def test_held_ticker_without_price_data_skips_the_run(self):
        write_state(self.env, holdings={A: 5}, cost_basis={A: 1000}, last_cash=100_000)
        b = self.broker(positions={A: 5})
        res = self.run_once(self.cfg(), b, snaps=[snap(B, 200, 80)])  # A の株価が取れない
        self.assertEqual(res["status"], "data_unavailable")
        self.assertEqual(b.sent, [])

    def test_strategy_capital_is_capped_by_capital_limit(self):
        b = self.broker(cash=1_000_000)  # 口座には100万円あるが戦略は10万円まで
        res = self.run_once(self.cfg(), b)
        spent = sum(o.qty * o.limit_price for o in res["orders"])
        self.assertTrue(res["orders"])
        self.assertLessEqual(spent, 100_000)

    def test_cooldown_counters_tick_down_and_expire(self):
        write_state(self.env, cooldown={"X.T": 2, "Y.T": 1}, last_cash=100_000)
        self.run_once(self.cfg(mode="notify"), self.broker(), snaps=[snap("Z.T", 100, 10)])  # 提案なし
        self.assertEqual(self.state()["cooldown"], {"X.T": 1})


class ReconciliationTests(LiveBase):
    def test_holdings_mismatch_at_start_halts_without_ordering(self):
        write_state(self.env, holdings={A: 5}, last_cash=100_000)
        b = self.broker(positions={})
        res = self.run_once(self.cfg(), b)
        self.assertEqual(res["status"], "halted")
        self.assertIsNotNone(ol.halt_reason(self.env.halt_path))
        self.assertEqual(b.sent, [])

    def test_unexpected_cash_drop_at_start_halts(self):
        b = self.broker(cash=90_000)  # 前回100,000円 → 許容3,000円を超えて減少
        res = self.run_once(self.cfg(), b)
        self.assertEqual(res["status"], "halted")
        self.assertEqual(b.sent, [])

    def test_holdings_outside_the_managed_universe_are_ignored(self):
        b = self.broker(positions={"6666.T": 100})
        res = self.run_once(self.cfg(), b)
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("6666.T", self.state()["holdings"])

    def test_notify_mode_follows_manual_trades_without_halting(self):
        b = self.broker(positions={A: 5})
        res = self.run_once(self.cfg(mode="notify"), b)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.state()["holdings"], {A: 5})
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_reconcile_reports_share_mismatch(self):
        problems = live_trade.reconcile({"holdings": {A: 5}}, {A: Position(A, 4)}, 0, TradingConfig())
        self.assertEqual(len(problems), 1)

    def test_reconcile_cash_drop_beyond_tolerance_only(self):
        cfg = TradingConfig(cash_tolerance_yen=3000)
        st = {"holdings": {}, "lastBrokerCash": 100_000}
        self.assertTrue(live_trade.reconcile(st, {}, 96_999, cfg))
        self.assertFalse(live_trade.reconcile(st, {}, 97_500, cfg))     # 許容内
        self.assertFalse(live_trade.reconcile(st, {}, 150_000, cfg))    # 増えるのは配当・入金として許容

    def test_apply_fill_buy_uses_weighted_average_cost(self):
        st = {"holdings": {A: 10}, "costBasis": {A: 1000.0}}
        live_trade.apply_fill(st, A, "buy", 10, 2000.0)
        self.assertEqual((st["holdings"][A], st["costBasis"][A]), (20, 1500.0))

    def test_apply_fill_selling_everything_clears_holding_and_basis(self):
        st = {"holdings": {A: 10}, "costBasis": {A: 1000.0}}
        live_trade.apply_fill(st, A, "sell", 10, 900.0)
        self.assertEqual((st["holdings"], st["costBasis"]), ({}, {}))


class ExecutionTests(LiveBase):
    def test_unknown_order_result_halts_and_sends_no_further_orders(self):
        b = self.broker(unknown_tickers={A})  # A(高スコア)が先に発注される
        res = self.run_once(self.cfg(), b)
        self.assertEqual([o.ticker for o in b.sent], [A])
        self.assertIsNotNone(ol.halt_reason(self.env.halt_path))
        self.assertFalse(self.env.ledger.has(self.env.ledger.make_key("2026-09-17", B, "buy")))
        self.assertTrue(any("発注結果不明" in m for m in self.msgs))
        self.assertEqual(self.run_once(self.cfg(), b)["status"], "blocked")  # 翌回も止まったまま

    def test_post_trade_balance_mismatch_halts(self):
        b = self.broker(cls=LyingBroker)
        self.run_once(self.cfg(), b)
        self.assertIn("一致しません", ol.halt_reason(self.env.halt_path))

    def test_rejected_order_leaves_state_untouched_and_next_order_proceeds(self):
        b = self.broker(reject_tickers={A})
        self.run_once(self.cfg(), b)
        self.assertEqual(len(b.sent), 2)
        self.assertEqual(list(self.state()["holdings"]), [B])
        self.assertEqual(self.env.ledger._load()[0]["status"], "REJECTED")
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_partial_fill_is_reflected_in_state_without_halting(self):
        b = self.broker(partial_ratio=0.5)
        self.run_once(self.cfg(), b)
        self.assertEqual(self.state()["holdings"], b.positions)
        self.assertEqual({e["status"] for e in self.env.ledger._load()}, {"PARTIAL"})
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_order_is_polled_until_filled(self):
        b = self.broker(cls=SlowFillBroker)
        cfg = self.cfg(fill_poll_seconds=5)
        self.run_once(cfg, b)
        self.assertGreaterEqual(b.polls, 2)
        self.assertEqual(self.state()["holdings"], b.positions)
        self.assertEqual({e["status"] for e in self.env.ledger._load()}, {"FILLED"})

    def test_order_still_open_after_wait_is_marked_working_not_halted(self):
        b = self.broker(cls=NeverFillBroker)
        self.run_once(self.cfg(), b)
        self.assertEqual({e["status"] for e in self.env.ledger._load()}, {"WORKING"})
        self.assertEqual(self.state()["holdings"], {})
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_stop_loss_sells_everything_and_starts_cooldown(self):
        write_state(self.env, holdings={A: 10}, cost_basis={A: 1000.0}, last_cash=50_000)
        b = self.broker(cash=50_000, positions={A: 10})
        self.run_once(self.cfg(), b, snaps=[snap(A, 800, 80)])  # -20%
        st = self.state()
        self.assertNotIn(A, st["holdings"])
        self.assertEqual(st["cooldown"][A], live_trade.rebalance_auto.STOP_LOSS_COOLDOWN_RUNS)
        self.assertTrue(self.env.ledger._load()[0]["stop_loss"])

    def test_circuit_breaker_halts_after_repeated_stop_losses(self):
        for i, t in enumerate(("X.T", "Y.T")):
            key = f"2026-09-15:{t}:sell"
            self.env.ledger.reserve(key, stop_loss=True)
            self.env.ledger.update(key, status="FILLED")
        b = self.broker()
        self.run_once(self.cfg(circuit_breaker_stop_losses=2), b)
        self.assertIn("損切り", ol.halt_reason(self.env.halt_path))

    def test_circuit_breaker_stays_quiet_below_threshold(self):
        self.env.ledger.reserve("2026-09-15:X.T:sell", stop_loss=True)
        self.env.ledger.update("2026-09-15:X.T:sell", status="FILLED")
        self.run_once(self.cfg(circuit_breaker_stop_losses=2), self.broker())
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_executing_the_same_orders_twice_sends_only_once(self):
        b = PaperBroker(100_000, {}, {A: 1000})
        state = {"holdings": {}, "costBasis": {}, "cooldown": {}}
        orders = [OrderRequest(A, "buy", 5, 1003)]
        args = (self.cfg(), b, self.env, state, "2026-09-17", self.msgs.append, noop_sleep)
        live_trade.execute_orders(orders, *args)
        lines = live_trade.execute_orders(orders, *args)
        self.assertEqual(len(b.sent), 1)
        self.assertIn("発注済み", lines[0])

    def test_late_fill_of_a_working_order_is_applied_exactly_once(self):
        key = "2026-09-16:7203.T:buy"
        self.env.ledger.reserve(key, ticker=A, side="buy", qty=10, applied_qty=0)
        self.env.ledger.update(key, status="WORKING", order_id="W1")
        b = self.broker(cls=FillLaterBroker)
        state = {"holdings": {}, "costBasis": {}}
        live_trade.settle_working_orders(b, self.env, state)
        live_trade.settle_working_orders(b, self.env, state)
        self.assertEqual(state["holdings"], {A: 10})
        self.assertEqual(self.env.ledger._load()[0]["status"], "FILLED")


class ApproveTests(LiveBase):
    def propose(self, **cfg_kw):
        self.b = self.broker()
        self.cfg_ = self.cfg(mode="approve", **cfg_kw)
        return self.run_once(self.cfg_, self.b)["plan_id"]

    def approve(self, plan_id, now=NOW_OPEN):
        return live_trade.approve(plan_id, self.cfg_, self.b, self.env, now, notify=self.msgs.append,
                                  environ={}, sleep=noop_sleep)

    def test_approving_places_the_proposed_orders_and_consumes_the_plan(self):
        pid = self.propose()
        res = self.approve(pid)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(self.b.sent), 2)
        self.assertFalse(self.env.pending_path.exists())

    def test_wrong_plan_id_is_refused_and_plan_kept(self):
        self.propose()
        self.assertEqual(self.approve("deadbeef")["status"], "error")
        self.assertEqual(self.b.sent, [])
        self.assertTrue(self.env.pending_path.exists())

    def test_expired_plan_is_refused(self):
        pid = self.propose()
        res = self.approve(pid, now=NOW_OPEN + dt.timedelta(hours=4))
        self.assertEqual(res["status"], "error")
        self.assertIn("有効期限", res["reason"])
        self.assertEqual(self.b.sent, [])

    def test_second_approval_of_the_same_plan_is_refused(self):
        pid = self.propose()
        self.approve(pid)
        sent = len(self.b.sent)
        self.assertEqual(self.approve(pid)["status"], "error")
        self.assertEqual(len(self.b.sent), sent)

    def test_approval_revalidates_against_fresh_quotes(self):
        pid = self.propose()
        self.b.prices[A] *= 1.10   # 提案後に急騰
        self.b.prices[B] *= 1.10
        res = self.approve(pid)
        self.assertEqual(self.b.sent, [])
        self.assertEqual(len(res["rejected"]), 2)

    def test_approval_outside_market_hours_is_blocked_and_plan_kept(self):
        pid = self.propose(approval_ttl_minutes=600)
        res = self.approve(pid, now=NOW_CLOSED)
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(self.env.pending_path.exists())

    def test_approval_without_pending_plan_is_an_error(self):
        self.b, self.cfg_ = self.broker(), self.cfg(mode="approve")
        self.assertEqual(self.approve("whatever")["status"], "error")

    def test_balance_change_between_proposal_and_approval_halts(self):
        pid = self.propose()
        self.b.positions[A] = 3   # 提案後に手動売買された
        res = self.approve(pid)
        self.assertEqual(res["status"], "halted")
        self.assertEqual(self.b.sent, [])
        self.assertIsNotNone(ol.halt_reason(self.env.halt_path))


class CliTests(LiveBase):
    def run_cli(self, argv, cfg=None):
        with mock.patch.object(live_trade, "default_env", return_value=self.env), \
             mock.patch.object(live_trade, "load_config", return_value=cfg or TradingConfig()), \
             contextlib.redirect_stdout(io.StringIO()):
            return live_trade.main(argv)

    def test_stop_creates_kill_switch_and_resume_clears_it_and_halt(self):
        self.run_cli(["stop"])
        self.assertTrue(self.env.kill_path.exists())
        ol.set_halt("x", self.env.halt_path)
        self.run_cli(["resume"])
        self.assertFalse(self.env.kill_path.exists())
        self.assertIsNone(ol.halt_reason(self.env.halt_path))

    def test_init_without_yes_does_not_write_state(self):
        self.env.state_path.unlink()
        b = self.broker(positions={A: 5})
        with contextlib.redirect_stdout(io.StringIO()):
            live_trade.cmd_init(self.cfg(), b, self.env, yes=False)
        self.assertFalse(self.env.state_path.exists())

    def test_init_with_yes_imports_managed_holdings_and_ignores_others(self):
        self.env.state_path.unlink()
        b = self.broker(cash=80_000, positions={A: 5, "6666.T": 9})
        with contextlib.redirect_stdout(io.StringIO()):
            live_trade.cmd_init(self.cfg(), b, self.env, yes=True)
        st = self.state()
        self.assertEqual(st["holdings"], {A: 5})
        self.assertEqual(st["lastBrokerCash"], 80_000)


if __name__ == "__main__":
    unittest.main()
