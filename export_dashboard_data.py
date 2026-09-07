"""
ダッシュボード(Artifact)用のデータを生成するスクリプト

使い方:
    python export_dashboard_data.py

やっていること:
    watchlist.py で全銘柄のスナップショットを取得し、screen.py の条件判定もかけた上で、
    logs/dashboard_data.json に1つのJSONとして書き出す。

    このJSONの中身を dashboard.html にそのまま埋め込むことで、
    「今日のダッシュボード」を自動更新できる。

    theme_candidates.json (theme_research.py が週次で生成)が存在すれば、
    「今週の注目テーマ」としてあわせて埋め込む。無ければテーマ欄は省略される。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import console_utf8

console_utf8.setup()

from screen import CRITERIA, passes
from watchlist import JAPANESE_NAMES, LOG_DIR, WATCHLIST, fetch_snapshot

BASE_DIR = Path(__file__).resolve().parent
THEME_CANDIDATES_PATH = BASE_DIR / "theme_candidates.json"


def load_themes(known_names: set[str]) -> dict | None:
    """theme_research.py(週次のGitHub Actions)が生成した候補を読み込む。

    ファイルが無ければ何もしない(まだ一度も実行されていない場合など)。
    """
    if not THEME_CANDIDATES_PATH.exists():
        return None
    data = json.loads(THEME_CANDIDATES_PATH.read_text(encoding="utf-8"))
    for theme in data.get("themes", []):
        for c in theme.get("candidates", []):
            c["inWatchlist"] = c.get("name") in known_names
    return data


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
                "rsi14": round(s.rsi14, 1) if s.rsi14 is not None else None,
                "macdHist": round(s.macd_hist, 2) if s.macd_hist is not None else None,
                "bbPct": round(s.bb_pct, 3) if s.bb_pct is not None else None,
                "buyScore": s.buy_score,
                "scoreReasons": s.score_reasons,
            }
        )

    known_names = {JAPANESE_NAMES.get(t, t) for t in WATCHLIST} | {s["name"] for s in stocks}
    themes = load_themes(known_names)

    data = {
        "generatedAt": now.isoformat(timespec="minutes"),
        "criteria": CRITERIA,
        "stocks": stocks,
        "themes": themes,
    }

    LOG_DIR.mkdir(exist_ok=True)
    out_path = LOG_DIR / "dashboard_data.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[保存しました] {out_path}  ({len(stocks)}銘柄)")


if __name__ == "__main__":
    main()
