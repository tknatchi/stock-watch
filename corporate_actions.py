"""
配当・株式分割/併合の反映(仮想シム rebalance_auto.py 用)

なぜ必要か:
    yfinanceの株価は分割後の値で返るが、portfolio_auto.json の保有株数・取得単価は
    自分で持っている数字なので、分割が起きても勝手には更新されない。放置すると
    「株数そのまま・株価が半分」= 見かけ上-50%となり、損切りルール(-15%)が誤発動する。
    配当も現金として入らないので、実際より成績が悪く見える。

簡略化(仮定):
    - 配当は権利落ち日に、その時点の保有株数ぶん現金に入るとみなす(実際の入金は数か月後)
    - 配当は特定口座の源泉徴収(20.315%)を引いた手取りで計上する(DIVIDEND_TAX_RATE)
    - 分割・併合で端数が出た分は切り捨て(端数株の現金精算は無視)
"""

from __future__ import annotations

import datetime as dt
import math

import yfinance as yf

DIVIDEND_TAX_RATE = 0.20315


def fetch_actions(ticker: str) -> list[dict]:
    """[{"date": "YYYY-MM-DD", "dividend": float, "split": float}, ...](該当なしの項目は0)。"""
    df = yf.Ticker(ticker).actions
    if df is None or df.empty:
        return []
    return [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "dividend": float(row.get("Dividends", 0) or 0),
            "split": float(row.get("Stock Splits", 0) or 0),
        }
        for idx, row in df.iterrows()
    ]


def apply_actions(
    holdings: dict[str, int],
    cost_basis: dict[str, float],
    cash: float,
    actions_by_ticker: dict[str, list[dict]],
    since: dt.date,
    until: dt.date,
    dividend_tax_rate: float = DIVIDEND_TAX_RATE,
) -> tuple[dict[str, int], dict[str, float], float, list[str]]:
    """since より後・until 以前の権利落ち/分割を、保有・取得単価・現金に反映して返す。

    入力は書き換えず新しい辞書を返す。戻り値: (holdings, cost_basis, cash, 実施内容のログ行)
    """
    holdings = dict(holdings)
    cost_basis = dict(cost_basis)
    events: list[str] = []
    since_s, until_s = since.isoformat(), until.isoformat()

    for ticker in sorted(holdings):
        for a in sorted(actions_by_ticker.get(ticker, []), key=lambda a: a["date"]):
            if not (since_s < a["date"] <= until_s):
                continue
            shares = holdings.get(ticker, 0)
            if shares <= 0:
                continue
            if a["split"] and a["split"] > 0 and a["split"] != 1:
                ratio = a["split"]
                new_shares = math.floor(shares * ratio + 1e-9)
                holdings[ticker] = new_shares
                if ticker in cost_basis:
                    cost_basis[ticker] = round(cost_basis[ticker] / ratio, 4)
                events.append(f"[{ticker}] {a['date']} 株式分割/併合 x{ratio:g}: {shares}株 → {new_shares}株")
                if new_shares <= 0:
                    holdings.pop(ticker)
                    cost_basis.pop(ticker, None)
                    break
            if a["dividend"] and a["dividend"] > 0:
                gross = shares * a["dividend"]
                net = gross * (1 - dividend_tax_rate)
                cash += net
                events.append(f"[{ticker}] {a['date']} 配当 {a['dividend']:g}円 x {shares}株 → 手取り{net:,.0f}円(税引前{gross:,.0f}円)")
    return holdings, cost_basis, cash, events
