"""
予算・保有株数から、目標配分と売買提案を計算するツール（月次リバランス想定）

前提・設計方針:
    - このツールは「提案」を出すだけで、自動発注は一切しない（売買は必ず自分の判断・手動で）
    - 保有状況(portfolio.json)は証券会社と自動連携しない。実際に売買したら自分で書き換える
    - 買いスコア(RSI・MACD等)は日々変動するが、日次で反応すると税コスト・手数料で損をしやすいため、
      このツールは月1回など間隔を空けて実行することを想定している（毎日は実行しない）
    - portfolio.json は現金・保有株数という個人の資産情報なので .gitignore 済み。
      コミットしないこと

使い方:
    1. portfolio.example.json を portfolio.json という名前でコピーする
    2. cash（現金）と holdings（銘柄コード: 保有株数）を実際の状況に書き換える
       （保有していない銘柄は書かなくてよい。空の状態で実行すれば「初回の買い付けプラン」になる）
    3. python rebalance.py を実行する

出力の読み方:
    銘柄ごとに「現在の評価額」と「目標評価額」を比較し、そのズレが
    ポートフォリオ全体に対してDRIFT_THRESHOLD以上ある銘柄だけ、売買を提案する。
    小さなズレでは何も提案しない（ノイズで売買しないようにするため）。
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import console_utf8
from scoring import compute_target_weights
from watchlist import LOG_DIR, WATCHLIST, fetch_snapshot

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent
PORTFOLIO_PATH = BASE_DIR / "portfolio.json"

# ==== ここを書き換えてカスタマイズする ====
MIN_SCORE = 45          # この買いスコア未満の銘柄は新規の投資対象から除外
MAX_WEIGHT = 0.15       # 1銘柄への集中を防ぐ上限比率（15%）
DRIFT_THRESHOLD = 0.02  # 目標比率からのズレがポートフォリオ全体比でこれを超えたら売買提案
LOT_SIZE = 100          # 単元株数（東証は原則100株単位）
# ===========================================


def load_portfolio() -> dict:
    if not PORTFOLIO_PATH.exists():
        raise SystemExit(
            f"{PORTFOLIO_PATH.name} が見つかりません。\n"
            f"portfolio.example.json をコピーして portfolio.json を作成し、"
            f"cash・holdings を書き換えてから再実行してください。"
        )
    return json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))


def main() -> None:
    portfolio = load_portfolio()
    cash = float(portfolio.get("cash", 0))
    holdings: dict[str, int] = portfolio.get("holdings", {})

    # WATCHLIST(監視銘柄) + 実際に保有している銘柄(監視外でもよい)をまとめて取得
    universe = sorted(set(WATCHLIST) | set(holdings.keys()))
    snapshots = []
    for ticker in universe:
        s = fetch_snapshot(ticker)
        if s is not None:
            snapshots.append(s)

    if not snapshots:
        print("銘柄データを取得できませんでした。")
        return

    target_weights = compute_target_weights(snapshots, min_score=MIN_SCORE, max_weight=MAX_WEIGHT)
    holdings_value = sum(holdings.get(s.ticker, 0) * s.price for s in snapshots)
    total_value = cash + holdings_value

    now = dt.datetime.now()
    lines = [f"=== ポートフォリオ・リバランス提案 [{now:%Y-%m-%d %H:%M}] ===", ""]

    def emit(line: str = "") -> None:
        lines.append(line)
        print(line)

    emit(f"現金: {cash:,.0f}円  保有評価額: {holdings_value:,.0f}円  合計: {total_value:,.0f}円")
    emit(f"（買いスコア{MIN_SCORE}未満は新規対象外・1銘柄上限{MAX_WEIGHT*100:.0f}%・"
         f"乖離{DRIFT_THRESHOLD*100:.0f}%未満は提案なし）")
    emit()

    if total_value <= 0:
        emit("現金・保有評価額がともに0円のため計算できません。portfolio.json を確認してください。")
        return

    rows = []
    for s in snapshots:
        current_shares = holdings.get(s.ticker, 0)
        current_value = current_shares * s.price
        target_weight = target_weights.get(s.ticker, 0.0)
        target_value = target_weight * total_value
        drift_value = target_value - current_value
        drift_ratio = drift_value / total_value

        if current_shares == 0 and target_weight == 0:
            continue  # 保有なし・新規対象外の銘柄は表示しない

        action_shares = 0
        if abs(drift_ratio) >= DRIFT_THRESHOLD:
            # 0方向へ切り捨て（trunc）。四捨五入だと目標額を超えて買い越し／売り越しになり、
            # 銘柄数が多いと現金残高がマイナスになりうるため、常に目標のズレの範囲内に収める。
            lots = math.trunc(drift_value / s.price / LOT_SIZE)
            action_shares = lots * LOT_SIZE
            if action_shares < 0:
                action_shares = max(action_shares, -current_shares)  # 保有以上には売らない

        rows.append(
            {
                "ticker": s.ticker,
                "name": s.name,
                "score": s.buy_score,
                "price": s.price,
                "current_shares": current_shares,
                "current_value": current_value,
                "target_weight": target_weight,
                "target_value": target_value,
                "action_shares": action_shares,
            }
        )

    rows.sort(key=lambda r: (-r["target_weight"], -r["current_value"]))

    net_cash_flow = 0.0
    for r in rows:
        flag = "  ⚠対象外（スコア低下・全売却の目安）" if r["target_weight"] == 0 and r["current_shares"] > 0 else ""
        emit(f"[{r['ticker']}] {r['name']}{flag}")
        emit(
            f"  スコア {r['score']:>3} / 目標比率 {r['target_weight']*100:5.1f}% "
            f"/ 現在 {r['current_shares']:>5}株（評価額 {r['current_value']:>10,.0f}円）"
            f"/ 目標評価額 {r['target_value']:>10,.0f}円"
        )
        if r["action_shares"] != 0:
            verb = "買い増し" if r["action_shares"] > 0 else "売却"
            cost = abs(r["action_shares"]) * r["price"]
            emit(f"  → 提案: {verb} {abs(r['action_shares'])}株（概算 {cost:,.0f}円）")
            net_cash_flow -= r["action_shares"] * r["price"]
        emit()

    if not rows:
        emit(f"買いスコア{MIN_SCORE}以上の銘柄がありません。MIN_SCOREを見直すか、日を改めて実行してください。")

    emit(f"提案どおり売買した場合の現金残高目安: {cash + net_cash_flow:,.0f}円"
         f"（単元株数への丸め誤差あり）")
    emit()
    emit("※これは売買の提案であり、自動発注は一切行いません。実際の売買は自分の判断で証券会社にて行ってください。")
    emit("※日々のスコア変動には反応せず、月1回など間隔を空けて実行することを推奨します。")

    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"rebalance_{now:%Y-%m-%d}.txt"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[保存しました] {log_file}")


if __name__ == "__main__":
    main()
