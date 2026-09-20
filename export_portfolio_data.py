"""
ポートフォリオ・ダッシュボード(Artifact)用のデータを生成するスクリプト

使い方:
    python export_portfolio_data.py

やっていること:
    portfolio.json(バイ&ホールド組・現金と保有株数)と、存在すれば portfolio_auto.json
    (仮想の毎日自動売買組)の両方について、現在の保有内訳・目標比率とのズレ・
    資産推移の時系列(logs/portfolio_history.json, logs/portfolio_auto_history.json)
    をまとめて1つのJSON(logs/portfolio_dashboard_data.json)に出力する。

    あわせて、バイ&ホールド組の開始時点と同額を「NISAでオルカンに入れていたら」の
    推移(BENCHMARK_TICKER=2559.T代替、data["benchmark"])も計算して添える。

    generate_portfolio_dashboard.py がこのJSONを portfolio_dashboard_template.html
    に埋め込み、portfolio_dashboard.html を生成する。portfolio_auto.json が無い場合は
    バイ&ホールド組のみのダッシュボードになる。

注意:
    portfolio.json / portfolio_auto.json は現金・保有株数という個人の資産情報。
    このスクリプトが出力するJSON(logs/portfolio_dashboard_data.json)にも同じ情報が
    含まれる。2026-09-10以降、ユーザーの承認のうえで、この金額入りデータを
    portfolio_dashboard_data_latest.json としてリポジトリにpushし、株モニタリングと
    同じダッシュボード(combined_dashboard_template.html、タブ切り替え)に載せて
    クラウド側の日次自動再公開の対象に含める運用に変更した。
    (portfolio.json / portfolio_auto.json 自体は引き続き.gitignore対象で非公開)
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import yfinance as yf

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

# 「NISAでオルカン(eMAXIS Slim 全世界株式(オール・カントリー))を買っていたら」の比較用ベンチマーク。
# オルカン自体(投資信託)の基準価額はyfinanceから安定して取得できないため、同じMSCI ACWIに
# 連動する東証上場ETF「2559 MAXIS全世界株式(オール・カントリー)上場投信」を代替指標として使う。
# 信託報酬もオルカンとほぼ同水準(年0.0576% vs 0.05775%)なので、長期の累積リターンはほぼ一致する想定。
BENCHMARK_TICKER = "2559.T"
BENCHMARK_LABEL = "オルカン(NISA想定, 2559.T代替)"


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


def fetch_benchmark_history(dates: list[str], start_value: float) -> list[dict] | None:
    """dates の各日について、start_value を開始日にベンチマークへ全額投入していたら
    その日の評価額がいくらになっているかを終値ベースで計算する。

    取得失敗時(ネットワークエラー・銘柄コード変更など)はNoneを返し、呼び出し側は
    ベンチマーク無しでダッシュボードを生成する(既存データの表示を止めない)。
    """
    if not dates:
        return None
    try:
        start = min(dates)
        end = (dt.date.fromisoformat(max(dates)) + dt.timedelta(days=1)).isoformat()
        h = yf.Ticker(BENCHMARK_TICKER).history(start=start, end=end)
        if h.empty:
            print(f"[警告] ベンチマーク({BENCHMARK_TICKER})の終値が取得できませんでした")
            return None
        closes = {idx.strftime("%Y-%m-%d"): float(row["Close"]) for idx, row in h.iterrows()}
    except Exception as e:
        print(f"[警告] ベンチマーク({BENCHMARK_TICKER})の取得に失敗しました: {e}")
        return None

    sorted_close_dates = sorted(closes)
    if not sorted_close_dates:
        return None

    def price_asof(d: str) -> float | None:
        # d以前で最も近い終値を使う(祝日などでdその日の終値が無い場合に備える)
        candidates = [cd for cd in sorted_close_dates if cd <= d]
        return closes[candidates[-1]] if candidates else None

    base_price = price_asof(dates[0])
    if base_price is None:
        return None

    result = []
    for d in dates:
        price = price_asof(d)
        if price is None:
            continue
        result.append({"date": d, "totalValue": round(start_value * price / base_price, 0)})
    return result


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

    # バイ&ホールド組の開始時点(同額・同日でportfolio_auto.jsonも始めている前提)を基準に、
    # 「同じ額をNISAでオルカンに入れていたら」の推移を計算して添える。
    benchmark_dates = [h["date"] for h in manual_view["history"]]
    benchmark_start_value = manual_view["history"][0]["totalValue"] if manual_view["history"] else manual_view["totalValue"]
    if benchmark_dates:
        benchmark_history = fetch_benchmark_history(benchmark_dates, benchmark_start_value)
        if benchmark_history:
            data["benchmark"] = {
                "ticker": BENCHMARK_TICKER,
                "label": BENCHMARK_LABEL,
                "history": benchmark_history,
            }

    LOG_DIR.mkdir(exist_ok=True)
    out_path = LOG_DIR / "portfolio_dashboard_data.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = f"バイ&ホールド {len(manual_view['holdings'])}銘柄・{manual_view['totalValue']:,.0f}円"
    if has_auto:
        summary += f" / 自動売買 {len(data['auto']['holdings'])}銘柄・{data['auto']['totalValue']:,.0f}円"
    if "benchmark" in data:
        bench_last = data["benchmark"]["history"][-1]["totalValue"]
        summary += f" / {BENCHMARK_LABEL} {bench_last:,.0f}円"
    print(f"[保存しました] {out_path}  ({summary})")


if __name__ == "__main__":
    main()
