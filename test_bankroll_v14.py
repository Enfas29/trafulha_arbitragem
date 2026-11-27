"""Virtual budget invariants; no database, network or real money."""
from dataclasses import FrozenInstanceError, asdict
import json
import os
import unittest
from unittest.mock import patch

import bankroll_v14 as bankroll


class PolicyTests(unittest.TestCase):
    def test_defaults_are_proportional_and_configuration_is_loaded_once(self):
        expected = bankroll.BankrollPolicy(3_000, 2_000, 7_000)
        self.assertEqual(bankroll.read_policy({}), expected)
        with patch.object(bankroll, "POLICY", expected), patch.dict(
                os.environ, {"PAPER_BANKROLL_RESERVE_PCT": "malformed"}):
            budget = bankroll.budget_for_entry(
                {"poly": 100_000, "sx": 100_000}, {"poly": 0, "sx": 0})
        self.assertEqual(budget.policy, expected)
        self.assertEqual(budget.total_cap_cents, 40_000)

    def test_decimal_percentages_are_exact_basis_points(self):
        policy = bankroll.read_policy({
            "PAPER_BANKROLL_RESERVE_PCT": " 30,25 ",
            "PAPER_BANKROLL_MAX_OPERATION_PCT": "19.75",
            "PAPER_BANKROLL_MAX_EXPOSURE_PCT": "69.75",
        })
        self.assertEqual(policy, bankroll.BankrollPolicy(3025, 1975, 6975))

    def test_each_percentage_setting_rejects_explicit_bad_values(self):
        names = ("PAPER_BANKROLL_RESERVE_PCT", "PAPER_BANKROLL_MAX_OPERATION_PCT",
                 "PAPER_BANKROLL_MAX_EXPOSURE_PCT")
        for name in names:
            for value in ("", " ", "NaN", "Infinity", "-Infinity", "1e1",
                          "20.001", "20,001", ".2", "20%", "twenty",
                          True, 30, 30.0, None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        bankroll.read_policy({name: value})

    def test_policy_bounds_and_cross_constraint_are_enforced(self):
        for kwargs in (
                {"reserve_bps": 999}, {"reserve_bps": 9001},
                {"max_operation_bps": 99}, {"max_operation_bps": 5001},
                {"max_exposure_bps": 999}, {"max_exposure_bps": 9001},
                {"reserve_bps": 3001, "max_exposure_bps": 7000},
                {"reserve_bps": True}, {"max_operation_bps": 2000.0},
                {"max_exposure_bps": float("nan")},
                {"max_exposure_bps": float("inf")},
                {"reserve_bps": "3000"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    bankroll.BankrollPolicy(**kwargs)
        self.assertEqual(bankroll.BankrollPolicy(1000, 100, 9000).reserve_bps, 1000)
        self.assertEqual(bankroll.BankrollPolicy(9000, 5000, 1000).max_exposure_bps, 1000)

    def test_unknown_policy_and_nonmapping_environment_are_rejected(self):
        for value in ([], 1, "30", object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    bankroll.read_policy(value)
                with self.assertRaises(ValueError):
                    bankroll.budget_for_entry(
                        {"poly": 0, "sx": 0}, {"poly": 0, "sx": 0}, policy=value)

    def test_policy_is_immutable(self):
        policy = bankroll.BankrollPolicy()
        with self.assertRaises(FrozenInstanceError):
            policy.reserve_bps = 0


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.policy = bankroll.BankrollPolicy()

    def budget(self, free, reserved=None, policy=None):
        return bankroll.budget_for_entry(
            free, {"poly": 0, "sx": 0} if reserved is None else reserved,
            policy=self.policy if policy is None else policy)

    def test_initial_thousand_each_leaves_reserves_and_allows_more_than_old_cap(self):
        result = self.budget({"poly": 100_000, "sx": 100_000})
        self.assertEqual(asdict(result), {
            "total_cap_cents": 40_000,
            "poly_available_cents": 70_000, "sx_available_cents": 70_000,
            "poly_reserve_cents": 30_000, "sx_reserve_cents": 30_000,
            "poly_equity_cents": 100_000, "sx_equity_cents": 100_000,
            "total_equity_cents": 200_000, "open_exposure_cents": 0,
            "operation_cap_cents": 40_000, "exposure_cap_cents": 140_000,
            "exposure_remaining_cents": 140_000,
            "policy": {"reserve_bps": 3000, "max_operation_bps": 2000,
                       "max_exposure_bps": 7000},
        })

    def test_concurrent_operations_use_reserved_cost_and_stop_at_reserve(self):
        free = {"poly": 100_000, "sx": 100_000}
        reserved = {"poly": 0, "sx": 0}
        for expected in (40_000, 40_000, 40_000, 20_000):
            result = self.budget(free, reserved)
            self.assertEqual(result.total_cap_cents, expected)
            # A symmetric synthetic allocation, not a quote or actual order.
            for wallet in ("poly", "sx"):
                free[wallet] -= expected // 2
                reserved[wallet] += expected // 2
        result = self.budget(free, reserved)
        self.assertEqual(free, {"poly": 30_000, "sx": 30_000})
        self.assertEqual(result.total_cap_cents, 0)
        self.assertEqual(result.poly_available_cents, 0)
        self.assertEqual(result.sx_available_cents, 0)
        self.assertEqual(result.open_exposure_cents, 140_000)

    def test_reserve_is_per_wallet_when_capital_is_imbalanced(self):
        result = self.budget({"poly": 10_000, "sx": 190_000})
        self.assertEqual(result.total_cap_cents, 40_000)
        self.assertEqual(result.poly_available_cents, 7_000)
        self.assertEqual(result.sx_available_cents, 133_000)
        self.assertEqual(result.poly_reserve_cents, 3_000)
        self.assertEqual(result.sx_reserve_cents, 57_000)
        # A total below 400 EUR is not sufficient by itself: the small wallet
        # independently prevents spending more than its 70 EUR available.
        self.assertLess(result.poly_available_cents, result.total_cap_cents)

    def test_exposure_headroom_can_limit_an_otherwise_valid_operation(self):
        result = self.budget({"poly": 35_000, "sx": 35_000},
                             {"poly": 65_000, "sx": 65_000})
        self.assertEqual(result.operation_cap_cents, 40_000)
        self.assertEqual(result.exposure_remaining_cents, 10_000)
        self.assertEqual(result.total_cap_cents, 10_000)

    def test_stricter_configured_exposure_stops_before_wallet_reserves(self):
        policy = bankroll.BankrollPolicy(3000, 2000, 5000)
        result = self.budget({"poly": 55_000, "sx": 55_000},
                             {"poly": 45_000, "sx": 45_000}, policy)
        self.assertEqual(result.poly_available_cents, 25_000)
        self.assertEqual(result.exposure_remaining_cents, 10_000)
        self.assertEqual(result.total_cap_cents, 10_000)

    def test_revaluation_after_loss_reduces_budget_and_after_gain_increases_it(self):
        loss = self.budget({"poly": 50_000, "sx": 50_000})
        gain = self.budget({"poly": 150_000, "sx": 150_000})
        self.assertEqual((loss.total_cap_cents, gain.total_cap_cents), (20_000, 60_000))
        self.assertEqual((loss.poly_reserve_cents, gain.poly_reserve_cents), (15_000, 45_000))

    def test_large_bankroll_has_no_fixed_euro_ceiling(self):
        result = self.budget({"poly": 1_000_000, "sx": 1_000_000})
        self.assertEqual(result.total_cap_cents, 400_000)
        self.assertEqual(result.poly_reserve_cents, 300_000)

    def test_cost_of_open_operations_is_counted_but_hypothetical_profit_is_not(self):
        result = self.budget({"poly": 60_000, "sx": 80_000},
                             {"poly": 40_000, "sx": 20_000})
        self.assertEqual((result.poly_equity_cents, result.sx_equity_cents),
                         (100_000, 100_000))
        self.assertEqual(result.open_exposure_cents, 60_000)
        self.assertEqual(result.total_equity_cents, 200_000)
        self.assertEqual(result.operation_cap_cents, 40_000)
        self.assertEqual(result.poly_available_cents, 30_000)

    def test_wallet_below_its_reserve_is_never_given_negative_or_spendable_budget(self):
        result = self.budget({"poly": 20_000, "sx": 100_000},
                             {"poly": 80_000, "sx": 0})
        self.assertEqual(result.poly_reserve_cents, 30_000)
        self.assertEqual(result.poly_available_cents, 0)
        self.assertEqual(result.sx_available_cents, 70_000)
        overexposed = self.budget({"poly": 10_000, "sx": 10_000},
                                  {"poly": 90_000, "sx": 90_000})
        self.assertEqual(overexposed.total_cap_cents, 0)
        self.assertEqual(overexposed.exposure_remaining_cents, 0)

    def test_zero_and_subcent_rounding_preserve_every_reserve_cent(self):
        zero = self.budget({"poly": 0, "sx": 0})
        self.assertEqual(zero.total_cap_cents, 0)
        self.assertEqual(zero.poly_reserve_cents, 0)
        tiny = self.budget({"poly": 1, "sx": 2})
        self.assertEqual((tiny.poly_reserve_cents, tiny.sx_reserve_cents), (1, 1))
        self.assertEqual((tiny.poly_available_cents, tiny.sx_available_cents), (0, 1))
        self.assertEqual(tiny.operation_cap_cents, 0)
        self.assertEqual(tiny.total_cap_cents, 0)
        cents = self.budget({"poly": 101, "sx": 103})
        self.assertEqual((cents.poly_reserve_cents, cents.sx_reserve_cents), (31, 31))
        self.assertEqual(cents.operation_cap_cents, 40)
        self.assertEqual(cents.exposure_cap_cents, 142)

    def test_budget_and_audit_snapshot_are_immutable_and_serializable(self):
        result = self.budget({"poly": 100_000, "sx": 100_000})
        with self.assertRaises(FrozenInstanceError):
            result.total_cap_cents = 200_000
        decoded = json.loads(json.dumps(asdict(result)))
        self.assertEqual(decoded["total_cap_cents"], 40_000)
        self.assertEqual(decoded["policy"]["reserve_bps"], 3000)

    def test_input_wallet_mappings_are_not_modified(self):
        free = {"poly": 100_000, "sx": 100_000}
        reserved = {"poly": 10_000, "sx": 5_000}
        original = (dict(free), dict(reserved))
        self.budget(free, reserved)
        self.assertEqual((free, reserved), original)

    def test_wallet_inputs_reject_missing_extra_negative_and_noninteger_cents(self):
        good = {"poly": 100_000, "sx": 100_000}
        invalid = [{}, {"poly": 1}, {"poly": 1, "sx": 1, "other": 1},
                   [], None, 1, "100000"]
        invalid += [{"poly": value, "sx": 100_000} for value in
                    (-1, True, False, 1.0, float("nan"), float("inf"), "100000", None)]
        for values in invalid:
            with self.subTest(values=repr(values)):
                with self.assertRaises(ValueError):
                    bankroll.budget_for_entry(values, good, self.policy)
                with self.assertRaises(ValueError):
                    bankroll.budget_for_entry(good, values, self.policy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
