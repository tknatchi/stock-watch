import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rebalance as rb
import rebalance_auto as ra

from tests.helpers import snap

TODAY = dt.date.today()
A = "7203.T"


class FetchFallbackTests(unittest.TestCase):
    def test_retries_after_an_exception_and_returns_the_later_success(self):
        calls = []

        def flaky(ticker):
            calls.append(ticker)
            if len(calls) == 1:
                raise RuntimeError("429")
            return snap(ticker, 100)

        with contextlib.redirect_stdout(io.StringIO()):
            s = rb.fetch_snapshot_with_retry(A, retries=3, base_delay=0, fetch=flaky)
        self.assertEqual(s.ticker, A)
        self.assertEqual(len(calls), 2)

    def test_gives_up_with_none_after_all_retries_instead_of_raising(self):
        with contextlib.redirect_stdout(io.StringIO()):
            s = rb.fetch_snapshot_with_retry(A, retries=3, base_delay=0, fetch=mock.Mock(side_effect=RuntimeError("x")))
        self.assertIsNone(s)

    def test_none_result_is_retried_too(self):
        fetch = mock.Mock(side_effect=[None, None, snap(A, 100)])
        self.assertIsNotNone(rb.fetch_snapshot_with_retry(A, retries=3, base_delay=0, fetch=fetch))

    def test_find_missing_holdings_flags_held_tickers_without_snapshots(self):
        missing = rb.find_missing_holdings({A: 5, "9432.T": 3, "GONE.T": 0}, [snap(A, 100)])
        self.assertEqual(missing, ["9432.T"])  # 保有0株の銘柄は数えない


class RebalanceAutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.pf, self.hist = t / "portfolio_auto.json", t / "hist.json"
        patches = [
            mock.patch.object(ra, "PORTFOLIO_AUTO_PATH", self.pf),
            mock.patch.object(ra, "HISTORY_AUTO_PATH", self.hist),
            mock.patch.object(ra, "LOG_DIR", t / "logs"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.alerts = []
        p = mock.patch.object(ra, "send_line_alert", side_effect=self.alerts.append)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, **kw):
        base = {"cash": 100_000.0, "holdings": {}, "costBasis": {}, "cooldown": {}}
        base.update(kw)
        self.pf.write_text(json.dumps(base), encoding="utf-8")

    def read(self):
        return json.loads(self.pf.read_text(encoding="utf-8"))

    def run_main(self, snaps, actions=None):
        fetch_actions = actions if actions is not None else mock.Mock(side_effect=AssertionError("配当・分割を取得しないはず"))
        with mock.patch.object(rb, "fetch_universe_snapshots", return_value=snaps), \
             mock.patch.object(ra.corporate_actions, "fetch_actions", fetch_actions), \
             contextlib.redirect_stdout(io.StringIO()):
            ra.main()

    def test_held_ticker_without_price_writes_nothing(self):
        self.write(holdings={A: 5}, costBasis={A: 1000.0})
        before = self.pf.read_text(encoding="utf-8")
        self.run_main([snap("9432.T", 200)])   # A の株価が無い
        self.assertEqual(self.pf.read_text(encoding="utf-8"), before)
        self.assertFalse(self.hist.exists())

    def test_first_run_records_action_baseline_without_fetching_actions(self):
        self.write(holdings={A: 5}, costBasis={A: 1000.0}, cash=1000.0)   # 保有あり=取得しようと思えばできる状況
        self.run_main([snap(A, 1000)])
        self.assertEqual(self.read()["lastActionCheck"], TODAY.isoformat())

    def test_action_fetch_failure_keeps_the_baseline_date_for_retry(self):
        self.write(holdings={A: 5}, costBasis={A: 1000.0}, lastActionCheck="2026-01-01")
        self.run_main([snap(A, 1000)], actions=mock.Mock(side_effect=RuntimeError("network")))
        self.assertEqual(self.read()["lastActionCheck"], "2026-01-01")

    def test_split_is_applied_so_it_does_not_trigger_a_false_stop_loss(self):
        # 2分割: 株価は2000→1000。株数・取得単価を直さないと-50%で損切りが誤発動する
        self.write(holdings={A: 10}, costBasis={A: 2000.0}, cash=0.0, lastActionCheck=(TODAY - dt.timedelta(days=3)).isoformat())
        self.run_main([snap(A, 1000)], actions=lambda t: [{"date": TODAY.isoformat(), "dividend": 0.0, "split": 2.0}])
        self.assertEqual(self.read()["cooldown"], {})
        self.assertEqual(self.alerts, [])

    def test_dividend_is_credited_to_cash(self):
        self.write(holdings={A: 10}, costBasis={A: 1000.0}, cash=1000.0, lastActionCheck=(TODAY - dt.timedelta(days=3)).isoformat())
        act = [{"date": TODAY.isoformat(), "dividend": 100.0, "split": 0.0}]
        with mock.patch.object(rb, "compute_plan", wraps=rb.compute_plan) as plan:
            self.run_main([snap(A, 1000)], actions=lambda t: act)
        cash_used_in_plan = plan.call_args[0][0]
        self.assertAlmostEqual(cash_used_in_plan, 1000.0 + 10 * 100.0 * (1 - 0.20315), places=2)

    def test_trading_cost_reduces_cash_and_total_by_the_traded_notional_share(self):
        self.write()
        self.run_main([snap(A, 1000, 90)])
        st0, total0 = self.read(), json.loads(self.hist.read_text())[-1]["totalValue"]
        traded = sum(n * 1000 for n in st0["holdings"].values())
        self.assertGreater(traded, 0)

        self.hist.unlink()
        self.write()
        with mock.patch.object(ra, "SLIPPAGE_RATE", 0.01):
            self.run_main([snap(A, 1000, 90)])
        st1, total1 = self.read(), json.loads(self.hist.read_text())[-1]["totalValue"]
        self.assertAlmostEqual(st0["cash"] - st1["cash"], traded * 0.01, delta=0.01)
        self.assertAlmostEqual(total0 - total1, traded * 0.01, delta=1.0)

    def test_zero_cost_default_keeps_previous_numbers(self):
        self.assertEqual((ra.SLIPPAGE_RATE, ra.COMMISSION_RATE), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
