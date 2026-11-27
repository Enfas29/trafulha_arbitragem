"""Urgency changes scheduling, never the meaning or age of a live proof."""
from __future__ import annotations

import unittest
import copy
from unittest.mock import patch

import monitor_v11 as monitor
from latency_metrics_v13 import LatencyMetrics


NOW = 1_800_000_000


def pair(start_offset, *, live=False, ended=False, enabled=True):
    return {
        "key": "priority-example",
        "poly": {"id": "poly-example", "sport": "basketball", "type": "moneyline",
                 "event_id": "poly-event", "event_live": live, "event_ended": ended,
                 "start": NOW + start_offset},
        "sx": {"hash": "sx-example", "sport": "basketball", "start": NOW + start_offset,
               "teams": ["Team A", "Team B"], "live_enabled": enabled},
        "poly_asset": "asset-example", "poly_label": "Team A", "sx_label": "Team B",
        "sx_target": 1,
    }


class MonitorPriorityTests(unittest.TestCase):
    def test_catalogue_counts_games_once_across_selections_and_participant_order(self):
        first = pair(60)
        reverse = copy.deepcopy(first)
        reverse["sx"]["teams"].reverse()
        third = pair(120)
        third["sx"]["sport"] = "hockey"
        self.assertEqual(monitor.catalog_games_by_sport([first, first, reverse, third]),
                         {"basketball": 1, "hockey": 1})

    def test_active_and_soon_boundaries_use_schedule_without_claiming_live(self):
        for offset, expected in ((-8 * 3600 - 1, 2), (-8 * 3600, 0),
                                 (-60, 0), (0, 0), (1, 1), (7200, 1), (7201, 2)):
            with self.subTest(offset=offset):
                candidate = pair(offset)
                self.assertEqual(monitor.pair_dispatch_priority(candidate, NOW), expected)
                self.assertFalse(candidate["poly"]["event_live"])

    def test_ended_and_unavailable_live_do_not_consume_urgent_capacity(self):
        self.assertEqual(monitor.pair_dispatch_priority(pair(-60, ended=True, live=True), NOW), 2)
        self.assertEqual(monitor.pair_dispatch_priority(pair(-60, enabled=False), NOW), 2)
        self.assertEqual(monitor.pair_dispatch_priority(pair(60, enabled=False), NOW), 1)
        self.assertEqual(monitor.pair_dispatch_priority(pair(60, live=True), NOW), 0)
        self.assertEqual(monitor.pair_dispatch_priority(pair(3 * 3600, live=True), NOW), 2)

    def test_missing_invalid_or_outdated_timestamps_stay_background(self):
        for value in (None, True, "1800000000", float("nan"), float("inf"), 0, -1):
            candidate = pair(0)
            candidate["sx"]["start"] = value
            self.assertEqual(monitor.pair_dispatch_priority(candidate, NOW), 2)
        for candidate in ({}, None, {"poly": None, "sx": None}):
            self.assertEqual(monitor.pair_dispatch_priority(candidate, NOW), 2)
        self.assertEqual(monitor.pair_dispatch_priority(pair(-9 * 3600, live=True), NOW), 2)

    def test_urgency_changes_as_time_advances_without_waiting_for_catalog_refresh(self):
        candidate = pair(3 * 3600)
        self.assertEqual(monitor.pair_dispatch_priority(candidate, NOW), 2)
        self.assertEqual(monitor.pair_dispatch_priority(candidate, NOW + 3600), 1)
        self.assertEqual(monitor.pair_dispatch_priority(candidate, NOW + 3 * 3600), 0)

    def test_group_metrics_keep_original_event_age_and_global_measurements(self):
        for offset, expected_group in ((-60, "active_window"), (60, "starts_soon"),
                                       (3 * 3600, "background")):
            with self.subTest(group=expected_group):
                clock = [100.0]
                global_metrics = LatencyMetrics(clock=lambda: clock[0])
                groups = {name: LatencyMetrics(clock=lambda: clock[0]) for name in monitor.PRIORITY_LABELS}
                conn = monitor.open_database(":memory:")
                self.addCleanup(conn.close)
                candidate = pair(offset)
                def books(_):
                    clock[0] += .1
                    return ([(.6, 100)], clock[0]), ([(.5, 100)], clock[0])
                with patch.object(monitor, "LATENCIES", global_metrics), \
                     patch.object(monitor, "PRIORITY_LATENCIES", groups), \
                     patch.object(monitor.time, "time", return_value=NOW), \
                     patch.object(monitor.time, "monotonic", side_effect=lambda: clock[0]), \
                     patch.object(monitor, "fetch_books", side_effect=books):
                    result = monitor.scan_pass(conn, [candidate], 0, 1, verbose=False,
                                               work_item=(candidate, (93.0, {"SX"}), "ws"))
                self.assertEqual(result["checked"], 1)
                overall = global_metrics.snapshot()
                selected = groups[expected_group].snapshot()
                self.assertEqual(overall["queue"]["p50_ms"], 7000)
                self.assertEqual(overall["queue"], selected["queue"])
                self.assertEqual(overall["decision"], selected["decision"])
                self.assertAlmostEqual(overall["decision"]["p50_ms"], 7100)
                for name, group in groups.items():
                    if name != expected_group:
                        self.assertEqual(group.snapshot()["decision_calls"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
