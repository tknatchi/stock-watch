"""
「買いスコア」の算出（ファンダメンタルズ + テクニカルの合成ヒューリスティック）

やっていること:
    トレンド(MA25/75)・RSI・MACD・ボリンジャーバンド・PER・配当利回りの6項目を
    それぞれ0〜1に正規化し、重み付け平均して0〜100のスコアにする。

注意:
    これは複数指標を機械的に合成しただけのヒューリスティックであり、
    将来の値動きを予測・保証するものではない。売買の助言でもない。
    重みは screen.py の CRITERIA と同様、下の WEIGHTS を書き換えて調整できる。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from watchlist import StockSnapshot

# 各項目の重み（合計が1.0でなくても内部で正規化される）
WEIGHTS = {
    "trend": 0.15,       # 中長期トレンド(MA25>MA75)
    "rsi": 0.20,         # RSI(14) の売られすぎ/買われすぎ
    "macd": 0.20,        # MACDヒストグラムの向き
    "bollinger": 0.15,   # ボリンジャーバンド内の位置
    "per": 0.15,         # PER(割安さ)
    "dividend": 0.15,    # 配当利回り
}


def compute_buy_score(s: "StockSnapshot") -> tuple[int, list[str]]:
    """0〜100の買いスコアと、その根拠(reasons)のリストを返す。"""
    reasons: list[str] = []
    scores: dict[str, float] = {}

    # 1. 中長期トレンド
    if s.trend.startswith("上昇"):
        scores["trend"] = 1.0
        reasons.append("中長期トレンド上昇（MA25>MA75）")
    elif s.trend.startswith("下降"):
        scores["trend"] = 0.0
    else:
        scores["trend"] = 0.5

    # 2. RSI(14): 売られすぎ(<=30)ほど加点、買われすぎ(>=70)ほど減点
    if s.rsi14 is not None:
        if s.rsi14 <= 30:
            scores["rsi"] = 1.0
            reasons.append(f"RSI {s.rsi14:.0f}（売られすぎ圏）")
        elif s.rsi14 >= 70:
            scores["rsi"] = 0.0
            reasons.append(f"RSI {s.rsi14:.0f}（買われすぎ圏に注意）")
        else:
            scores["rsi"] = max(0.0, (70 - s.rsi14) / 40)
    else:
        scores["rsi"] = 0.5

    # 3. MACDヒストグラム: プラス(シグナル線上抜け方向)なら加点
    if s.macd_hist is not None:
        scores["macd"] = 1.0 if s.macd_hist > 0 else 0.0
        if s.macd_hist > 0:
            reasons.append("MACDが上向き（シグナル線を上抜け中）")
    else:
        scores["macd"] = 0.5

    # 4. ボリンジャーバンド内の位置: 下限に近いほど加点(逆張り的な意味合い)
    if s.bb_pct is not None:
        pct = min(max(s.bb_pct, 0.0), 1.0)
        scores["bollinger"] = 1.0 - pct
        if s.bb_pct <= 0.2:
            reasons.append("ボリンジャーバンド下限付近")
        elif s.bb_pct >= 0.8:
            reasons.append("ボリンジャーバンド上限付近に注意")
    else:
        scores["bollinger"] = 0.5

    # 5. PER: 低いほど加点（30倍でゼロ、0倍で満点にキャップ）
    if s.pe_ratio is not None:
        scores["per"] = min(max((30 - s.pe_ratio) / 30, 0.0), 1.0)
        if s.pe_ratio <= 15:
            reasons.append(f"PER {s.pe_ratio:.1f}倍（割安水準）")
    else:
        scores["per"] = 0.5

    # 6. 配当利回り: 高いほど加点（5%で満点にキャップ）
    if s.dividend_yield is not None:
        scores["dividend"] = min(max(s.dividend_yield / 5.0, 0.0), 1.0)
        if s.dividend_yield >= 3.0:
            reasons.append(f"配当利回り {s.dividend_yield:.2f}%")
    else:
        scores["dividend"] = 0.0

    total_weight = sum(WEIGHTS.values())
    weighted = sum(WEIGHTS[k] * v for k, v in scores.items())
    score = round(100 * weighted / total_weight)

    if not reasons:
        reasons.append("突出した根拠はなく、各指標とも中庸")

    return score, reasons


def compute_target_weights(
    snapshots: list["StockSnapshot"], min_score: int = 45, max_weight: float = 0.15
) -> dict[str, float]:
    """買いスコアをもとにした目標配分比率(銘柄コード→0〜1、合計1.0)を計算する。

    やっていること:
        - min_score未満の銘柄は対象外（比率0）とし、新規の投資対象から除外する
        - 残った銘柄はスコアの大きさに比例して配分する
        - 1銘柄への集中を避けるため、比率がmax_weightを超える銘柄は上限でキャップし、
          超過分を他の銘柄へ再配分する（数回の反復で収束させる簡易ウォーターフィリング）

    月次リバランスなど、rebalance.py から呼ばれる想定。
    """
    eligible = {s.ticker: s.buy_score for s in snapshots if s.buy_score >= min_score}
    if not eligible:
        return {}

    total = sum(eligible.values())
    weights = {t: sc / total for t, sc in eligible.items()}

    for _ in range(10):
        over = {t: w for t, w in weights.items() if w > max_weight + 1e-9}
        if not over:
            break
        excess = sum(w - max_weight for w in over.values())
        for t in over:
            weights[t] = max_weight
        under = {t: w for t, w in weights.items() if t not in over}
        under_total = sum(under.values())
        if under_total <= 0:
            break
        for t in under:
            weights[t] += excess * (under[t] / under_total)

    return weights
