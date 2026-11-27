"""Deterministic offline checks of bounded, concurrent latency telemetry."""

import json
import threading
import unittest

from latency_metrics_v13 import LatencyMetrics


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class LatencyMetricsTests(unittest.TestCase):
    def test_quantiles_and_local_stage_attribution(self):
        metrics = LatencyMetrics(clock=FakeClock())
        for duration in range(1, 21):
            metrics.record_source("POLY", duration)
        for value in range(1, 5):
            metrics.record_decision(value, 10 * value, 100 * value,
                                    sources=("POLY", "polymarket", "SX", "private-label"))
        result = metrics.snapshot()
        self.assertEqual(result["poly"]["n"], 20)
        self.assertEqual(result["poly"]["samples"], 20)
        self.assertEqual(result["poly"]["p50_ms"], 10.5)
        self.assertAlmostEqual(result["poly"]["p95_ms"], 19.05)
        self.assertEqual(result["poly"]["max_ms"], 20.0)
        self.assertEqual(result["queue"]["p50_ms"], 2.5)
        self.assertEqual(result["rest"]["p50_ms"], 25.0)
        self.assertEqual(result["decision"]["p50_ms"], 250.0)
        self.assertEqual(result["decision_sources"], {"poly": 4, "sx": 4})
        self.assertEqual(result["decision_calls"], 4)
        self.assertEqual(result["sx"]["n"], 0)
        self.assertNotIn("private-label", json.dumps(result))

    def test_invalid_durations_do_not_lose_failure_counters(self):
        metrics = LatencyMetrics(clock=FakeClock())
        for value in (float("nan"), float("inf"), float("-inf"), -1, None, "10", True):
            self.assertTrue(metrics.record_source("poly", value, ok=False, status_code=429))
        metrics.record_source("poly", 0)
        metrics.record_source("sx", 12, status_code=429)
        metrics.record_source("sx", 15, status_code=503)
        metrics.record_decision(float("nan"), 7, -1, sources="SX Bet")
        self.assertFalse(metrics.record_source("unknown-private-name", 100, ok=False))
        result = metrics.snapshot()
        self.assertEqual(result["poly"]["calls"], 8)
        self.assertEqual(result["poly"]["n"], 1)
        self.assertEqual(result["poly"]["invalid"], 7)
        self.assertEqual(result["poly"]["errors"], 7)
        self.assertEqual(result["poly"]["http_429"], 7)
        self.assertEqual(result["poly"]["p95_ms"], 0.0)
        self.assertEqual(result["sx"]["errors"], 2)
        self.assertEqual(result["sx"]["http_429"], 1)
        self.assertEqual(result["queue"]["invalid"], 1)
        self.assertEqual(result["decision"]["invalid"], 1)
        self.assertEqual(result["rest"]["n"], 1)
        self.assertIsNone(result["queue"]["p50_ms"])
        self.assertEqual(result["decision_sources"], {"poly": 0, "sx": 1})
        json.dumps(result, allow_nan=False)

    def test_snapshot_is_observational_or_atomically_resets_window(self):
        clock = FakeClock(100)
        metrics = LatencyMetrics(clock=clock)
        metrics.record_source("sx", 42, ok=False, status_code=429)
        metrics.record_decision(1, 2, 3, sources=("sx",))
        clock.value = 130
        before = metrics.snapshot(reset=False)
        again = metrics.snapshot(reset=False)
        self.assertEqual(before, again)
        self.assertEqual(before["window"], {
            "start_monotonic": 100, "end_monotonic": 130, "seconds": 30})
        self.assertEqual(metrics.snapshot(), before)
        clock.value = 135
        empty = metrics.snapshot()
        self.assertEqual(empty["window"], {
            "start_monotonic": 130, "end_monotonic": 135, "seconds": 5})
        self.assertEqual(empty["sx"]["calls"], 0)
        self.assertEqual(empty["sx"]["errors"], 0)
        self.assertEqual(empty["sx"]["http_429"], 0)
        self.assertEqual(empty["sx"]["n"], 0)
        self.assertEqual(empty["decision_calls"], 0)
        self.assertEqual(empty["decision_sources"], {"poly": 0, "sx": 0})
        self.assertIsNone(empty["sx"]["max_ms"])

    def test_sample_limit_keeps_recent_quantiles_and_full_window_counts(self):
        metrics = LatencyMetrics(clock=FakeClock(), max_samples=3)
        for duration in (50, 1, 4, 2, 3):
            metrics.record_source("sx", duration)
            metrics.record_decision(duration, duration, duration)
        result = metrics.snapshot()
        for key in ("sx", "queue", "rest", "decision"):
            self.assertEqual(result[key]["n"], 5)
            self.assertEqual(result[key]["samples"], 3)
            self.assertEqual(result[key]["p50_ms"], 3)
            self.assertAlmostEqual(result[key]["p95_ms"], 3.9)
            self.assertEqual(result[key]["max_ms"], 50)

    def test_constructor_rejects_unbounded_or_invalid_buffer_sizes(self):
        for value in (0, -1, 2.5, True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LatencyMetrics(max_samples=value)
        with self.assertRaises(TypeError):
            LatencyMetrics(clock=None)

    def test_concurrent_writers_and_resets_preserve_every_counter(self):
        metrics = LatencyMetrics(clock=FakeClock(), max_samples=64)
        writer_count, iterations = 8, 400
        start = threading.Barrier(writer_count + 2, timeout=5)
        done = threading.Event()
        snapshots, failures = [], []

        def writer():
            try:
                start.wait()
                for number in range(iterations):
                    metrics.record_source("poly", number, ok=number % 7 != 0,
                                          status_code=429 if number % 13 == 0 else 200)
                    metrics.record_decision(1, 2, 3, sources=("poly", "sx", "poly"))
            except Exception as exc:
                failures.append(exc)

        def capture():
            try:
                start.wait()
                while not done.is_set():
                    snapshots.append(metrics.snapshot())
                    done.wait(0.001)
                snapshots.append(metrics.snapshot())
            except Exception as exc:
                failures.append(exc)

        writers = [threading.Thread(target=writer) for _ in range(writer_count)]
        collector = threading.Thread(target=capture)
        for thread in writers + [collector]:
            thread.start()
        start.wait()
        for thread in writers:
            thread.join(5)
        done.set()
        collector.join(5)
        self.assertFalse(any(thread.is_alive() for thread in writers + [collector]))
        self.assertEqual(failures, [])
        count = writer_count * iterations
        expected_errors = writer_count * sum(i % 7 == 0 or i % 13 == 0 for i in range(iterations))
        expected_429 = writer_count * sum(i % 13 == 0 for i in range(iterations))
        self.assertEqual(sum(s["poly"]["calls"] for s in snapshots), count)
        self.assertEqual(sum(s["poly"]["n"] for s in snapshots), count)
        self.assertEqual(sum(s["poly"]["errors"] for s in snapshots), expected_errors)
        self.assertEqual(sum(s["poly"]["http_429"] for s in snapshots), expected_429)
        self.assertEqual(sum(s["decision_calls"] for s in snapshots), count)
        for key in ("queue", "rest", "decision"):
            self.assertEqual(sum(s[key]["n"] for s in snapshots), count)
        for source in ("poly", "sx"):
            self.assertEqual(sum(s["decision_sources"][source] for s in snapshots), count)
        for result in snapshots:
            for key in ("poly", "sx", "queue", "rest", "decision"):
                self.assertLessEqual(result[key]["samples"], 64)


if __name__ == "__main__":
    unittest.main()
