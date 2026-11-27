"""Real bounded concurrency: slow markets cannot hold every other read."""
import threading
import time
import unittest

from parallel_books_v15 import ParallelBooks


def item(key):
    return ({"key": key}, None, "rotation")


def poll_until(pool, count, timeout=2):
    found = []
    deadline = time.monotonic() + timeout
    while len(found) < count and time.monotonic() < deadline:
        found.extend(pool.poll())
        if len(found) < count:
            time.sleep(.001)
    return found


class ParallelBookTests(unittest.TestCase):
    def test_slow_market_does_not_block_fast_market_and_capacity_is_bounded(self):
        release = threading.Event()
        slow_started = threading.Event()
        calls = []
        def read(pair):
            calls.append(pair["key"])
            if pair["key"] == "slow":
                slow_started.set()
                if not release.wait(2):
                    raise TimeoutError("offline timeout")
            return pair["key"]
        pool = ParallelBooks(read, 2)
        try:
            self.assertTrue(pool.submit(item("slow"), 1))
            self.assertTrue(slow_started.wait(1))
            self.assertFalse(pool.submit(item("slow"), 1))
            self.assertTrue(pool.submit(item("fast"), 1))
            self.assertFalse(pool.submit(item("overflow"), 1))
            results = poll_until(pool, 1)
            self.assertEqual([r.books for r in results], ["fast"])
            self.assertEqual(pool.pending, 1)
            self.assertNotIn("overflow", calls)
            release.set()
            results = poll_until(pool, 1)
            self.assertEqual([r.books for r in results], ["slow"])
            self.assertEqual(pool.poll(), [])  # every dispatch consumed once
        finally:
            release.set()
            pool.stop()

    def test_failure_is_local_and_does_not_poison_next_market(self):
        def read(pair):
            if pair["key"] == "bad":
                raise TimeoutError("offline")
            return "fresh"
        pool = ParallelBooks(read, 1)
        try:
            pool.submit(item("bad"), 3)
            failure = poll_until(pool, 1)[0]
            self.assertIsInstance(failure.error, TimeoutError)
            self.assertIsNone(failure.books)
            self.assertEqual(failure.generation, 3)
            self.assertTrue(pool.submit(item("good"), 4))
            good = poll_until(pool, 1)[0]
            self.assertEqual(good.books, "fresh")
            self.assertIsNone(good.error)
            self.assertEqual(good.generation, 4)
        finally:
            pool.stop()

    def test_later_dispatch_reads_again_without_reusing_prices(self):
        calls = []
        def read(pair):
            calls.append(pair["key"])
            return len(calls)
        pool = ParallelBooks(read, 1)
        try:
            pool.submit(item("same"), 1)
            first = poll_until(pool, 1)[0]
            pool.submit(item("same"), 1)
            second = poll_until(pool, 1)[0]
            self.assertEqual((first.books, second.books), (1, 2))
            self.assertLessEqual(first.started_mono, first.finished_mono)
            self.assertLessEqual(first.finished_mono, second.started_mono)
        finally:
            pool.stop()

    def test_stop_rejects_new_reads(self):
        pool = ParallelBooks(lambda pair: pair, 1)
        pool.stop()
        self.assertEqual(pool.available, 0)
        self.assertFalse(pool.submit(item("new"), 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
