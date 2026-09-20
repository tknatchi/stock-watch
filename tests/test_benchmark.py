import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import export_portfolio_data as epd


def fake_ticker(closes: dict[str, float] | Exception):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, start=None, end=None):
            if isinstance(closes, Exception):
                raise closes
            idx = pd.to_datetime(list(closes)).tz_localize("Asia/Tokyo")
            return pd.DataFrame({"Close": list(closes.values())}, index=idx)
    return FakeTicker


class BenchmarkTests(unittest.TestCase):
    def fetch(self, closes, dates, start_value=300_000):
        with mock.patch.object(epd.yf, "Ticker", fake_ticker(closes)):
            return epd.fetch_benchmark_history(dates, start_value)

    def test_series_starts_at_start_value_and_scales_by_close_ratio(self):
        r = self.fetch({"2026-09-09": 100.0, "2026-09-10": 110.0}, ["2026-09-09", "2026-09-10"])
        self.assertEqual([p["totalValue"] for p in r], [300_000, 330_000])

    def test_date_without_a_close_uses_the_previous_close(self):
        r = self.fetch({"2026-09-09": 100.0, "2026-09-10": 110.0}, ["2026-09-09", "2026-09-11"])
        self.assertEqual(r[1]["totalValue"], 330_000)

    def test_download_exception_returns_none_instead_of_crashing(self):
        self.assertIsNone(self.fetch(RuntimeError("429 Too Many Requests"), ["2026-09-09"]))

    def test_empty_price_data_returns_none(self):
        with mock.patch.object(epd.yf, "Ticker", lambda s: SimpleNamespace(history=lambda **kw: pd.DataFrame())):
            self.assertIsNone(epd.fetch_benchmark_history(["2026-09-09"], 300_000))

    def test_no_close_on_or_before_the_first_date_returns_none(self):
        self.assertIsNone(self.fetch({"2026-09-15": 100.0}, ["2026-09-09", "2026-09-15"]))

    def test_unadjusted_split_discontinuity_returns_none_instead_of_a_fake_crash(self):
        # 2559.T(2026年6月)のように分割が未調整のまま混入すると 30,357→3,045 と日次-90%になる
        closes = {"2026-06-03": 30_357.0, "2026-06-04": 30_400.0, "2026-06-05": 3_045.0, "2026-06-08": 3_050.0}
        self.assertIsNone(self.fetch(closes, list(closes)))

    def test_ordinary_large_daily_move_is_still_plotted(self):
        closes = {"2026-09-09": 100.0, "2026-09-10": 92.0}   # -8%: 暴落日でも比較線は出す
        self.assertIsNotNone(self.fetch(closes, list(closes)))

    def test_split_outside_the_requested_window_is_ignored(self):
        closes = {"2026-06-03": 30_357.0, "2026-06-05": 3_045.0, "2026-09-09": 3_000.0, "2026-09-10": 3_030.0}
        r = self.fetch(closes, ["2026-09-09", "2026-09-10"])
        self.assertEqual([p["totalValue"] for p in r], [300_000, 303_000])

    def test_no_dates_returns_none(self):
        self.assertIsNone(epd.fetch_benchmark_history([], 300_000))


class ExportMainTests(unittest.TestCase):
    """main() が benchmark を出力に載せる/失敗時は載せずに完走することの確認。"""

    def run_main(self, benchmark_result):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "portfolio.json").write_text(json.dumps({"cash": 1000, "holdings": {"A.T": 1}}), encoding="utf-8")
            hist = tmp / "hist.json"
            hist.write_text(json.dumps([{"date": "2026-09-09", "cash": 1000, "holdingsValue": 100, "totalValue": 1100}]), encoding="utf-8")
            s = SimpleNamespace(ticker="A.T", name="A", sector="x", price=100.0, change_pct=0.0, buy_score=50)
            with mock.patch.multiple(epd, PORTFOLIO_PATH=tmp / "portfolio.json", PORTFOLIO_AUTO_PATH=tmp / "none.json",
                                     HISTORY_PATH=hist, HISTORY_AUTO_PATH=tmp / "none2.json", LOG_DIR=tmp / "logs",
                                     WATCHLIST=["A.T"]), \
                 mock.patch.object(epd, "fetch_snapshot", return_value=s), \
                 mock.patch.object(epd, "fetch_benchmark_history", return_value=benchmark_result):
                epd.main()
            return json.loads((tmp / "logs" / "portfolio_dashboard_data.json").read_text(encoding="utf-8"))

    def test_benchmark_is_included_in_dashboard_data_when_available(self):
        data = self.run_main([{"date": "2026-09-09", "totalValue": 1100.0}])
        self.assertEqual(data["benchmark"]["ticker"], "2559.T")
        self.assertEqual(data["benchmark"]["history"][0]["totalValue"], 1100.0)

    def test_dashboard_data_is_still_written_without_benchmark_on_failure(self):
        data = self.run_main(None)
        self.assertNotIn("benchmark", data)
        self.assertEqual(data["totalValue"], 1100)


if __name__ == "__main__":
    unittest.main()
