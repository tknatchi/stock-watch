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

損切りルールについて(2026-09-14追加):
    portfolio_auto.json の costBasis(銘柄ごとの加重平均取得単価。買い増しのたびに
    このスクリプトが自動更新する)を使い、現在値が取得単価からrebalance.py の
    STOP_LOSS_THRESHOLD以上下落した銘柄は、スコアに関係なく全売却を実際に実行する
    (rebalance.py と違い、こちらは仮想シミュレーションなのでそのまま自動適用してよい)。
    損切りが発動した場合はLINE_CHANNEL_ACCESS_TOKENが設定されていればLINEにも
    アラートを送る(line_alert.py)。

クールダウンについて(2026-09-14追加):
    損切り直後は急落の反動でPER・RSIなどのスコアがむしろ上がりやすく、対策なしだと
    次の実行で同じ銘柄を即買い戻してしまう。これを防ぐため、損切りした銘柄は
    STOP_LOSS_COOLDOWN_RUNS回(このスクリプトの実行回数ベース。日次実行前提なので
    おおむね営業日数に相当)は新規の買い候補から除外する。portfolio_auto.jsonの
    cooldownフィールドで残り回数を管理し、実行のたびに1減らして0になったら解除する。
    まずは推奨値(10営業日)で運用し、様子を見ながら調整する想定。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import console_utf8
import corporate_actions
import rebalance as rb
from line_alert import send_line_alert
from watchlist import LOG_DIR

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent
PORTFOLIO_AUTO_PATH = BASE_DIR / "portfolio_auto.json"
HISTORY_AUTO_PATH = LOG_DIR / "portfolio_auto_history.json"

# 損切り後のクールダウン期間(このスクリプトの実行回数ベース。日次実行前提)。
# 推奨値として10営業日(約2週間)からスタートし、様子を見て調整する。
STOP_LOSS_COOLDOWN_RUNS = 10

