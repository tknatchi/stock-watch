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
