"""
株価ウォッチリスト・簡易分析ツール

使い方:
    python watchlist.py

やっていること:
    1. WATCHLIST に書いた銘柄コードの現在値・前日比・出来高を取得
    2. 25日/75日移動平均を計算してトレンドをざっくり判定
    3. PER・配当利回りなど基本指標を表示

銘柄コードの書き方:
    日本株  : "7203.T" (トヨタ), "6758.T" (ソニーG), "9984.T" (ソフトバンクG)
    米国株  : "AAPL", "MSFT", "NVDA" のようにそのまま

注意:
    - これは情報表示ツールであり、売買の助言ではありません。
    - Yahoo Finance 由来のデータのため、多少の遅延・誤差があり得ます。
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yfinance as yf

import console_utf8

console_utf8.setup()

# 実行結果を保存するフォルダ（このスクリプトと同じ場所に logs/ を作る）
LOG_DIR = Path(__file__).resolve().parent / "logs"

# ここに好きな銘柄コードを追加/削除してください（業種を分散させた例）
WATCHLIST = [
    "7203.T",  # トヨタ自動車 - 自動車
    "7267.T",  # ホンダ - 自動車
    "6758.T",  # ソニーグループ - 電機・エンタメ
    "6861.T",  # キーエンス - 精密機器
    "9432.T",  # NTT - 通信
    "9433.T",  # KDDI - 通信
    "9984.T",  # ソフトバンクグループ - 投資持株
    "8306.T",  # 三菱UFJフィナンシャル・グループ - 銀行
    "8316.T",  # 三井住友フィナンシャルグループ - 銀行
    "9983.T",  # ファーストリテイリング - 小売
    "3382.T",  # セブン&アイ・ホールディングス - 小売
    "4502.T",  # 武田薬品工業 - 医薬品
    "4568.T",  # 第一三共 - 医薬品
    "5020.T",  # ENEOSホールディングス - エネルギー
    "4063.T",  # 信越化学工業 - 化学
    "8058.T",  # 三菱商事 - 総合商社
    "8031.T",  # 三井物産 - 総合商社
    "7974.T",  # 任天堂 - ゲーム
    "2914.T",  # 日本たばこ産業 - 食品・たばこ
    "9201.T",  # 日本航空 - 航空
    "8801.T",  # 三井不動産 - 不動産
]

# 日本語の銘柄名（yfinanceは英語社名しか返さないため手動で用意）
# ここにない銘柄コードを追加した場合は、yfinanceが返す英語名にフォールバックする
JAPANESE_NAMES: dict[str, str] = {
    "7203.T": "トヨタ自動車",
    "7267.T": "本田技研工業",
    "6758.T": "ソニーグループ",
    "6861.T": "キーエンス",
    "9432.T": "日本電信電話",
    "9433.T": "KDDI",
    "9984.T": "ソフトバンクグループ",
    "8306.T": "三菱UFJフィナンシャル・グループ",
    "8316.T": "三井住友フィナンシャルグループ",
    "9983.T": "ファーストリテイリング",
    "3382.T": "セブン&アイ・ホールディングス",
    "4502.T": "武田薬品工業",
    "4568.T": "第一三共",
    "5020.T": "ENEOSホールディングス",
    "4063.T": "信越化学工業",
    "8058.T": "三菱商事",
    "8031.T": "三井物産",
    "7974.T": "任天堂",
    "2914.T": "日本たばこ産業",
    "9201.T": "日本航空",
    "8801.T": "三井不動産",
}

# ダッシュボード表示用の業種分類
SECTORS: dict[str, str] = {
    "7203.T": "自動車", "7267.T": "自動車",
    "6758.T": "電機・エンタメ", "6861.T": "精密機器",
    "9432.T": "通信", "9433.T": "通信",
    "9984.T": "投資持株",
    "8306.T": "銀行", "8316.T": "銀行",
    "9983.T": "小売", "3382.T": "小売",
    "4502.T": "医薬品", "4568.T": "医薬品",
    "5020.T": "エネルギー",
    "4063.T": "化学",
    "8058.T": "総合商社", "8031.T": "総合商社",
    "7974.T": "ゲーム",
    "2914.T": "食品・たばこ",
    "9201.T": "航空",
    "8801.T": "不動産",
}


@dataclass
class StockSnapshot:
    ticker: str
    name: str
    sector: str
    price: float
    prev_close: float
    change_pct: float
    volume: int
    ma25: float | None
    ma75: float | None
    trend: str
    pe_ratio: float | None
    dividend_yield: float | None
    history: list[float]  # 直近30営業日の終値（スパークライン用）


def fetch_snapshot(ticker: str) -> StockSnapshot | None:
    t = yf.Ticker(ticker)

    hist = t.history(period="6mo")
    if hist.empty:
        print(f"  [警告] {ticker}: データ取得失敗（銘柄コードを確認してください）", file=sys.stderr)
        return None

    info = t.info
    name = JAPANESE_NAMES.get(ticker) or info.get("longName") or info.get("shortName") or ticker

    price = float(hist["Close"].iloc[-1])
    prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price
    change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
    volume = int(hist["Volume"].iloc[-1])

    ma25 = float(hist["Close"].rolling(25).mean().iloc[-1]) if len(hist) >= 25 else None
    ma75 = float(hist["Close"].rolling(75).mean().iloc[-1]) if len(hist) >= 75 else None

    if ma25 is not None and ma75 is not None:
        if ma25 > ma75:
            trend = "上昇トレンド(短期>長期)"
        elif ma25 < ma75:
            trend = "下降トレンド(短期<長期)"
        else:
            trend = "横ばい"
    else:
        trend = "データ不足"

    pe_ratio = info.get("trailingPE")
    dividend_yield = info.get("dividendYield")
    history = [round(float(v), 1) for v in hist["Close"].tail(30).tolist()]

    return StockSnapshot(
        ticker=ticker,
        name=name,
        sector=SECTORS.get(ticker, "その他"),
        price=price,
        prev_close=prev_close,
        change_pct=change_pct,
        volume=volume,
        ma25=ma25,
        ma75=ma75,
        trend=trend,
        pe_ratio=pe_ratio,
        dividend_yield=dividend_yield,
        history=history,
    )


def build_table(snapshots: list[StockSnapshot]) -> pd.DataFrame:
    rows = []
    for s in snapshots:
        rows.append(
            {
                "コード": s.ticker,
                "銘柄名": s.name,
                "株価": round(s.price, 1),
                "前日比%": round(s.change_pct, 2),
                "MA25": round(s.ma25, 1) if s.ma25 else None,
                "MA75": round(s.ma75, 1) if s.ma75 else None,
                "トレンド": s.trend,
                "PER": round(s.pe_ratio, 1) if s.pe_ratio else None,
                "配当利回り%": round(s.dividend_yield, 2) if s.dividend_yield else None,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    now = dt.datetime.now()
    header = f"=== ウォッチリスト ({len(WATCHLIST)}銘柄) [{now:%Y-%m-%d %H:%M}] ==="

    lines = [header, ""]
    snapshots = []
    for ticker in WATCHLIST:
        s = fetch_snapshot(ticker)
        if s is not None:
            snapshots.append(s)

    if snapshots:
        lines.append(build_table(snapshots).to_string(index=False))
    else:
        lines.append("表示できる銘柄がありませんでした。")

    output = "\n".join(lines)

    # 画面に表示
    print(output)

    # logs/2026-09-04.txt のような日付ファイルに保存（1日1ファイル、実行のたびに上書き）
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{now:%Y-%m-%d}.txt"
    log_file.write_text(output + "\n", encoding="utf-8")
    print(f"\n[保存しました] {log_file}")


if __name__ == "__main__":
    main()
