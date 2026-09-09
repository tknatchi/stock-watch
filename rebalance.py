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

買い付けの割り当てロジック(greedy_lot_allocation)について:
    銘柄ごとに独立して「目標評価額 ÷ 株価」を単元株数に丸めると、予算が小さい場合に
    ほぼ全銘柄が「1単元にも届かない」判定になり、実質何も買えなくなる
    (例: 30万円を15銘柄に分散すると1銘柄あたり2万円程度になるが、値がさ株は1単元30万円超のため)。
    これを避けるため、買いはスコアの高い銘柄から順に「使えるお金の範囲で1単元ずつ」割り当てる
    貪欲法(greedy)にしている。個別銘柄の目標額(上限)より小さい額しか使えない場合でも、
    最初の1単元だけは(予算が許す限り)買うことを許可し、2単元目以降は上限を守る。
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
HISTORY_PATH = BASE_DIR / "logs" / "portfolio_history.json"

# ==== ここを書き換えてカスタマイズする ====
MIN_SCORE = 45          # この買いスコア未満の銘柄は新規の投資対象から除外
MAX_WEIGHT = 0.15       # 1銘柄への集中を防ぐ上限比率（15%）
DRIFT_THRESHOLD = 0.02  # 目標比率からのズレがポートフォリオ全体比でこれを超えたら売買提案

# 単元株数。通常の取引(東証は原則100株単位)を使うなら100のまま。
# 単元未満株(ミニ株式・S株など、1株単位で売買できるサービス。SBI証券・楽天証券・
# マネックス証券などが対応)を使うなら1にする。
#
# 100のままだと、少額予算では「1単元の値段がそもそも目標配分額を大きく超える」
# 銘柄ばかりになり、買った直後に集中上限(MAX_WEIGHT)超過を理由に売り戻しを
# 提案する、といった矛盾が起きやすい。単元未満株を使うなら1にすることでこの
# 矛盾は解消する(目標比率どおりの細かい金額で売買できるため)。
LOT_SIZE = 1
# ===========================================


def greedy_lot_allocation(
    candidates: list[dict], available_cash: float, lot_size: int
) -> tuple[dict[str, int], float]:
    """買い候補にスコア優先で単元株を割り当てる。

    candidates の各要素は {"ticker", "price", "score", "current_value", "cap"}。
    cap(目標評価額)より1単元のコストの方が高い銘柄でも、最初の1単元だけは
    (予算が許せば)買うことを許可する。2単元目以降はcapを超えない範囲に制限する。
    これにより、少額予算で目標額が小さい銘柄ばかりになっても「何も買えない」状態を避ける。

    戻り値: (ticker → 追加で買う株数, 残余現金)
    """
    additional_shares = {c["ticker"]: 0 for c in candidates}
    cash = available_cash

    for c in sorted(candidates, key=lambda c: -c["score"]):
        lot_cost = c["price"] * lot_size
        if lot_cost <= 0 or lot_cost > cash:
            continue

        additional_shares[c["ticker"]] += lot_size  # 最初の1単元は無条件で許可
        cash -= lot_cost

        while True:
            held_value = c["current_value"] + (additional_shares[c["ticker"]] + lot_size) * c["price"]
            if held_value > c["cap"] or lot_cost > cash:
                break
            additional_shares[c["ticker"]] += lot_size
            cash -= lot_cost

    return additional_shares, cash


