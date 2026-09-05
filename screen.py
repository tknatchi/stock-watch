"""
スクリーニングツール（条件に合う銘柄だけを抽出する）

使い方:
    python screen.py

やっていること:
    watchlist.py の WATCHLIST 銘柄について株価・PER・配当利回りなどを取得し、
    下の CRITERIA（条件）に合う銘柄だけを一覧表示する。

条件を変えたいときは、下の CRITERIA の数値を書き換えるだけでOK。
"""

from __future__ import annotations

import console_utf8

console_utf8.setup()

from watchlist import WATCHLIST, StockSnapshot, fetch_snapshot


# ==== ここを書き換えて条件をカスタマイズする ====
CRITERIA = {
    "max_pe": 20.0,          # PERがこの値以下(割安寄り)。使わないなら None
    "min_dividend_yield": 2.0,  # 配当利回り%がこの値以上。使わないなら None
    "trend": "上昇トレンド(短期>長期)",  # このトレンドの銘柄だけ見たい場合。使わないなら None
}
# ===========================================


def passes(s: StockSnapshot) -> tuple[bool, list[str]]:
    """条件を満たすか判定し、満たした理由のリストも返す"""
    reasons = []
    ok = True

    if CRITERIA.get("max_pe") is not None:
        if s.pe_ratio is not None and s.pe_ratio <= CRITERIA["max_pe"]:
            reasons.append(f"PER {s.pe_ratio:.1f} <= {CRITERIA['max_pe']}")
        else:
            ok = False

    if CRITERIA.get("min_dividend_yield") is not None:
        if s.dividend_yield is not None and s.dividend_yield >= CRITERIA["min_dividend_yield"]:
            reasons.append(f"配当利回り {s.dividend_yield:.2f}% >= {CRITERIA['min_dividend_yield']}%")
        else:
            ok = False

    if CRITERIA.get("trend") is not None:
        if s.trend == CRITERIA["trend"]:
            reasons.append(f"トレンド一致: {s.trend}")
        else:
            ok = False

    return ok, reasons


def main() -> None:
    print(f"=== スクリーニング対象: {len(WATCHLIST)}銘柄 ===")
    print("条件:", CRITERIA, "\n")

    matched = []
    for ticker in WATCHLIST:
        s = fetch_snapshot(ticker)
        if s is None:
            continue
        ok, reasons = passes(s)
        if ok:
            matched.append((s, reasons))

    if not matched:
        print("条件に合う銘柄はありませんでした。CRITERIA を緩めてみてください。")
        return

    print(f"--- 条件に合致した銘柄: {len(matched)}件 ---\n")
    for s, reasons in matched:
        print(f"[{s.ticker}] {s.name}")
        print(f"  株価: {s.price:.1f}  前日比: {s.change_pct:+.2f}%  トレンド: {s.trend}")
        for r in reasons:
            print(f"  ✓ {r}")
        print()


if __name__ == "__main__":
    main()
