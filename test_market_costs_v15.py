"""Fee metadata: official null versus unknown, expiry and isolated HTTP."""
from __future__ import annotations

from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import Mock, patch

import market_costs_v15 as costs


def market(ident="123", observed=1000.0, rate="0.05"):
    return {"id": ident, "feesEnabled": True,
            "feeSchedule": {"rate": rate, "exponent": 1, "takerOnly": True},
            "fees_fetched_epoch": observed}


def pair(ident="123", observed=1000.0):
    return {"poly": market(ident, observed)}


class FeeMetadataTests(unittest.TestCase):
    def test_sx_explicit_null_is_zero_and_decimal_fraction_is_not_percent(self):
        self.assertEqual(costs.parse_sx_fees({"data": {"takerPayoutFee": None}}), 0)
        self.assertEqual(costs.parse_sx_fees({"data": {"takerPayoutFee": "0.005"}}), .005)

    def test_sx_missing_invalid_or_nonfinite_rate_does_not_become_zero(self):
        for response in ({}, {"data": {}}, {"data": None},
                         {"status": "failure", "data": {"takerPayoutFee": None}},
                         *({"data": {"takerPayoutFee": value}}
                           for value in (True, False, "nan", "inf", "-0.1", "1.01", "bad", []))):
            with self.subTest(response=response), self.assertRaises(costs.FeeDataError):
                costs.parse_sx_fees(response)

    def test_poly_market_specific_rate_and_explicit_fee_free_market(self):
        self.assertEqual(costs.parse_poly_fees(market(rate="0.025"), "123"), (.025, 1))
        self.assertEqual(costs.parse_poly_fees({"id": "123", "feesEnabled": False}, "123"), (0, 1))

    def test_poly_missing_schedule_wrong_identity_and_unvalidated_curve_block(self):
        cases = [{"id": "123"}, {"id": "123", "feesEnabled": True},
                 {**market(), "id": "124"},
                 {**market(), "feeSchedule": {"rate": .05, "exponent": 2, "takerOnly": True}},
                 {**market(), "feeSchedule": {"rate": "nan", "exponent": 1, "takerOnly": True}},
                 {**market(), "feeSchedule": {"rate": .05, "exponent": 1}}]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(costs.FeeDataError):
                costs.parse_poly_fees(item, "123")

    def test_context_is_bound_to_market_and_expires(self):
        ctx = costs.FeeContext("123", .05, 1, .02, 1000, 1100)
        self.assertTrue(ctx.valid(1000, "123"))
        self.assertFalse(ctx.valid(1100, "123"))
        self.assertFalse(ctx.valid(1000, "124"))
        self.assertFalse(ctx.valid(990, "123"))
        self.assertFalse(replace(ctx, sx_taker_payout_rate=float("nan")).valid(1000))
        self.assertFalse(replace(ctx, poly_rate=True).valid(1000))

    def test_catalog_metadata_avoids_extra_http_and_context_lookup_never_fetches(self):
        get = Mock(return_value={"data": {"takerPayoutFee": "0.02"}})
        provider = costs.FeeProvider(get_json=get)
        with patch.object(costs.time, "time", return_value=1000):
            provider.update_pairs([pair(), pair()])
        self.assertEqual(provider.context_for_pair(pair(), 1000)[1], "sx_fees_pending")
        get.assert_not_called()
        provider.refresh_once(1000)
        get.assert_called_once_with(costs.SX_FEES_URL, timeout=4)
        ctx, reason = provider.context_for_pair(pair(), 1000)
        self.assertEqual(reason, "fees_verified")
        self.assertEqual(ctx.sx_taker_payout_rate, .02)
        self.assertEqual(ctx.poly_rate, .05)
        for _ in range(20):
            self.assertIsNotNone(provider.context_for_pair(pair(), 1001)[0])
        self.assertEqual(get.call_count, 1)

    def test_worker_fetches_full_market_when_catalog_fee_data_is_missing(self):
        def get(url, **kwargs):
            if url == costs.SX_FEES_URL:
                return {"data": {"takerPayoutFee": None}}
            self.assertEqual(url, costs.POLY_MARKETS_URL + "123")
            return market()
        provider = costs.FeeProvider(get_json=get)
        provider.update_pairs([{"poly": {"id": "123"}}])
        provider.refresh_once(1000)
        self.assertEqual(provider.context_for_pair(pair(), 1000)[1], "fees_verified")
        self.assertEqual(provider.context_for_pair(pair(), 1000)[0].sx_taker_payout_rate, 0)

    def test_failed_refresh_keeps_last_verified_data_only_until_expiry(self):
        get = Mock(return_value={"data": {"takerPayoutFee": ".02"}})
        provider = costs.FeeProvider(get_json=get, refresh_seconds=10, ttl_seconds=20, retry_seconds=1)
        with patch.object(costs.time, "time", return_value=1000):
            provider.update_pairs([pair()])
        provider.refresh_once(1000)
        get.side_effect = OSError("network details must not be logged")
        with self.assertLogs(costs.LOG, "WARNING") as logged:
            provider.refresh_once(1012)
        self.assertNotIn("network details", " ".join(logged.output))
        self.assertIsNotNone(provider.context_for_pair(pair(), 1019)[0])
        self.assertEqual(provider.context_for_pair(pair(), 1020)[1], "sx_fees_expired")

    def test_unknown_sx_rate_blocks_even_with_valid_poly_data(self):
        provider = costs.FeeProvider(get_json=lambda *a, **k: {"data": {}})
        with patch.object(costs.time, "time", return_value=1000):
            provider.update_pairs([pair()])
        with self.assertLogs(costs.LOG, "WARNING"):
            provider.refresh_once(1000)
        self.assertEqual(provider.context_for_pair(pair(), 1000), (None, "sx_taker_fee_missing"))

    def test_catalog_removal_drops_cost_cache_and_arbitrary_url_ids_are_ignored(self):
        provider = costs.FeeProvider(get_json=Mock())
        with patch.object(costs.time, "time", return_value=1000):
            provider.update_pairs([pair(), pair("124")])
            provider.update_pairs([pair("124"), pair("../../secret")])
        self.assertEqual(provider.snapshot(1000)["watched"], 1)
        self.assertEqual(provider.context_for_pair(pair(), 1000)[1], "fees_market_not_watched")
        self.assertNotIn("123", provider._poly)

    def test_slow_http_worker_cannot_block_scanner_cache_lookup(self):
        started, release = threading.Event(), threading.Event()
        def get(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise TimeoutError()
            return {"data": {"takerPayoutFee": None}}
        provider = costs.FeeProvider(get_json=get)
        now = time.time()
        provider.update_pairs([pair(observed=now)])
        provider.start()
        try:
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            self.assertEqual(provider.context_for_pair(pair(), now)[1], "sx_fees_pending")
            self.assertLess(time.monotonic() - before, .1)
        finally:
            release.set()
            provider.stop()
        self.assertFalse(provider.thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
