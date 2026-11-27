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

    def test_live_delay_longer_than_ws_ttl_keeps_original_time_and_short_deadline(self):
        scheduler = ReactiveScheduler(pairs(2), ttl_seconds=5)
        scheduler.ingest_ready({"1": 10.0}, {"1": 32.5}, now=30.0)
        item = scheduler.choose(now=30.1)
        self.assertEqual(item[0]["key"], "1")
        self.assertEqual(item[1], (10.0, {"LIVE"}))
        self.assertFalse(scheduler._ready_expiries)
        scheduler.completed("1", now=30.2)
        scheduler.ingest_ready({"1": 10.0}, {"1": 32.5}, now=30.3)
        scheduler.ingest_ready({"1": 10.0}, {"1": 33.0}, now=31.0)  # Cannot extend.
        scheduler.ingest(now=32.6)
        self.assertNotIn("1", scheduler.pending)
        self.assertEqual(scheduler.metrics["ttl"], 1)

    def test_live_and_ws_events_coalesce_without_replacing_the_oldest_time(self):
        scheduler = ReactiveScheduler(pairs(2))
        scheduler.ingest({"p1": 19.0}, now=20.0)
        scheduler.ingest_ready({"1": 12.0}, {"1": 22.5}, now=20.0)
        scheduler.ingest(sx_events={"s1": 20.1}, now=20.2)
        self.assertEqual(scheduler.pending["1"], (12.0, {"LIVE", "POLY", "SX"}))
        scheduler.rebuild(pairs(1), now=20.3)
        self.assertFalse(scheduler.pending)
        self.assertFalse(scheduler._ready_expiries)

    def test_live_ready_hints_obey_cap_and_validate_timestamps(self):
        scheduler = ReactiveScheduler(pairs(4), max_pending=2)
        scheduler.ingest_ready({"0": 10.0, "1": 10.1, "2": 10.2},
                               {"0": 22.0, "1": 22.1, "2": 22.2}, now=20.0)
        self.assertEqual(set(scheduler.pending), {"1", "2"})
        self.assertEqual(set(scheduler._ready_expiries), {"1", "2"})
        scheduler.ingest_ready({"3": float("nan"), "missing": 10.0},
                               {"3": 22.0, "missing": 22.0}, now=20.0)
        scheduler.ingest_ready({"3": True}, {"3": 22.0}, now=20.0)
        scheduler.ingest_ready({"3": 21.0}, {"3": 22.0}, now=20.0)
        scheduler.ingest_ready({"3": 1.0}, {"3": 72.0}, now=70.0)
        self.assertEqual(scheduler.metrics["invalid"], 4)
        self.assertEqual(scheduler.metrics["unknown"], 1)
        self.assertFalse(scheduler.pending)

    def test_short_live_proof_precedes_old_ws_hints_without_renewing_its_lifetime(self):
        scheduler = ReactiveScheduler(pairs(6), min_key_interval=0)
        scheduler.ingest({"p0": 90, "p1": 90, "p2": 90, "p3": 90}, now=100)
        scheduler.ingest_ready({"4": 92}, {"4": 101.0}, now=100)
        item = scheduler.choose(now=100)
        self.assertEqual((item[0]["key"], item[1], item[2]),
                         ("4", (92.0, {"LIVE"}), "live"))
        scheduler.completed("4", now=100.2)
        scheduler.ingest_ready({"4": 92}, {"4": 100.1}, now=100.2)
        self.assertNotIn("4", scheduler.pending)
        self.assertEqual(scheduler.metrics["ttl"], 1)
        self.assertEqual(scheduler.choose(now=100.2)[0]["key"], "0")

    def test_urgency_preempts_older_background_without_changing_receipts(self):
        ranks = {"0": 2, "1": 1, "2": 0}
        scheduler = ReactiveScheduler(pairs(3), priority=lambda pair: ranks[pair["key"]])
        scheduler.ingest({"p0": 10.0, "p1": 11.0, "p2": 12.0}, now=12.0)
        selected = [scheduler.choose(now=12.1) for _ in range(3)]
        self.assertEqual([item[0]["key"] for item in selected], ["2", "1", "0"])
        self.assertEqual([item[1] for item in selected],
                         [(12.0, {"POLY"}), (11.0, {"POLY"}), (10.0, {"POLY"})])
        self.assertEqual([item[2] for item in selected], ["ws", "ws", "ws"])

    def test_same_urgency_preserves_fifo_and_stable_key_ties(self):
        scheduler = ReactiveScheduler(pairs(4), priority=lambda pair: 1)
        scheduler.ingest({"p3": 10.0, "p2": 10.0, "p1": 10.1}, now=10.2)
        self.assertEqual([scheduler.choose(now=10.2)[0]["key"] for _ in range(3)],
                         ["2", "3", "1"])

    def test_priority_updates_without_rebuild_and_skips_inflight_work(self):
        ranks, calls = {"0": 0, "1": 2, "2": 2}, []
        def priority(pair):
            calls.append(pair["key"])
            return ranks[pair["key"]]
        scheduler = ReactiveScheduler(pairs(3), priority=priority)
        scheduler.ingest({"p0": 10.0, "p1": 10.0, "p2": 10.0}, now=10.0)
        self.assertEqual(scheduler.choose(now=10.1)[0]["key"], "0")
        self.assertCountEqual(calls, ["0", "1", "2"])
        calls.clear()
        scheduler.ingest({"p0": 10.2}, now=10.2)
        ranks["2"] = 0  # The game started; no catalog refresh is required.
        self.assertEqual(scheduler.choose(now=10.3)[0]["key"], "2")
        self.assertCountEqual(calls, ["1", "2"])
        self.assertEqual(scheduler.pending["0"], (10.2, {"POLY"}))

    def test_priority_failures_fall_back_to_background_without_stopping_scanner(self):
        for invalid in (None, -1, True, "0", 0.0, float("nan"), RuntimeError("not logged")):
            with self.subTest(invalid=invalid):
                def priority(pair):
                    if pair["key"] == "1":
                        return 1
                    if isinstance(invalid, Exception):
                        raise invalid
                    return invalid
                scheduler = ReactiveScheduler(pairs(2), priority=priority)
                scheduler.ingest({"p0": 10.0, "p1": 11.0}, now=11.0)
                self.assertEqual(scheduler.choose(now=11.1)[0]["key"], "1")
                self.assertEqual(scheduler.choose(now=11.1)[0]["key"], "0")
                self.assertGreater(scheduler.metrics["priority_error"], 0)
        scheduler = ReactiveScheduler(pairs(1), priority="not callable")
        scheduler.ingest({"p0": 10.0}, now=10.0)
        self.assertEqual(scheduler.choose(now=10.0)[0]["key"], "0")
        self.assertEqual(scheduler.metrics["priority_error"], 1)

    def test_priority_rotation_covers_quiet_and_continuously_updated_background(self):
        scheduler = ReactiveScheduler(
            pairs(4), max_hot_burst=2, normal_interval=0, min_key_interval=0,
            priority=lambda pair: 0 if pair["key"] == "0" else 2)
        rotations, selected = [], []
        for tick in range(24):
            now = 10.0 + tick / 10
            # One urgent game, two background games with continuous hints,
            # and one quiet game. Every class must retain normal coverage.
            scheduler.ingest({"p0": now, "p1": now, "p2": now}, now=now)
            pair, event, kind = scheduler.choose(now=now)
            selected.append(kind)
            if kind == "rotation":
                rotations.append(pair["key"])
            scheduler.completed(pair["key"], now=now)
        self.assertEqual(selected[:3], ["ws", "ws", "rotation"])
        self.assertEqual(rotations[:4], ["0", "1", "2", "3"])
        self.assertEqual(set(rotations), {"0", "1", "2", "3"})
        self.assertEqual(scheduler.coverage(now=12.4)["unvisited"], 0)

    def test_ready_live_proof_precedes_urgency_without_changing_its_deadline(self):
        calls = []
        def priority(pair):
            calls.append(pair["key"])
            return 0 if pair["key"] == "0" else 2
        scheduler = ReactiveScheduler(pairs(3), priority=priority)
        scheduler.ingest({"p0": 90.0, "p1": 91.0}, now=100.0)
        scheduler.ingest_ready({"2": 92.0}, {"2": 100.5}, now=100.0)
        item = scheduler.choose(now=100.1)
        self.assertEqual((item[0]["key"], item[1], item[2]),
                         ("2", (92.0, {"LIVE"}), "live"))
        self.assertEqual(calls, [])
        scheduler.completed("2", now=100.2)
        scheduler.ingest_ready({"2": 92.0}, {"2": 100.5}, now=100.6)
        self.assertNotIn("2", scheduler.pending)
        self.assertEqual(scheduler.choose(now=100.6)[0]["key"], "0")

    def test_high_priority_does_not_resurrect_expired_hints(self):
        scheduler = ReactiveScheduler(pairs(2), ttl_seconds=5,
                                      priority=lambda pair: int(pair["key"]))
        scheduler.ingest({"p0": 10.0}, now=10.0)
        scheduler.ingest({"p1": 16.0}, now=16.0)
        self.assertNotIn("0", scheduler.pending)
        self.assertEqual(scheduler.metrics["ttl"], 1)
        item = scheduler.choose(now=16.1)
        self.assertEqual((item[0]["key"], item[1], item[2]),
                         ("1", (16.0, {"POLY"}), "ws"))

    def test_expiry_counts_by_priority_reconcile_with_total_ttl(self):
        ranks = {"0": 0, "1": 1, "2": 2, "3": 9}
        scheduler = ReactiveScheduler(pairs(4), ttl_seconds=5,
                                      priority=lambda pair: ranks[pair["key"]])
        scheduler.ingest({"p0": 10.0, "p1": 10.0, "p2": 10.0, "p3": 10.0}, now=10.0)
        scheduler.ingest(now=16.0)
        # Also include a stale incoming WS hint and an already-expired LIVE
        # proof; they must not disappear from the priority breakdown.
        scheduler.ingest({"p0": 10.0}, now=16.0)
        scheduler.ingest_ready({"1": 10.0}, {"1": 15.9}, now=16.0)
        self.assertEqual(scheduler.metrics["ttl"], 6)
        self.assertEqual(scheduler.metrics["ttl_priority_0"], 2)
        self.assertEqual(scheduler.metrics["ttl_priority_1"], 2)
        self.assertEqual(scheduler.metrics["ttl_priority_2"], 1)
        self.assertEqual(scheduler.metrics["ttl_priority_other"], 1)
        self.assertFalse(scheduler.pending)
        legacy = ReactiveScheduler(pairs(1), ttl_seconds=5)
        legacy.ingest({"p0": 10.0}, now=16.0)
        self.assertEqual(legacy.metrics["ttl"], 1)
        self.assertFalse(any(key.startswith("ttl_priority_") for key in legacy.metrics))

    def test_overflow_drops_background_before_current_games_and_ready_proofs(self):
        ranks = {"0": 0, "1": 1, "2": 2, "3": 2, "4": 2}
        scheduler = ReactiveScheduler(pairs(5), max_pending=3,
                                      priority=lambda pair: ranks[pair["key"]])
        scheduler.ingest({"p0": 10.0, "p1": 10.1, "p2": 10.2}, now=20.0)
        scheduler.ingest_ready({"4": 9.0}, {"4": 22.5}, now=20.0)
        self.assertEqual(set(scheduler.pending), {"0", "1", "4"})
        self.assertEqual(scheduler._ready_expiries, {"4": 22.5})
        scheduler.ingest({"p3": 20.1}, now=20.1)
        self.assertEqual(set(scheduler.pending), {"0", "1", "4"})
        self.assertEqual(scheduler.metrics["overflow"], 2)
        item = scheduler.choose(now=20.2)
        self.assertEqual((item[0]["key"], item[1], item[2]),
                         ("4", (9.0, {"LIVE"}), "live"))


if __name__ == "__main__":
    unittest.main()
