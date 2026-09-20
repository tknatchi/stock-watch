"""
実発注(live_trade.py)の設定。trading_config.json から読み込む。

- trading_config.json は .gitignore 済み。雛形は trading_config.example.json
- パスワード・APIキーなどの秘密情報はここ(JSON)には書かない。環境変数のみで渡す
- 実発注は「設定の live=true」かつ「環境変数 LIVE_TRADING=1」の両方が揃ったときだけ有効
  (どちらか片方の設定ミス・持ち越しだけでは実弾が飛ばない二重ガード)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "trading_config.json"

VALID_MODES = ("notify", "approve", "auto")
VALID_BROKERS = ("paper", "kabu")


@dataclass
class TradingConfig:
    # notify : LINEに提案を送るだけ(発注しない)
    # approve: 提案を保存し、`live_trade.py approve <ID>` で承認したときだけ発注
    # auto   : 提案をそのまま自動発注
    mode: str = "notify"
    broker: str = "paper"          # paper=仮想約定 / kabu=auカブコム証券 kabuステーションAPI
    live: bool = False             # trueでも環境変数 LIVE_TRADING=1 が無ければ実発注しない

    capital_limit: float = 300_000            # 戦略が使う資金の上限(円)。口座に余分な資金があっても、これ以上は使わない
    paper_cash: float = 300_000               # broker=paper のときの仮想口座の初期資金(円)
    max_orders_per_day: int = 10
    max_order_notional: float = 100_000       # 1注文あたりの上限金額(円)
    max_daily_notional: float = 300_000       # 1日の合計発注上限(円)
    hard_max_weight: float = 0.25             # 発注後の1銘柄比率の絶対上限(rebalance.MAX_WEIGHTとは別の最後の砦)
    price_deviation_limit: float = 0.03       # 計算に使った価格と証券会社の現在値が3%超ずれたら見送る
    cash_reserve_rate: float = 0.002          # 買付代金に上乗せして確保する手数料・値動きの余裕(0.2%)
    limit_price_margin: float = 0.003         # 指値: 買いは現在値+0.3%・売りは現在値-0.3%(成行は使わない)

    circuit_breaker_stop_losses: int = 3      # 直近window日に損切りがこの回数に達したら自動停止
    circuit_breaker_window_days: int = 10
    cash_tolerance_yen: float = 3_000         # 残高照合で許容する現金のズレ(配当入金・端数など)
    approval_ttl_minutes: int = 180           # 承認待ちプランの有効期限
    fill_poll_seconds: int = 60               # 発注後、約定を待つ最大秒数
    fill_poll_interval: float = 2.0

    account_type: int = 4                     # kabu API: 2=一般 4=特定(NISAは非対応。頻繁売買には不向きでもある)
    kabu_base_url: str = "http://localhost:18081/kabusapi"  # 18081=検証環境 / 18080=本番環境(意図して変えること)
    kabu_exchange: int = 1                    # 1=東証

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode は {VALID_MODES} のいずれか: {self.mode!r}")
        if self.broker not in VALID_BROKERS:
            raise ValueError(f"broker は {VALID_BROKERS} のいずれか: {self.broker!r}")
        for name in ("capital_limit", "max_orders_per_day", "max_order_notional", "max_daily_notional"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} は正の値にしてください")
        if not 0 < self.hard_max_weight <= 1:
            raise ValueError("hard_max_weight は0より大きく1以下")

    @property
    def is_production_endpoint(self) -> bool:
        return self.kabu_base_url.rstrip("/").endswith(":18080/kabusapi")

    def live_enabled(self, environ: dict | None = None) -> bool:
        env = os.environ if environ is None else environ
        return bool(self.live) and env.get("LIVE_TRADING") == "1"


def load_config(path: Path = CONFIG_PATH) -> TradingConfig:
    """設定ファイルを読む。無ければ安全側の既定値(notify・paper・live無効)を返す。"""
    if not path.exists():
        cfg = TradingConfig()
        cfg.validate()
        return cfg
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(TradingConfig)}
    kwargs = {k: v for k, v in raw.items() if k in known}
    unknown = [k for k in raw if k not in known and not k.startswith("_")]
    if unknown:
        raise ValueError(f"trading_config.json に未知のキー(綴り違いの可能性): {unknown}")
    cfg = TradingConfig(**kwargs)
    cfg.validate()
    return cfg
