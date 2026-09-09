"""
ポートフォリオ・ダッシュボード(Artifact)用のデータを生成するスクリプト

使い方:
    python export_portfolio_data.py

やっていること:
    portfolio.json(バイ&ホールド組・現金と保有株数)と、存在すれば portfolio_auto.json
    (仮想の毎日自動売買組)の両方について、現在の保有内訳・目標比率とのズレ・
    資産推移の時系列(logs/portfolio_history.json, logs/portfolio_auto_history.json)
    をまとめて1つのJSON(logs/portfolio_dashboard_data.json)に出力する。

    generate_portfolio_dashboard.py がこのJSONを portfolio_dashboard_template.html
    に埋め込み、portfolio_dashboard.html を生成する。portfolio_auto.json が無い場合は
    バイ&ホールド組のみのダッシュボードになる。

注意:
    portfolio.json / portfolio_auto.json は現金・保有株数という個人の資産情報。
    このスクリプトが出力するJSON/HTMLにも同じ情報が含まれるため、株モニタリング側の
    ダッシュボードとは別のArtifactとして扱い、Claudeが手動で読んで公開する運用とする
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
PORTFOLIO_AUTO_PATH = BASE_DIR / "portfolio_auto.json"
HISTORY_PATH = LOG_DIR / "portfolio_history.json"
HISTORY_AUTO_PATH = LOG_DIR / "portfolio_auto_history.json"

# rebalance.pyと揃える(こちらは表示用の参考値なので、実際の対象判定はrebalance.py側)
MIN_SCORE = 45
MAX_WEIGHT = 0.15


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def build_portfolio_view(cash: float, holdings: dict[str, int], history: list, snapshots: list, snapshot_by_ticker: dict) -> dict:
    target_weights = compute_target_weights(snapshots, min_score=MIN_SCORE, max_weight=MAX_WEIGHT)
    holdings_value = sum(holdings.get(t, 0) * s.price for t, s in snapshot_by_ticker.items())
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

    return {
        "cash": round(cash, 0),
        "holdingsValue": round(holdings_value, 0),
        "totalValue": round(total_value, 0),
        "history": history,
        "holdings": rows,
    }


def main() -> None:
    if not PORTFOLIO_PATH.exists():
        print(f"{PORTFOLIO_PATH.name} が見つかりません。先に rebalance.py を実行してください。")
        return

    portfolio = load_json(PORTFOLIO_PATH, {})
    holdings: dict[str, int] = portfolio.get("holdings", {})

    has_auto = PORTFOLIO_AUTO_PATH.exists()
    portfolio_auto = load_json(PORTFOLIO_AUTO_PATH, {}) if has_auto else {}
    holdings_auto: dict[str, int] = portfolio_auto.get("holdings", {})

    universe = sorted(set(WATCHLIST) | set(holdings.keys()) | set(holdings_auto.keys()))
    snapshots = [s for s in (fetch_snapshot(t) for t in universe) if s is not None]
    if not snapshots:
        print("銘柄データを取得できませんでした。")
        return
    snapshot_by_ticker = {s.ticker: s for s in snapshots}

    manual_view = build_portfolio_view(
        float(portfolio.get("cash", 0)), holdings, load_json(HISTORY_PATH, []), snapshots, snapshot_by_ticker
    )

    data = {
        "generatedAt": dt.datetime.now().isoformat(timespec="minutes"),
        "minScore": MIN_SCORE,
        "maxWeight": MAX_WEIGHT,
        **manual_view,
    }

    if has_auto:
        auto_view = build_portfolio_view(
            float(portfolio_auto.get("cash", 0)),
            holdings_auto,
            load_json(HISTORY_AUTO_PATH, []),
            snapshots,
            snapshot_by_ticker,
        )
        data["auto"] = auto_view

    LOG_DIR.mkdir(exist_ok=True)
    out_path = LOG_DIR / "portfolio_dashboard_data.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = f"バイ&ホールド {len(manual_view['holdings'])}銘柄・{manual_view['totalValue']:,.0f}円"
    if has_auto:
        summary += f" / 自動売買 {len(data['auto']['holdings'])}銘柄・{data['auto']['totalValue']:,.0f}円"
    print(f"[保存しました] {out_path}  ({summary})")


if __name__ == "__main__":
    main()
