"""
実発注の安全装置(純粋なチェックロジック)

- check_execution_allowed : 発注してよい状況か(kill switch・halt・営業日/取引時間・実弾の二重ガード)
- build_orders            : compute_plan の提案を、上限・価格乖離・資金・保有超過などで検証し、
                            通ったものだけを OrderRequest にする(落としたものは理由付きで返す)

ここは「何もしない方向に倒す」のが原則。少しでも怪しければ発注しない。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import market_calendar
from broker import OrderRequest, Quote
from order_ledger import OrderLedger
from trading_config import TradingConfig


@dataclass
class GuardResult:
    orders: list[OrderRequest] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (ticker, 理由)


def tick_size(price: float) -> float:
    """東証の標準呼値(TOPIX100の細かい呼値の倍数になっているので、どちらの銘柄にも有効)。"""
    table = [(3_000, 1), (5_000, 5), (30_000, 10), (50_000, 50), (300_000, 100), (500_000, 500),
             (3_000_000, 1_000), (5_000_000, 5_000)]
    for limit, tick in table:
        if price <= limit:
            return tick
    return 10_000


def round_to_tick(price: float, side: str) -> float:
    """買いは切り上げ・売りは切り下げ(約定しやすい側へ丸める)。"""
    tick = tick_size(price)
    steps = price / tick
    n = math.ceil(steps - 1e-9) if side == "buy" else math.floor(steps + 1e-9)
    return round(n * tick, 4)


def check_execution_allowed(
    now: dt.datetime,
    cfg: TradingConfig,
    *,
    kill_switch: bool,
    halt_reason: str | None,
    will_place_orders: bool,
    environ: dict | None = None,
) -> str | None:
    """発注を止めるべき理由があればその文字列、問題なければ None。

    will_place_orders=False(通知・承認待ちの提案作成のみ)のときは取引時間を問わない
    (営業日でない日は提案自体が無意味なので止める)。
    """
    if kill_switch:
        return "kill switch(STOP_TRADING)が有効です"
    if halt_reason:
        return f"停止中(halt): {halt_reason}。原因を確認して `live_trade.py resume` するまで発注しません"
    if not market_calendar.is_trading_day(now.date()):
        return f"{now.date()} は東証の休場日です"
    if not will_place_orders:
        return None
    if cfg.broker == "kabu":
        if not market_calendar.holidays_known():
            return "jpholiday が無く祝日を判定できないため発注しません(pip install jpholiday)"
        if not cfg.live_enabled(environ):
            return "実発注が無効です(設定 live=true かつ 環境変数 LIVE_TRADING=1 の両方が必要)"
    if not market_calendar.is_market_open(now):
        return "東証の取引時間外です(9:00-11:30 / 12:30-15:30)"
    return None


def build_orders(
    rows: list[dict],
    *,
    quotes: dict[str, Quote],
    cash: float,
    holdings: dict[str, int],
    total_value: float,
    cfg: TradingConfig,
    ledger: OrderLedger,
    today: str,
) -> GuardResult:
    """compute_plan の rows(action_shares != 0 の行)を検証して発注リストを作る。"""
    result = GuardResult()
    spent_notional = ledger.notional_on(today)
    order_count = ledger.count_on(today)
    budget = cash

    proposals = [r for r in rows if r["action_shares"] != 0]
    # 売り(損切り優先)→買い(スコア高い順)。売却代金は約定・受渡前なので買い資金には数えない
    proposals.sort(key=lambda r: (r["action_shares"] > 0, not r["stop_loss"], -r["score"]))

    for r in proposals:
        ticker = r["ticker"]
        side = "buy" if r["action_shares"] > 0 else "sell"
        qty = abs(int(r["action_shares"]))

        def reject(reason: str) -> None:
            result.rejected.append((ticker, reason))

        if ledger.has(ledger.make_key(today, ticker, side)):
            reject("本日すでに同じ銘柄・同じ方向で発注済み(二重発注防止)")
            continue
        q = quotes.get(ticker)
        if q is None or not q.price or q.price <= 0:
            reject("証券会社の現在値を取得できない")
            continue
        deviation = abs(q.price - r["price"]) / r["price"]
        if deviation > cfg.price_deviation_limit:
            reject(f"計算に使った価格と現在値が{deviation*100:.1f}%乖離(上限{cfg.price_deviation_limit*100:.1f}%)")
            continue
        if side == "sell" and qty > holdings.get(ticker, 0):
            reject(f"保有{holdings.get(ticker, 0)}株を超える売却({qty}株)は出さない")
            continue

        base = q.ask if side == "buy" and q.ask else q.bid if side == "sell" and q.bid else q.price
        margin = cfg.limit_price_margin
        limit = round_to_tick(base * (1 + margin if side == "buy" else 1 - margin), side)
        notional = qty * limit

        if notional > cfg.max_order_notional:
            reject(f"1注文の上限{cfg.max_order_notional:,.0f}円を超える({notional:,.0f}円)")
            continue
        if spent_notional + notional > cfg.max_daily_notional:
            reject(f"1日の発注上限{cfg.max_daily_notional:,.0f}円を超える")
            continue
        if order_count + 1 > cfg.max_orders_per_day:
            reject(f"1日の発注回数上限{cfg.max_orders_per_day}件を超える")
            continue
        if side == "buy":
            need = notional * (1 + cfg.cash_reserve_rate)
            if need > budget:
                reject(f"買付余力不足(必要{need:,.0f}円 > 余力{budget:,.0f}円。売却代金は受渡まで使わない)")
                continue
            after_weight = (holdings.get(ticker, 0) * q.price + qty * q.price) / total_value if total_value else 1
            if after_weight > cfg.hard_max_weight:
                reject(f"発注後の比率{after_weight*100:.1f}%が絶対上限{cfg.hard_max_weight*100:.0f}%を超える")
                continue
            budget -= need

        spent_notional += notional
        order_count += 1
        result.orders.append(OrderRequest(ticker, side, qty, limit, stop_loss=bool(r["stop_loss"])))

    return result
