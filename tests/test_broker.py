import json
import unittest

from broker import BrokerError, KabuStationBroker, OrderRequest, PaperBroker

from tests.helpers import FakeKabu


class PaperBrokerTests(unittest.TestCase):
    def test_unreachable_limit_leaves_account_untouched(self):
        b = PaperBroker(100_000, {}, {"A": 1000})
        res = b.place_order(OrderRequest("A", "buy", 10, limit_price=990))
        self.assertEqual(res.status, "CANCELLED")
        self.assertEqual((b.cash, b.positions), (100_000, {}))

    def test_partial_fill_updates_only_filled_quantity(self):
        b = PaperBroker(100_000, {}, {"A": 1000}, partial_ratio=0.5)
        res = b.place_order(OrderRequest("A", "buy", 10, limit_price=1010))
        self.assertEqual((res.status, res.filled_qty), ("PARTIAL", 5))
        self.assertEqual((b.cash, b.positions["A"]), (95_000, 5))

    def test_buy_without_enough_cash_is_rejected(self):
        b = PaperBroker(5_000, {}, {"A": 1000})
        self.assertEqual(b.place_order(OrderRequest("A", "buy", 10, 1010)).status, "REJECTED")
        self.assertEqual(b.cash, 5_000)

    def test_sell_is_capped_at_held_shares(self):
        b = PaperBroker(0, {"A": 3}, {"A": 1000})
        res = b.place_order(OrderRequest("A", "sell", 10, 990))
        self.assertEqual(res.filled_qty, 3)
        self.assertEqual(b.positions["A"], 0)


class KabuStationBrokerTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeKabu()
        self.audit: list = []
        self.broker = KabuStationBroker(self.srv.base_url, "api-pass", "test-order-pass", timeout=0.4,
                                        audit=lambda kind, payload: self.audit.append((kind, payload)))

    def tearDown(self):
        self.srv.stop()

    def test_missing_passwords_fail_fast(self):
        with self.assertRaises(BrokerError):
            KabuStationBroker(self.srv.base_url, "", "x")
        with self.assertRaises(BrokerError):
            KabuStationBroker(self.srv.base_url, "x", "")

    def test_token_is_fetched_once_and_sent_as_api_key(self):
        self.broker.get_cash()
        self.broker.get_cash()
        self.assertEqual(self.srv.count("POST", "/token"), 1)
        gets = [r for r in self.srv.requests if r[0] == "GET"]
        self.assertTrue(all(r[2]["key"] == "tok123" for r in gets))

    def test_cash_and_positions_are_parsed_with_ticker_suffix_and_zero_positions_dropped(self):
        self.assertEqual(self.broker.get_cash(), 123456.0)
        pos = self.broker.get_positions()
        self.assertEqual([(p.ticker, p.shares, p.avg_price) for p in pos], [("7203.T", 10, 2500.5)])

    def test_quote_is_parsed(self):
        q = self.broker.get_quote("7203.T")
        self.assertEqual((q.price, q.bid, q.ask), (2510.0, 2509.0, 2511.0))
        self.assertTrue(any(p.endswith("/board/7203@1") for _, p, _ in self.srv.requests))

    def test_buy_order_request_body(self):
        res = self.broker.place_order(OrderRequest("7203.T", "buy", 10, 2515.0))
        body = next(b for m, p, b in self.srv.requests if p.endswith("/sendorder"))
        self.assertEqual(res.status, "SUBMITTED")
        self.assertEqual(res.order_id, "20260917A01")
        self.assertEqual((body["Symbol"], body["Side"], body["Qty"], body["Price"]), ("7203", "2", 10, 2515.0))
        self.assertEqual((body["FrontOrderType"], body["CashMargin"], body["DelivType"], body["FundType"]), (20, 1, 2, "AA"))
        self.assertEqual((body["Exchange"], body["AccountType"], body["ExpireDay"]), (1, 4, 0))
        self.assertEqual(body["Password"], "test-order-pass")

    def test_sell_order_uses_sell_side_and_no_fund_type(self):
        self.broker.place_order(OrderRequest("7203.T", "sell", 5, 2500.0))
        body = next(b for m, p, b in self.srv.requests if p.endswith("/sendorder"))
        self.assertEqual((body["Side"], body["DelivType"], body["FundType"]), ("1", 0, "  "))

    def test_order_password_never_reaches_audit_callback(self):
        self.broker.place_order(OrderRequest("7203.T", "buy", 10, 2515.0))
        self.assertNotIn("test-order-pass", json.dumps(self.audit, ensure_ascii=False))
        self.assertNotIn("api-pass", json.dumps(self.audit, ensure_ascii=False))

    def test_timeout_on_sendorder_is_unknown_and_never_retried(self):
        self.srv.sendorder_delay = 1.5
        res = self.broker.place_order(OrderRequest("7203.T", "buy", 10, 2515.0))
        self.assertEqual(res.status, "UNKNOWN")
        self.assertEqual(self.srv.count("POST", "/sendorder"), 1)

    def test_server_error_on_sendorder_is_unknown(self):
        self.srv.sendorder_status, self.srv.sendorder_body = 500, {"Message": "internal"}
        self.assertEqual(self.broker.place_order(OrderRequest("7203.T", "buy", 10, 2515.0)).status, "UNKNOWN")
        self.assertEqual(self.srv.count("POST", "/sendorder"), 1)

    def test_business_error_on_sendorder_is_rejected(self):
        self.srv.sendorder_status, self.srv.sendorder_body = 400, {"Code": 4001005, "Message": "余力不足"}
        res = self.broker.place_order(OrderRequest("7203.T", "buy", 10, 2515.0))
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("余力不足", res.message)

    def test_get_is_retried_on_transient_failure(self):
        self.srv.get_failures_left = 2
        self.assertEqual(self.broker.get_cash(), 123456.0)

    def test_get_gives_up_after_retries(self):
        self.srv.get_failures_left = 99
        with self.assertRaises(BrokerError):
            self.broker.get_cash()

    def test_order_status_mapping(self):
        def status(**o):
            self.srv.orders_body = [{"OrderQty": 10, **o}]
            return self.broker.get_order("X")
        filled = status(State=5, CumQty=10, Details=[{"RecType": 8, "Price": 2510.0, "Qty": 10}])
        self.assertEqual((filled.status, filled.filled_qty, filled.avg_price), ("FILLED", 10, 2510.0))
        self.assertEqual(status(State=5, CumQty=4).status, "PARTIAL")
        self.assertEqual(status(State=5, CumQty=0).status, "CANCELLED")
        self.assertEqual(status(State=3, CumQty=0).status, "SUBMITTED")

    def test_unknown_order_id_raises(self):
        self.srv.orders_body = []
        with self.assertRaises(BrokerError):
            self.broker.get_order("nope")


if __name__ == "__main__":
    unittest.main()
