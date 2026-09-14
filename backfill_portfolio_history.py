"""
資産推移グラフの欠損日を、実際の過去終値から復元するための一回限りのバックフィルスクリプト。

背景:
    rebalance.py / rebalance_auto.py が history に1日1点を追記する仕組みだが、
    2026-09-09に実行が中断されて以来動いておらず、資産推移グラフがずっと1点のまま
    だった(portfolio.json / portfolio_auto.json の保有株数・現金は9/9時点から
    一切変わっていないことをログで確認済み)。そのため、9/9時点の保有株数を
    そのまま使い、欠けている営業日の終値だけを取得してhistoryに挿入する
    (架空の売買は行わない・仮定しない)。

使い方:
    python backfill_portfolio_history.py 2026-09-10 2026-09-11
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import yfinance as yf

import console_utf8

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent
PORTFOLIO_PATH = BASE_DIR / "portfolio.json"
PORTFOLIO_AUTO_PATH = BASE_DIR / "portfolio_auto.json"
HISTORY_PATH = BASE_DIR / "logs" / "portfolio_history.json"
HISTORY_AUTO_PATH = BASE_DIR / "logs" / "portfolio_auto_history.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_closes(tickers: list[str], dates: list[str]) -> dict[str, dict[str, float]]:
    start = min(dates)
    end = (dt.date.fromisoformat(max(dates)) + dt.timedelta(days=1)).isoformat()
    result: dict[str, dict[str, float]] = {}
    for t in tickers:
        h = yf.Ticker(t).history(start=start, end=end)
        by_date = {idx.strftime("%Y-%m-%d"): float(row["Close"]) for idx, row in h.iterrows()}
        missing = [d for d in dates if d not in by_date]
        if missing:
            print(f"  [警告] {t}: {missing} の終値が取得できませんでした")
        result[t] = by_date
    return result


def backfill(portfolio_path: Path, history_path: Path, dates: list[str], label: str) -> None:
    portfolio = load_json(portfolio_path, None)
    if portfolio is None:
        print(f"{label}: {portfolio_path.name} が無いのでスキップ")
        return

    cash = float(portfolio.get("cash", 0))
    holdings: dict[str, int] = portfolio.get("holdings", {})
    if not holdings:
        print(f"{label}: 保有銘柄が無いのでスキップ")
        return

    print(f"{label}: {sorted(holdings)} の終値を取得中...")
    closes = fetch_closes(sorted(holdings), dates)

    history = load_json(history_path, [])
    existing_dates = {h["date"] for h in history}

    for d in dates:
        if d in existing_dates:
            print(f"  {d} は既に記録済みなのでスキップ")
            continue
        holdings_value = 0.0
        ok = True
        for ticker, shares in holdings.items():
            price = closes.get(ticker, {}).get(d)
            if price is None:
                print(f"  [中止] {d}: {ticker} の終値が無いため、この日はスキップします")
                ok = False
                break
            holdings_value += shares * price
        if not ok:
            continue
        total_value = cash + holdings_value
        history.append(
            {
                "date": d,
                "cash": round(cash, 0),
                "holdingsValue": round(holdings_value, 0),
                "totalValue": round(total_value, 0),
            }
        )
        print(f"  [追加] {d}: 保有評価額 {holdings_value:,.0f}円 + 現金 {cash:,.0f}円 = {total_value:,.0f}円")

    history.sort(key=lambda h: h["date"])
    history_path.parent.mkdir(exist_ok=True)
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[保存しました] {history_path} ({len(history)}件)")


def main() -> None:
    dates = sys.argv[1:]
    if not dates:
        raise SystemExit("使い方: python backfill_portfolio_history.py YYYY-MM-DD [YYYY-MM-DD ...]")

    backfill(PORTFOLIO_PATH, HISTORY_PATH, dates, "バイ&ホールド組")
    print()
    backfill(PORTFOLIO_AUTO_PATH, HISTORY_AUTO_PATH, dates, "自動売買組")


if __name__ == "__main__":
    main()