def record_history(
    now: dt.datetime, cash: float, holdings_value: float, total_value: float, path: Path = HISTORY_PATH
) -> None:
    """資産推移(現金・保有評価額・合計)を日次で path(既定は logs/portfolio_history.json)に追記する。

    ダッシュボード(export_portfolio_data.py)の推移グラフ用。
    同じ日に複数回実行された場合は、その日の分を上書きする(1日1点)。
    """
    path.parent.mkdir(exist_ok=True)
    history = []
    if path.exists():
        history = json.loads(path.read_text(encoding="utf-8"))

    today = now.strftime("%Y-%m-%d")
    history = [h for h in history if h["date"] != today]
    history.append(
        {
            "date": today,
            "cash": round(cash, 0),
            "holdingsValue": round(holdings_value, 0),
            "totalValue": round(total_value, 0),
        }
    )
    history.sort(key=lambda h: h["date"])
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def load_portfolio(path: Path = PORTFOLIO_PATH) -> dict:
    if not path.exists():
        raise SystemExit(
            f"{path.name} が見つかりません。\n"
            f"portfolio.example.json をコピーして {path.name} を作成し、"
            f"cash・holdings を書き換えてから再実行してください。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def compute_plan(cash: float, holdings: dict[str, int], snapshots: list) -> dict:
    """現金・保有株数・現在の銘柄スナップショットから、リバランスの計算結果を返す。

    rebalance.py(提案を表示するだけ)と rebalance_auto.py(仮想ポートフォリオに
    自動適用する)の両方から呼ばれる共通ロジック。

    戻り値: {
        "total_value", "holdings_value", "rows"(銘柄ごとの現在値・目標値・提案株数),
        "leftover_cash"(全ての提案を実行した場合に残る現金),
    }
    """
    target_weights = compute_target_weights(snapshots, min_score=MIN_SCORE, max_weight=MAX_WEIGHT)
    holdings_value = sum(holdings.get(s.ticker, 0) * s.price for s in snapshots)
    total_value = cash + holdings_value

    if total_value <= 0:
        return {"total_value": total_value, "holdings_value": holdings_value, "rows": [], "leftover_cash": cash}

    rows = {}
    buy_candidates = []
    freed_cash = 0.0

    for s in snapshots:
        current_shares = holdings.get(s.ticker, 0)
        current_value = current_shares * s.price
        target_weight = target_weights.get(s.ticker, 0.0)
        target_value = target_weight * total_value
        drift_value = target_value - current_value
        drift_ratio = drift_value / total_value

        if current_shares == 0 and target_weight == 0:
            continue  # 保有なし・新規対象外の銘柄は対象外

        row = {
            "ticker": s.ticker,
            "name": s.name,
            "score": s.buy_score,
            "price": s.price,
            "current_shares": current_shares,
            "current_value": current_value,
            "target_weight": target_weight,
            "target_value": target_value,
            "action_shares": 0,
        }
        rows[s.ticker] = row

        if abs(drift_ratio) < DRIFT_THRESHOLD:
            continue

        if drift_value < 0:
            # 売り: 「目標評価額を超えない最大の単元数」を直接計算して、そこまで減らす。
            # (差額 ÷ 株価 ÷ 単元 を切り捨てる方式だと、1単元の価値自体が目標額を
            #  大きく超える銘柄で「差額が1単元未満」に丸まり、大幅な超過保有でも
            #  売却提案が出ない不具合があったため)
            # 他銘柄と現金を取り合わないので、これまで通り単独で決めてよい。
            target_lots = math.floor(target_value / s.price / LOT_SIZE)
            action_shares = target_lots * LOT_SIZE - current_shares
            row["action_shares"] = action_shares
            freed_cash += -action_shares * s.price
        else:
            # 買い: 複数銘柄で現金を取り合うため、後でまとめてgreedy_lot_allocationに回す
            buy_candidates.append(
                {
                    "ticker": s.ticker,
                    "price": s.price,
                    "score": s.buy_score,
                    "current_value": current_value,
                    "cap": target_value,
                }
            )

    available_cash = cash + freed_cash
    additional_shares, leftover_cash = greedy_lot_allocation(buy_candidates, available_cash, LOT_SIZE)
    for ticker, shares in additional_shares.items():
        if shares > 0:
            rows[ticker]["action_shares"] = shares

    rows = sorted(rows.values(), key=lambda r: (-r["target_weight"], -r["current_value"]))
    return {"total_value": total_value, "holdings_value": holdings_value, "rows": rows, "leftover_cash": leftover_cash}


def fetch_universe_snapshots(holdings: dict[str, int]) -> list:
    """WATCHLIST(監視銘柄) + 実際に保有している銘柄(監視外でもよい)をまとめて取得する。"""
    universe = sorted(set(WATCHLIST) | set(holdings.keys()))
    return [s for s in (fetch_snapshot(t) for t in universe) if s is not None]


def main() -> None:
    portfolio = load_portfolio()
    cash = float(portfolio.get("cash", 0))
    holdings: dict[str, int] = portfolio.get("holdings", {})

    snapshots = fetch_universe_snapshots(holdings)
    if not snapshots:
        print("銘柄データを取得できませんでした。")
        return

    plan = compute_plan(cash, holdings, snapshots)
    holdings_value = plan["holdings_value"]
    total_value = plan["total_value"]
    leftover_cash = plan["leftover_cash"]

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

    record_history(now, cash, holdings_value, total_value)

    rows = plan["rows"]
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
        emit()

    if not rows:
        emit(f"買いスコア{MIN_SCORE}以上の銘柄がありません。MIN_SCOREを見直すか、日を改めて実行してください。")

    emit(f"提案どおり売買した場合の現金残高目安: {leftover_cash:,.0f}円")
    emit()
    emit("※これは売買の提案であり、自動発注は一切行いません。実際の売買は自分の判断で証券会社にて行ってください。")
    emit("※日々のスコア変動には反応せず、月1回など間隔を空けて実行することを推奨します。")

    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"rebalance_{now:%Y-%m-%d}.txt"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[保存しました] {log_file}")


if __name__ == "__main__":
    main()
