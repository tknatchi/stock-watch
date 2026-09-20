import datetime as dt
import unittest
from unittest import mock

import market_calendar as mc


class MarketCalendarTests(unittest.TestCase):
    def test_weekend_is_not_a_trading_day(self):
        self.assertFalse(mc.is_trading_day(dt.date(2026, 9, 19)))  # 土

    def test_japanese_holiday_is_not_a_trading_day(self):
        self.assertFalse(mc.is_trading_day(dt.date(2026, 9, 22)))  # 国民の休日

    def test_year_end_and_new_year_closed(self):
        self.assertFalse(mc.is_trading_day(dt.date(2026, 12, 31)))
        self.assertFalse(mc.is_trading_day(dt.date(2027, 1, 2)))

    def test_regular_weekday_is_a_trading_day(self):
        self.assertTrue(mc.is_trading_day(dt.date(2026, 9, 17)))

    def test_session_boundaries(self):
        d = dt.date(2026, 9, 17)
        at = lambda h, m: mc.is_market_open(dt.datetime.combine(d, dt.time(h, m)))
        self.assertFalse(at(8, 59))
        self.assertTrue(at(9, 0))
        self.assertFalse(at(11, 30))   # 前場の終わりは含まない
        self.assertFalse(at(12, 0))    # 昼休み
        self.assertTrue(at(12, 30))
        self.assertTrue(at(15, 29))
        self.assertFalse(at(15, 30))   # 大引け後

    def test_aware_datetime_is_converted_to_jst(self):
        utc_10jst = dt.datetime(2026, 9, 17, 1, 0, tzinfo=dt.timezone.utc)  # = JST 10:00
        self.assertTrue(mc.is_market_open(utc_10jst))

    def test_holidays_unknown_without_jpholiday(self):
        with mock.patch.object(mc, "jpholiday", None):
            self.assertFalse(mc.holidays_known())
            self.assertTrue(mc.is_trading_day(dt.date(2026, 9, 22)))  # 判定不能=休場を検出できない(だから発注側で拒否する)


if __name__ == "__main__":
    unittest.main()
