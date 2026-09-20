"""テスト共通のヘルパ(フェイク銘柄・環境・フェイクkabuステーションAPIサーバ)。"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import live_trade
import order_ledger as ol

NOW_OPEN = dt.datetime(2026, 9, 17, 10, 0)     # 木曜・場中
NOW_CLOSED = dt.datetime(2026, 9, 17, 16, 0)   # 木曜・大引け後


def snap(ticker: str, price: float, score: int = 80, name: str | None = None):
    return SimpleNamespace(ticker=ticker, name=name or ticker, price=price, buy_score=score, sector="x", change_pct=0.0)


def noop_sleep(_seconds: float) -> None:
    return None


def make_env(tmp: Path) -> live_trade.Env:
    logs = tmp / "logs"
    return live_trade.Env(
        state_path=tmp / "portfolio_live.json",
        pending_path=logs / "pending_plan.json",
        paper_path=logs / "paper_broker.json",
        ledger=ol.OrderLedger(logs / "live_orders.json"),
        halt_path=logs / "live_halt.json",
        kill_path=tmp / "STOP_TRADING",
        audit_path=logs / "live_audit.jsonl",
    )


def write_state(env: live_trade.Env, holdings=None, cost_basis=None, last_cash=None, cooldown=None) -> None:
    live_trade.save_state(env, {
        "holdings": holdings or {}, "costBasis": cost_basis or {}, "cooldown": cooldown or {},
        "lastBrokerCash": last_cash,
    })


class FakeKabu:
    """kabuステーションAPIのフェイクHTTPサーバ。受けたリクエストを requests に記録する。"""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.sendorder_delay = 0.0
        self.sendorder_status = 200
        self.sendorder_body: dict = {"Result": 0, "OrderId": "20260917A01"}
        self.get_failures_left = 0
        self.orders_body: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append(("POST", self.path, body))
                if self.path.endswith("/token"):
                    self._send(200, {"ResultCode": 0, "Token": "tok123"})
                elif self.path.endswith("/sendorder"):
                    time.sleep(outer.sendorder_delay)
                    self._send(outer.sendorder_status, outer.sendorder_body)
                else:
                    self._send(404, {})

            def do_GET(self):
                u = urlparse(self.path)
                outer.requests.append(("GET", u.path, {"query": u.query, "key": self.headers.get("X-API-KEY")}))
                if outer.get_failures_left > 0:
                    outer.get_failures_left -= 1
                    self._send(503, {"Message": "busy"})
                elif u.path.endswith("/wallet/cash"):
                    self._send(200, {"StockAccountWallet": 123456.0})
                elif u.path.endswith("/positions"):
                    self._send(200, [{"Symbol": "7203", "LeavesQty": 10, "Price": 2500.5},
                                     {"Symbol": "9432", "LeavesQty": 0, "Price": 150.0}])
                elif "/board/" in u.path:
                    self._send(200, {"CurrentPrice": 2510.0, "BidPrice": 2509.0, "AskPrice": 2511.0})
                elif u.path.endswith("/orders"):
                    self._send(200, outer.orders_body)
                else:
                    self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/kabusapi"

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p, _ in self.requests if m == method and p.endswith(suffix))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