# 売買コスト(売買代金に対する片道の比率)。既存の比較実験の連続性を保つため既定は0。
# 実弾では手数料・スプレッド・スリッページで売買のたびに目減りするので、現実に近づけたい
# ときは 0.001(=0.1%)などに変える。0のままでも、ログには「0.1%と仮定した場合の目安」を出す。
SLIPPAGE_RATE = 0.0
COMMISSION_RATE = 0.0
ASSUMED_COST_RATE = 0.001


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
    cost_basis: dict[str, float] = dict(portfolio.get("costBasis", {}))

    # クールダウン: 前回までの残り回数を1消化(0になったら解除)。今回発動した損切り分は
    # このあと trade 適用後に新規追加するので、ここではまだ加えない。
    cooldown: dict[str, int] = {
        t: d - 1 for t, d in dict(portfolio.get("cooldown", {})).items() if d - 1 > 0
    }

    snapshots = rb.fetch_universe_snapshots(holdings)
    if not snapshots:
        print("銘柄データを取得できませんでした。")
        return

    missing = rb.find_missing_holdings(holdings, snapshots)
    if missing:
        print(f"保有銘柄の株価を取得できませんでした({', '.join(missing)})。総資産を誤って計算するため、今回の売買・記録は見送ります。")
        return

    # 配当・株式分割の反映。初回は遡及せず基準日だけ記録する。取得に失敗したら基準日を
    # 進めず、次回の実行で取りこぼしなく再挑戦する。
    today = dt.date.today()
    action_events: list[str] = []
    next_action_check = portfolio.get("lastActionCheck")
    if next_action_check is None:
        next_action_check = today.isoformat()
    else:
        try:
            actions = {t: corporate_actions.fetch_actions(t) for t in holdings}
        except Exception as e:
            print(f"[警告] 配当・分割情報の取得に失敗したため今回は反映を見送ります: {e}")
        else:
            holdings, cost_basis, cash, action_events = corporate_actions.apply_actions(
                holdings, cost_basis, cash, actions, dt.date.fromisoformat(next_action_check), today
            )
            next_action_check = today.isoformat()

    plan = rb.compute_plan(cash, holdings, snapshots, cost_basis, set(cooldown))
    if plan["total_value"] <= 0:
        print("現金・保有評価額がともに0円のため計算できません。portfolio_auto.json を確認してください。")
        return

    now = dt.datetime.now()
    lines = [f"=== 自動売買(仮想)実行ログ [{now:%Y-%m-%d %H:%M}] ===", ""]

    def emit(line: str = "") -> None:
        lines.append(line)
        print(line)

    if action_events:
        emit("配当・株式分割を反映しました:")
        for ev in action_events:
            emit(f"  {ev}")
        emit()

    emit(f"実行前: 現金 {cash:,.0f}円 + 保有評価額 {plan['holdings_value']:,.0f}円 "
         f"= 合計 {plan['total_value']:,.0f}円")
    emit()

    stop_loss_rows = plan["stop_loss_rows"]
    if stop_loss_rows:
        emit("🚨 損切り発動(全額売却を自動実行) 🚨")
        for r in stop_loss_rows:
            emit(
                f"  [{r['ticker']}] {r['name']}: 取得単価 {r['cost_basis']:,.1f}円 → 現在値 {r['price']:,.1f}円 "
                f"（{r['loss_pct']*100:+.1f}%）"
            )
        emit()

    # rebalance.py の提案(rows の action_shares)をそのまま全部適用する
    # (損切り対象行は compute_plan 側で action_shares=-保有数 に強制済み)
    traded = False
    traded_notional = 0.0
    for r in plan["rows"]:
        if r["action_shares"] == 0:
            continue
        traded = True
        traded_notional += abs(r["action_shares"]) * r["price"]
        ticker = r["ticker"]
        old_shares = holdings.get(ticker, 0)
        new_shares = old_shares + r["action_shares"]

        if r["action_shares"] > 0:
            # 買い増し: 加重平均で取得単価を更新(新規建ての場合はそのまま今回の価格)
            old_basis = cost_basis.get(ticker, r["price"])
            new_basis = (old_shares * old_basis + r["action_shares"] * r["price"]) / new_shares if new_shares else r["price"]
            cost_basis[ticker] = round(new_basis, 2)

        if new_shares <= 0:
            holdings.pop(ticker, None)
            cost_basis.pop(ticker, None)  # 全売却したら取得単価もリセット
        else:
            holdings[ticker] = new_shares

        verb = "買い増し" if r["action_shares"] > 0 else "売却"
        tag = " [損切り]" if r["stop_loss"] else ""
        cost = abs(r["action_shares"]) * r["price"]
        emit(f"[{r['ticker']}] {r['name']}: {verb} {abs(r['action_shares'])}株（概算 {cost:,.0f}円）{tag}")

        if r["stop_loss"]:
            # 損切りした銘柄はクールダウンを開始(今回の実行分は消化済み扱いにしないので、
            # 次回実行時の1回目の減算からカウントが始まる)
            cooldown[ticker] = STOP_LOSS_COOLDOWN_RUNS

    trading_cost = traded_notional * (SLIPPAGE_RATE + COMMISSION_RATE)
    new_cash = plan["leftover_cash"] - trading_cost
    total_after = plan["total_value"] - trading_cost

    if not traded:
        emit("本日の売買はありませんでした（目標比率とのズレがしきい値未満）。")
    else:
        emit()
        emit(f"売買代金 {traded_notional:,.0f}円 / 売買コスト {trading_cost:,.0f}円"
             f"（参考: コスト{ASSUMED_COST_RATE*100:.1f}%と仮定すると {traded_notional * ASSUMED_COST_RATE:,.0f}円）")
        if new_cash < 0:
            emit(f"[警告] コスト控除後の現金がマイナス({new_cash:,.0f}円)になりました。")

    cooldown_rows = [r for r in plan["rows"] if r.get("cooldown")]
    if cooldown_rows:
        emit()
        emit("（クールダウン中につき買い候補から除外: " + "、".join(f"{r['ticker']}({r['name']})" for r in cooldown_rows) + "）")

    emit()
    emit(f"実行後: 現金 {new_cash:,.0f}円 + 保有評価額 {total_after - new_cash:,.0f}円 "
         f"= 合計 {total_after:,.0f}円")
    emit()
    emit("※完全な仮想シミュレーションです。実際の資金は一切動いていません。")

    PORTFOLIO_AUTO_PATH.write_text(
        json.dumps(
            {
                "cash": round(new_cash, 2),
                "holdings": holdings,
                "costBasis": cost_basis,
                "cooldown": cooldown,
                "lastActionCheck": next_action_check,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    rb.record_history(now, new_cash, total_after - new_cash, total_after, path=HISTORY_AUTO_PATH)

    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"rebalance_auto_{now:%Y-%m-%d}.txt"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[保存しました] {log_file}")
    print(f"[更新しました] {PORTFOLIO_AUTO_PATH}")

    if stop_loss_rows:
        alert_lines = [
            f"🚨【株モニタリング】損切り自動実行 [{now:%Y-%m-%d %H:%M}]",
            "自動売買(仮想)組で以下を損切り・全売却しました:",
            "",
        ]
        for r in stop_loss_rows:
            alert_lines.append(
                f"[{r['ticker']}] {r['name']}: {r['loss_pct']*100:+.1f}%"
                f"（取得 {r['cost_basis']:,.0f}円→売却 {r['price']:,.1f}円）"
            )
        alert_lines.append("\n※完全な仮想シミュレーションです。実際の資金は動いていません。")
        send_line_alert("\n".join(alert_lines))


if __name__ == "__main__":
    main()
