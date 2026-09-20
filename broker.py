"""
証券会社アダプタ(実発注用)

- PaperBroker      : 仮想約定。ドライラン・テスト用。実際の資金は動かない
- KabuStationBroker: auカブコム証券 kabuステーションAPI(REST)。

KabuStationBroker について(重要):
    エンドポイント・パラメータは公式仕様に基づいて書いてあるが、実機(口座・kabuステーション)
    での動作は未検証。テストは自前のフェイクHTTPサーバに対してのみ行っている。
    まず検証環境(ポート18081)で少額の動作確認をしてから本番(18080)に切り替えること。
    発注(POST /sendorder)は絶対に自動リトライしない。タイムアウト等で結果が不明なときは
    status="UNKNOWN" を返し、呼び出し側が停止(halt)して人間が証券会社の画面で確認する。
"""

from __future__ import annotations

import itertools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests

TERMINAL_STATUSES = {"FILLED", "PARTIAL", "REJECTED", "CANCELLED"}


@dataclass
class Quote:
    ticker: str
    price: float
    bid: float | None = None
    ask: float | None = None


@dataclass
class Position:
    ticker: str
    shares: int
    avg_price: float | None = None


@dataclass
class OrderRequest:
    ticker: str
    side: str            # "buy" | "sell"
    qty: int
    limit_price: float   # 成行は使わず必ず指値
    stop_loss: bool = False


@dataclass
class OrderResult:
    order_id: str | None
    status: str          # SUBMITTED / FILLED / PARTIAL / REJECTED / CANCELLED / UNKNOWN
    filled_qty: int = 0
    avg_price: float | None = None
    message: str = ""
    raw: dict = field(default_factory=dict)


class BrokerError(Exception):
    """取得系APIの失敗(発注結果の不明はOrderResult(status="UNKNOWN")で表す)。"""


class Broker(ABC):
    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def get_order(self, order_id: str) -> OrderResult: ...


class PaperBroker(Broker):
    """仮想約定ブローカー。指値が現在値と交差していれば即約定する。"""

    def __init__(self, cash: float, positions: dict[str, int], prices: dict[str, float],
                 commission_rate: float = 0.0, partial_ratio: float = 1.0,
                 reject_tickers: set[str] | None = None, unknown_tickers: set[str] | None = None):
        self.cash = float(cash)
        self.positions = dict(positions)
        self.prices = dict(prices)
        self.commission_rate = commission_rate
        self.partial_ratio = partial_ratio
        self.reject_tickers = reject_tickers or set()
        self.unknown_tickers = unknown_tickers or set()
        self.sent: list[OrderRequest] = []
        self._orders: dict[str, OrderResult] = {}
        self._ids = itertools.count(1)

    def get_cash(self) -> float:
        return self.cash

    def get_positions(self) -> list[Position]:
        return [Position(t, n) for t, n in self.positions.items() if n > 0]

    def get_quote(self, ticker: str) -> Quote:
        if ticker not in self.prices:
            raise BrokerError(f"{ticker}: 気配なし")
        p = self.prices[ticker]
        return Quote(ticker, p, bid=p, ask=p)

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.sent.append(req)
        if req.ticker in self.unknown_tickers:
            return OrderResult(None, "UNKNOWN", message="タイムアウト(仮想)")
        oid = f"P{next(self._ids)}"
        if req.ticker in self.reject_tickers:
            res = OrderResult(oid, "REJECTED", message="拒否(仮想)")
            self._orders[oid] = res
            return res
        price = self.prices.get(req.ticker)
        crosses = price is not None and (
            (req.side == "buy" and req.limit_price >= price) or (req.side == "sell" and req.limit_price <= price)
        )
        if not crosses:
            res = OrderResult(oid, "CANCELLED", message="指値が届かず未約定(仮想)")
            self._orders[oid] = res
            return res
        qty = req.qty if self.partial_ratio >= 1 else int(req.qty * self.partial_ratio)
        if req.side == "sell":
            qty = min(qty, self.positions.get(req.ticker, 0))
        amount = qty * price
        fee = amount * self.commission_rate
        if req.side == "buy":
            if amount + fee > self.cash:
                res = OrderResult(oid, "REJECTED", message="資金不足(仮想)")
                self._orders[oid] = res
                return res
            self.cash -= amount + fee
            self.positions[req.ticker] = self.positions.get(req.ticker, 0) + qty
        else:
            self.cash += amount - fee
            self.positions[req.ticker] = self.positions.get(req.ticker, 0) - qty
        status = "FILLED" if qty == req.qty else "PARTIAL"
        res = OrderResult(oid, status, filled_qty=qty, avg_price=price)
        self._orders[oid] = res
        return res

    def get_order(self, order_id: str) -> OrderResult:
        if order_id not in self._orders:
            raise BrokerError(f"注文なし: {order_id}")
        return self._orders[order_id]


