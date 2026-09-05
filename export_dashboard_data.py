"""
ダッシュボード(Artifact)用のデータを生成するスクリプト

使い方:
    python export_dashboard_data.py

やっていること:
    watchlist.py で全銘柄のスナップショットを取得し、screen.py の条件判定もかけた上で、
    logs/dashboard_data.json に1つのJSONとして書き出す。

    このJSONの中身を dashboard.html にそのまま埋め込むことで、
    「今日のダッシュボード」を自動更新できる。
"""

from __future__ import annotations

import datetime as dt
import json

import console_utf8

console_utf8.setup()

from screen import CRITERIA, passes
from watchlist import LOG_DIR, WATCHLIST, fetch_snapshot


def main() -> None:
    now = dt.datetime.now()
    stocks = []

    for ticker in WATCHLIST:
        s = fetch_snapshot(ticker)
        if s is None:
            continue
        matched, reasons = passes(s)
        stocks.append(
            {
                "ticker": s.ticker,
                "name": s.name,
                "sector": s.sector,
                "price": round(s.price, 1),
                "prevClose": round(s.prev_close, 1),
                "changePct": round(s.change_pct, 2),
                "volume": s.volume,
                "ma25": round(s.ma25, 1) if s.ma25 else None,
                "ma75": round(s.ma75, 1) if s.ma75 else None,
                "trend": s.trend,
                "peRatio": round(s.pe_ratio, 1) if s.pe_ratio else None,
                "dividendYield": round(s.dividend_yield, 2) if s.dividend_yield else None,
                "history": s.history,
                "matched": matched,
                "matchReasons": reasons,
            }
        )

    data = {
        "generatedAt": now.isoformat(timespec="minutes"),
        "criteria": CRITERIA,
        "stocks": stocks,
    }

    LOG_DIR.mkdir(exist_ok=True)
    out_path = LOG_DIR / "dashboard_data.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[保存しました] {out_path}  ({len(stocks)}銘柄)")


if __name__ == "__main__":
    main()
