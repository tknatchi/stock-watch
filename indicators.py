"""
テクニカル指標の計算ユーティリティ

RSI・MACD・ボリンジャーバンドを pandas の終値Seriesから計算する。
どれも「直近の値だけ」ではなく系列(pd.Series)全体を返すので、
呼び出し側で `.iloc[-1]` して最新値を取り出す想定。
"""

from __future__ import annotations

import pandas as pd


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI(相対力指数)。Wilderの平滑化(EMA近似)で計算。

    0〜100の範囲で、一般に70以上は買われすぎ・30以下は売られすぎとされる。
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD。(MACD線, シグナル線, ヒストグラム=MACD線-シグナル線) を返す。

    ヒストグラムがプラスに転じるのが「ゴールデンクロス」方向のシグナル。
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def calc_bollinger(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ボリンジャーバンド。(上限バンド, 下限バンド, 中心線=移動平均) を返す。"""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, lower, mid