class KabuStationBroker(Broker):
    """auカブコム証券 kabuステーションAPI。実機未検証(モジュール冒頭の注記を参照)。"""

    def __init__(self, base_url: str, api_password: str, order_password: str,
                 account_type: int = 4, exchange: int = 1, session: requests.Session | None = None,
                 timeout: float = 10.0, get_retries: int = 2, audit=None):
        if not api_password or not order_password:
            raise BrokerError("KABU_API_PASSWORD / KABU_ORDER_PASSWORD が未設定です")
        self.base_url = base_url.rstrip("/")
        self.api_password = api_password
        self.order_password = order_password
        self.account_type = account_type
        self.exchange = exchange
        self.session = session or requests.Session()
        self.timeout = timeout
        self.get_retries = get_retries
        self.audit = audit or (lambda kind, payload: None)
        self._token: str | None = None

    @staticmethod
    def _symbol(ticker: str) -> str:
        return ticker.split(".")[0]

    def _headers(self) -> dict:
        if self._token is None:
            r = self.session.post(f"{self.base_url}/token", json={"APIPassword": self.api_password}, timeout=self.timeout)
            if not r.ok or "Token" not in r.json():
                raise BrokerError(f"トークン取得失敗 {r.status_code}")
            self._token = r.json()["Token"]
        return {"X-API-KEY": self._token, "Content-Type": "application/json"}

    def _get(self, path: str, params: dict | None = None):
        last: Exception | None = None
        for attempt in range(self.get_retries + 1):
            try:
                r = self.session.get(f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=self.timeout)
                if r.status_code == 401:
                    self._token = None
                    raise BrokerError("認証エラー(401)")
                if not r.ok:
                    raise BrokerError(f"GET {path} → {r.status_code}: {r.text[:200]}")
                self.audit("GET", {"path": path, "params": params, "status": r.status_code})
                return r.json()
            except (requests.RequestException, BrokerError) as e:
                last = e
                time.sleep(min(2 ** attempt, 4) * 0.1)
        raise BrokerError(f"GET {path} 失敗: {last}")

    def get_cash(self) -> float:
        return float(self._get("/wallet/cash")["StockAccountWallet"])

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._get("/positions", {"product": 1}):
            qty = int(p.get("LeavesQty", 0))
            if qty > 0:
                out.append(Position(f"{p['Symbol']}.T", qty, float(p["Price"]) if p.get("Price") else None))
        return out

    def get_quote(self, ticker: str) -> Quote:
        b = self._get(f"/board/{self._symbol(ticker)}@{self.exchange}")
        price = b.get("CurrentPrice")
        if not price:
            raise BrokerError(f"{ticker}: 現在値なし")
        return Quote(ticker, float(price), b.get("BidPrice"), b.get("AskPrice"))

    def place_order(self, req: OrderRequest) -> OrderResult:
        buy = req.side == "buy"
        body = {
            "Password": self.order_password,
            "Symbol": self._symbol(req.ticker),
            "Exchange": self.exchange,
            "SecurityType": 1,
            "Side": "2" if buy else "1",
            "CashMargin": 1,
            "DelivType": 2 if buy else 0,
            "FundType": "AA" if buy else "  ",
            "AccountType": self.account_type,
            "Qty": req.qty,
            "FrontOrderType": 20,          # 指値
            "Price": float(req.limit_price),
            "ExpireDay": 0,                # 当日限り
        }
        self.audit("POST_SENDORDER", {k: v for k, v in body.items() if k != "Password"})
        try:
            r = self.session.post(f"{self.base_url}/sendorder", headers=self._headers(), json=body, timeout=self.timeout)
        except requests.RequestException as e:
            # 送信できたか不明。再送すると二重発注になりうるので絶対にリトライしない
            return OrderResult(None, "UNKNOWN", message=f"送信結果不明: {e}")
        try:
            data = r.json()
        except ValueError:
            return OrderResult(None, "UNKNOWN", message=f"応答を解釈できません {r.status_code}")
        self.audit("RESP_SENDORDER", {"status": r.status_code, "body": data})
        if r.ok and data.get("Result") == 0 and data.get("OrderId"):
            return OrderResult(str(data["OrderId"]), "SUBMITTED", raw=data)
        if r.status_code >= 500:
            return OrderResult(None, "UNKNOWN", message=f"サーバーエラー {r.status_code}", raw=data)
        return OrderResult(None, "REJECTED", message=str(data.get("Message", r.text[:200])), raw=data)

    def get_order(self, order_id: str) -> OrderResult:
        orders = self._get("/orders", {"id": order_id})
        if not orders:
            raise BrokerError(f"注文が見つかりません: {order_id}")
        o = orders[0]
        filled = int(o.get("CumQty", 0))
        qty = int(o.get("OrderQty", 0))
        state = int(o.get("State", 0))
        prices = [(float(d["Price"]), float(d["Qty"])) for d in o.get("Details", []) if d.get("RecType") == 8 and d.get("Qty")]
        avg = sum(p * q for p, q in prices) / sum(q for _, q in prices) if prices else None
        if state == 5:  # 終了
            status = "FILLED" if filled >= qty > 0 else ("PARTIAL" if filled > 0 else "CANCELLED")
        else:
            status = "SUBMITTED"
        return OrderResult(str(order_id), status, filled_qty=filled, avg_price=avg, raw=o)
