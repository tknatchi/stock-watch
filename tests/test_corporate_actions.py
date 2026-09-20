import datetime as dt
import unittest

from corporate_actions import DIVIDEND_TAX_RATE, apply_actions

SINCE, UNTIL = dt.date(2026, 9, 10), dt.date(2026, 9, 17)


def act(date, dividend=0.0, split=0.0):
    return {"date": date, "dividend": dividend, "split": split}


class CorporateActionsTests(unittest.TestCase):
    def test_split_multiplies_shares_and_divides_cost_basis(self):
        h, b, cash, _ = apply_actions({"A": 10}, {"A": 2000.0}, 0.0, {"A": [act("2026-09-15", split=2.0)]}, SINCE, UNTIL)
        self.assertEqual(h["A"], 20)
        self.assertAlmostEqual(b["A"], 1000.0)

    def test_dividend_is_credited_net_of_withholding_tax(self):
        _, _, cash, _ = apply_actions({"A": 10}, {}, 100.0, {"A": [act("2026-09-15", dividend=50.0)]}, SINCE, UNTIL)
        self.assertAlmostEqual(cash, 100.0 + 10 * 50.0 * (1 - DIVIDEND_TAX_RATE))

    def test_actions_outside_window_are_ignored(self):
        actions = {"A": [act("2026-09-10", dividend=50.0),      # since当日=前回反映済み
                         act("2026-09-18", split=2.0),           # 未来
                         act("2026-09-01", dividend=50.0)]}      # 過去
        h, b, cash, events = apply_actions({"A": 10}, {"A": 100.0}, 0.0, actions, SINCE, UNTIL)
        self.assertEqual((h["A"], b["A"], cash, events), (10, 100.0, 0.0, []))

    def test_action_on_until_date_is_included(self):
        _, _, cash, _ = apply_actions({"A": 10}, {}, 0.0, {"A": [act("2026-09-17", dividend=10.0)]}, SINCE, UNTIL)
        self.assertGreater(cash, 0)

    def test_reverse_split_floors_fractional_shares(self):
        h, b, _, _ = apply_actions({"A": 25}, {"A": 100.0}, 0.0, {"A": [act("2026-09-15", split=0.1)]}, SINCE, UNTIL)
        self.assertEqual(h["A"], 2)
        self.assertAlmostEqual(b["A"], 1000.0)

    def test_reverse_split_to_zero_removes_position(self):
        h, b, _, _ = apply_actions({"A": 5}, {"A": 100.0}, 0.0, {"A": [act("2026-09-15", split=0.1)]}, SINCE, UNTIL)
        self.assertNotIn("A", h)
        self.assertNotIn("A", b)

    def test_tickers_not_held_are_ignored(self):
        h, _, cash, events = apply_actions({"A": 10}, {}, 0.0, {"B": [act("2026-09-15", dividend=99.0)]}, SINCE, UNTIL)
        self.assertEqual((h, cash, events), ({"A": 10}, 0.0, []))

    def test_inputs_are_not_mutated(self):
        holdings, basis = {"A": 10}, {"A": 2000.0}
        apply_actions(holdings, basis, 0.0, {"A": [act("2026-09-15", split=2.0)]}, SINCE, UNTIL)
        self.assertEqual((holdings, basis), ({"A": 10}, {"A": 2000.0}))


if __name__ == "__main__":
    unittest.main()
