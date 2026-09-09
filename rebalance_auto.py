"""
仮想の「毎日自動売買」ポートフォリオを1日進めるスクリプト

これは何のためのものか:
    rebalance.py は「月1回程度、提案を見て自分で判断して売買する」運用を前提にしている。
    それとは別に、「もし毎日スコアに追随して機械的に売買していたらどうなるか」を
    実際のデータで確認するための、完全に仮想のペーパートレード実験用スクリプト。

    portfolio.json(バイ&ホールド組、rebalance.py が提案するだけで何もしない)とは
    別に portfolio_auto.json(自動売買組)を用意し、こちらは実行するたびに
    rebalance.py と同じロジックの提案を"そのまま全部適用"する。
    同じ開始時点から分岐させることで、資産推移を比較できるようにしている。

前提:
    - 実際の資金は一切動かさない、完全な仮想シミュレーション
    - portfolio_auto.json は現金・保有株数という情報を含むため .gitignore 済み

使い方:
    1. 初回のみ: portfolio.json (バイ&ホールド組の開始時点) を
       portfolio_auto.json という名前でコピーして、同じ開始状態を作る
    2. python rebalance_auto.py を実行するたびに、その時点のスコアに基づく
       提案を全て適用し、portfolio_auto.json を書き換える
    3. run_watchlist.bat から日次で自動実行される(portfolio_auto.jsonがあれば)
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import console_utf8
import rebalance as rb
from watchlist import LOG_DIR

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent
PORTFOLIO_AUTO_PATH = BASE_DIR / "portfolio_auto.json"
HISTORY_AUTO_PATH = LOG_DIR / "portfolio_auto_history.json"


def main() -> None:
    if not PORTFOLIO_AUTO_PATH.exists():
        raise SystemExit(
            f"{PORTFOLIO_AUTO_PATH.name} が見つかりません。\n"
            f"portfolio.json(バイ&ホールド組の開始時点)を portfolio_auto.json という"
            f"名前でコピーして、同じ開始状態を作ってから再実行してください。"
        )

    portfolio = json.loads(PORTFOLIO_AUTO_PATH.read_text(encoding="utf-8"))
    cash = float(portfolio.get("cash", 0))
    holdings: dict[str, int] = dict(portfolio.get("holdings", {}))

    snapshots = rb.fetch_universe_snapshots(holdings)
    if not snapshots:
        print("銘柄データを取得できませんでした。")
        return

    plan = rb.compute_plan(cash, holdings, snapshots)
    if plan["total_value"] <= 0:
        print("現金・保有評価額がともに0円のため計算できません。portfolio_auto.json を確認してください。")
        return

    now = dt.datetime.now()
    lines = [f"=== 自動売買(仮想)実行ログ [{now:%Y-%m-%d %H:%M}] ===", ""]

    def emit(line: str = "") -> None:
        lines.append(line)
        print(line)

    emit(f"実行前: 現金 {cash:,.0f}円 + 保有評価額 {plan['holdings_value']:,.0f}円 "
         f"= 合計 {plan['total_value']:,.0f}円")
    emit()

    # rebalance.py の提案(rows の action_shares)をそのまま全部適用する
    traded = False
    for r in plan["rows"]:
        if r["action_shares"] == 0:
            continue
        traded = True
        ticker = r["ticker"]
        new_shares = holdings.get(ticker, 0) + r["action_shares"]
        if new_shares <= 0:
            holdings.pop(ticker, None)
        else:
            holdings[ticker] = new_shares

        verb = "買い増し" if r["action_shares"] > 0 else "売却"
        cost = abs(r["action_shares"]) * r["price"]
        emit(f"[{r['ticker']}] {r['name']}: {verb} {abs(r['action_shares'])}株（概算 {cost:,.0f}円）")

    new_cash = plan["leftover_cash"]

    if not traded:
        emit("本日の売買はありませんでした（目標比率とのズレがしきい値未満）。")

    emit()
    emit(f"実行後: 現金 {new_cash:,.0f}円 + 保有評価額 {plan['total_value'] - new_cash:,.0f}円 "
         f"= 合計 {plan['total_value']:,.0f}円")
    emit()
    emit("※完全な仮想シミュレーションです。実際の資金は一切動いていません。")

    PORTFOLIO_AUTO_PATH.write_text(
        json.dumps({"cash": round(new_cash, 2), "holdings": holdings}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rb.record_history(now, new_cash, plan["total_value"] - new_cash, plan["total_value"], path=HISTORY_AUTO_PATH)

    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"rebalance_auto_{now:%Y-%m-%d}.txt"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[保存しました] {log_file}")
    print(f"[更新しました] {PORTFOLIO_AUTO_PATH}")


if __name__ == "__main__":
    main()
