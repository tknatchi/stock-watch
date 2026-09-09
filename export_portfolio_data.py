"""
ポートフォリオ・ダッシュボード(Artifact)用のデータを生成するスクリプト

使い方:
    python export_portfolio_data.py

やっていること:
    portfolio.json(現金・保有株数)と logs/portfolio_history.json(rebalance.py が
    日次で記録する資産推移)を読み込み、現在の保有内訳・目標比率とのズレ・
    資産推移の時系列を1つのJSON(logs/portfolio_dashboard_data.json)にまとめる。

    generate_portfolio_dashboard.py がこのJSONを portfolio_dashboard_template.html
    に埋め込み、portfolio_dashboard.html を生成する。

注意:
    portfolio.json は現金・保有株数という個人の資産情報。このスクリプトが出力する
    JSON/HTMLにも同じ情報が含まれるため、株モニタリング側のダッシュボードとは
    別のArtifactとして扱い、Claudeが手動で読んで公開する運用とする
    (自動の週次/日次クラウド再公開の対象には含めない)。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import console_utf8

console_utf8.setup()

from scoring import compute_target_weights
from watchlist import LOG_DIR, WATCHLIST, fetch_snapshot

BASE_DIR = Path(__file__).resolve().parent
PORTFOLIO_PATH = BASE_DIR / "portfolio.json"
HISTORY_PATH = LOG_DIR / "portfolio_history.json"

# rebalance.pyと揃える(こちらは表示用の参考値なので、実際の対象判定はrebalance.py側)
MIN_SCORE = 45
MAX_WEIGHT = 0.15


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    if not PORTFOLIO_PATH.exists():
        print(f"{PORTFOLIO_PATH.name} が見つかりません。先に rebalance.py を実行してください。")
        return

    portfolio = load_json(PORTFOLIO_PATH, {})
    cash = float(portfolio.get("cash", 0))
    holdings: dict[str, int] = portfolio.get("holdings", {})
    history = load_json(HISTORY_PATH, [])

    universe = sorted(set(WATCHLIST) | set(holdings.keys()))
    snapshots = [s for s in (fetch_snapshot(t) for t in universe) if s is not None]

    if not snapshots:
        print("銘柄データを取得できませんでした。")
        return

    target_weights = compute_target_weights(snapshots, min_score=MIN_SCORE, max_weight=MAX_WEIGHT)
    holdings_value = sum(holdings.get(s.ticker, 0) * s.price for s in snapshots)
    total_value = cash + holdings_value

    rows = []
    for s in snapshots:
        shares = holdings.get(s.ticker, 0)
        value = shares * s.price
        target_weight = target_weights.get(s.ticker, 0.0)
        if shares == 0 and target_weight == 0:
            continue
        rows.append(
            {
                "ticker": s.ticker,
                "name": s.name,
                "sector": s.sector,
                "shares": shares,
                "price": round(s.price, 1),
                "changePct": round(s.change_pct, 2),
                "value": round(value, 0),
                "weight": round(value / total_value, 4) if total_value else 0,
                "targetWeight": round(target_weight, 4),
                "buyScore": s.buy_score,
            }
        )
    rows.sort(key=lambda r: -r["value"])

    data = {
        "generatedAt": dt.datetime.now().isoformat(timespec="minutes"),
        "cash": round(cash, 0),
        "holdingsValue": round(holdings_value, 0),
        "totalValue": round(total_value, 0),
        "minScore": MIN_SCORE,
        "maxWeight": MAX_WEIGHT,
        "history": history,
        "holdings": rows,
    }

    LOG_DIR.mkdir(exist_ok=True)
    out_path = LOG_DIR / "portfolio_dashboard_data.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[保存しました] {out_path}  ({len(rows)}銘柄・合計{total_value:,.0f}円)")


if __name__ == "__main__":
    main()
