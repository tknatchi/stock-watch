"""
東京証券取引所の営業日・取引時間の判定(実発注の安全装置用)

jpholiday が入っていない環境では祝日を判定できない。その場合 holidays_known() が
False を返し、実発注側(trade_guard.py)は「祝日かもしれない日に発注しない」ために
発注を拒否する(ペーパートレードや通知だけなら支障なし)。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

try:
    import jpholiday
except ImportError:  # pragma: no cover - 環境依存
    jpholiday = None

JST = ZoneInfo("Asia/Tokyo")

# 東証の取引時間(2024-11-05以降、大引けは15:30)
SESSIONS = ((dt.time(9, 0), dt.time(11, 30)), (dt.time(12, 30), dt.time(15, 30)))


def holidays_known() -> bool:
    return jpholiday is not None


def is_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in {(12, 31), (1, 1), (1, 2), (1, 3)}:  # 東証の年末年始休場
        return False
    if jpholiday is not None and jpholiday.is_holiday(d):
        return False
    return True


def is_market_open(now: dt.datetime) -> bool:
    """now(tz付き推奨。tzなしならJSTとみなす)が東証の取引時間内かどうか。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    now = now.astimezone(JST)
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return any(start <= t < end for start, end in SESSIONS)
