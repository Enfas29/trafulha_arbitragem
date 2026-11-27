"""Deterministic scheduling tests: no network, ledger, sleeps or live orders."""
import unittest

from reactive_scheduler_v13 import ReactiveScheduler


def pairs(count):
    return [{"key": str(i), "poly_asset": "p" + str(i),
             "sx": {"hash": "s" + str(i)}} for i in range(count)]


class SchedulerTests(unittest.TestCase):
    def test_one_direction_still_has_websocket_priority(self):
        scheduler = ReactiveScheduler(pairs(1))
        scheduler.ingest({"p0": 10.0}, now=10.0)
        pair, event, kind = scheduler.choose(now=10.0)
        self.assertEqual((pair["key"], event, kind),
                         ("0", (10.0, {"POLY"}), "ws"))

    def test_new_event_during_work_is_seen_at_next_opportunity(self):
        scheduler = ReactiveScheduler(pairs(3))
        first = scheduler.choose(now=10.0)
        self.assertEqual(first[0]["key"], "0")
        scheduler.ingest({"p2": 10.05}, now=10.1)
        scheduler.completed("0", now=10.1)
        pair, event, kind = scheduler.choose(now=10.1)
        self.assertEqual((pair["key"], event, kind),
                         ("2", (10.05, {"POLY"}), "ws"))

    def test_repeated_hints_keep_original_receipt_and_union_sources(self):
        scheduler = ReactiveScheduler(pairs(2))
        scheduler.ingest({"p1": 10.0}, now=10.1)
        scheduler.ingest({"p1": 10.3}, {"s1": 10.4}, now=10.5)
        self.assertEqual(scheduler.pending["1"], (10.0, {"POLY", "SX"}))
        self.assertEqual(scheduler.choose(now=10.5)[1],
                         (10.0, {"POLY", "SX"}))

    def test_completed_keeps_same_key_new_hint_and_enforces_cooldown(self):
        scheduler = ReactiveScheduler(pairs(1), min_key_interval=0.25)
        scheduler.ingest({"p0": 10.0}, now=10.0)
        scheduler.choose(now=10.0)
        scheduler.ingest({"p0": 10.05}, now=10.1)
        self.assertIsNone(scheduler.choose(now=10.1))  # still in flight
        scheduler.completed("0", now=10.1)
        self.assertEqual(scheduler.pending["0"], (10.05, {"POLY"}))
        self.assertIsNone(scheduler.choose(now=10.2))
        self.assertEqual(scheduler.idle_seconds(now=10.2), 0.05)
        self.assertAlmostEqual(scheduler.idle_seconds(now=10.34), 0.01)
        self.assertEqual(scheduler.choose(now=10.36)[1], (10.05, {"POLY"}))

    def test_hot_burst_limit_and_cold_coverage_under_continuous_updates(self):
        scheduler = ReactiveScheduler(pairs(8), max_hot_burst=4,
                                      normal_interval=0, min_key_interval=0)
        selected = []
        for tick in range(50):
            now = 10.0 + tick / 10
            scheduler.ingest({"p0": now}, now=now)
            pair, _, kind = scheduler.choose(now=now)
            selected.append((pair["key"], kind))
            scheduler.completed(pair["key"], now=now)
        self.assertEqual([kind for _, kind in selected[:5]],
                         ["ws", "ws", "ws", "ws", "rotation"])
        cold = {key for key, kind in selected if kind == "rotation"}
        self.assertTrue(set(map(str, range(1, 8))) <= cold)
        self.assertLessEqual(max(len(block) for block in
                                 "".join("H" if kind == "ws" else " "
                                         for _, kind in selected).split()), 4)

    def test_rotation_uses_spare_capacity_without_waiting_for_interval(self):
        scheduler = ReactiveScheduler(pairs(2), normal_interval=0.8)
        first = scheduler.choose(now=10.0)
        self.assertEqual(first[2], "rotation")
        scheduler.completed(first[0]["key"], now=10.1)
        self.assertEqual(scheduler.idle_seconds(now=10.1), 0.0)
        second = scheduler.choose(now=10.1)
        self.assertEqual((second[0]["key"], second[2]), ("1", "rotation"))
        scheduler.completed("1", now=10.2)
        self.assertIsNone(scheduler.choose(now=10.3))
        self.assertAlmostEqual(scheduler.idle_seconds(now=10.34), 0.01)
        self.assertEqual(scheduler.choose(now=10.36)[0]["key"], "0")

    def test_rotation_interval_only_limits_reserved_quota_under_hot_load(self):
        scheduler = ReactiveScheduler(pairs(3), normal_interval=0.8,
                                      min_key_interval=0)
        first = scheduler.choose(now=10.0)
        scheduler.completed(first[0]["key"], now=10.0)
        for tick in range(1, 7):
            now = 10.0 + tick / 10
            scheduler.ingest({"p1": now}, now=now)
            item = scheduler.choose(now=now)
            self.assertEqual((item[0]["key"], item[2]), ("1", "ws"))
            scheduler.completed("1", now=now)
        scheduler.ingest({"p1": 10.8}, now=10.8)
        item = scheduler.choose(now=10.8)
        self.assertEqual((item[0]["key"], item[2]), ("2", "rotation"))

    def test_pending_cooldown_does_not_starve_other_hot_keys(self):
        scheduler = ReactiveScheduler(pairs(3), min_key_interval=1)
        scheduler.ingest({"p0": 10.0}, now=10.0)
        scheduler.choose(now=10.0)
        scheduler.completed("0", now=10.1)
        scheduler.ingest({"p0": 10.11, "p2": 10.12}, now=10.2)
        self.assertEqual(scheduler.choose(now=10.2)[0]["key"], "2")
        self.assertIn("0", scheduler.pending)

    def test_fifo_uses_oldest_pending_timestamp(self):
        scheduler = ReactiveScheduler(pairs(4))
        scheduler.ingest({"p1": 10.4, "p2": 10.2, "p3": 10.3}, now=10.5)
        self.assertEqual(scheduler.choose(now=10.5)[0]["key"], "2")
        scheduler.completed("2", now=10.5)
        self.assertEqual(scheduler.choose(now=10.5)[0]["key"], "3")

    def test_queue_cap_expiry_and_invalid_inputs_are_observable(self):
        scheduler = ReactiveScheduler(pairs(5), max_pending=2, ttl_seconds=5)
        scheduler.ingest({"p0": 10.0, "p1": 10.1, "p2": 10.2}, now=10.3)
        self.assertEqual(set(scheduler.pending), {"1", "2"})
        self.assertEqual(scheduler.metrics["overflow"], 1)
        scheduler.ingest({"p3": float("nan"), "p4": 20.0,
                          "unknown": 10.3}, now=10.4)
        self.assertEqual(scheduler.metrics["invalid"], 2)
        self.assertEqual(scheduler.metrics["unknown"], 1)
        scheduler.ingest(now=16.0)
        self.assertFalse(scheduler.pending)
        self.assertEqual(scheduler.metrics["ttl"], 2)
        scheduler.ingest({"p4": 10.0}, now=16.0)
        self.assertEqual(scheduler.metrics["ttl"], 3)

    def test_rebuild_deduplicates_and_removes_orphan_hints(self):
        original = pairs(3)
        scheduler = ReactiveScheduler(original)
        scheduler.ingest({"p0": 10.0, "p2": 10.1}, now=10.2)
        scheduler.rebuild([original[0], original[0], original[1]], now=10.3)
        self.assertEqual([pair["key"] for pair in scheduler.pairs], ["0", "1"])
        self.assertEqual(set(scheduler.pending), {"0"})
        self.assertEqual(scheduler.metrics["rebuild"], 1)
        scheduler.ingest({"p2": 10.4}, now=10.4)
        self.assertEqual(scheduler.metrics["unknown"], 1)
        scheduler.choose(now=10.4)
        scheduler.rebuild([], now=10.5)
        scheduler.completed("0", now=10.6)
        self.assertIsNone(scheduler.choose(now=10.6))
        self.assertEqual(scheduler.idle_seconds(now=10.6), 0.05)
        self.assertFalse(scheduler._last_completed)

    def test_rotation_cursor_survives_catalog_reordering(self):
        original = pairs(3)
        scheduler = ReactiveScheduler(original, normal_interval=0)
        first = scheduler.choose(now=10.0)
        scheduler.completed(first[0]["key"], now=10.1)
        scheduler.rebuild([original[2], original[1], original[0]], now=10.2)
        self.assertEqual(scheduler.choose(now=10.2)[0]["key"], "1")

    def test_one_sx_hint_can_prioritize_both_directions_without_duplicates(self):
        markets = pairs(2)
        markets[1]["sx"]["hash"] = "s0"
        scheduler = ReactiveScheduler(markets + markets)
        scheduler.ingest(sx_events={"s0": 10.0}, now=10.0)
        self.assertEqual(set(scheduler.pending), {"0", "1"})
        first = scheduler.choose(now=10.0)
        scheduler.completed(first[0]["key"], now=10.1)
        second = scheduler.choose(now=10.1)
        self.assertNotEqual(first[0]["key"], second[0]["key"])
        self.assertEqual(first[1], (10.0, {"SX"}))
        self.assertEqual(second[1], (10.0, {"SX"}))

    def test_coverage_marks_unvisited_directions_and_survives_rebuild(self):
        original = pairs(3)
        scheduler = ReactiveScheduler(original)
        self.assertEqual(scheduler.coverage(now=10.0), {
            "directions": 3, "visited": 0, "unvisited": 3,
            "max_dispatch_age_s": None,
        })
        scheduler.choose(now=10.0)
        scheduler.completed("0", now=10.1)
        scheduler.choose(now=11.0)
        scheduler.completed("1", now=11.1)
        self.assertEqual(scheduler.coverage(now=12.0), {
            "directions": 3, "visited": 2, "unvisited": 1,
            "max_dispatch_age_s": 2.0,
        })
        scheduler.rebuild(original[1:], now=12.0)
        self.assertEqual(scheduler.coverage(now=12.0), {
            "directions": 2, "visited": 1, "unvisited": 1,
            "max_dispatch_age_s": 1.0,
        })


if __name__ == "__main__":
    unittest.main()
