"""Offline tests for bounded, complete or rotating websocket identifier coverage."""
import unittest
from unittest.mock import Mock, patch

from feed_coverage_v15 import read_limit, select_identifiers
from sx_ws_v12 import SXBookHints
from poly_ws_v12 import PolyBookHints


class CoverageTests(unittest.TestCase):
    def test_current_catalogue_fits_both_feeds(self):
        sx, poly = SXBookHints(key="test"), PolyBookHints()
        sx.update(f"market-{i}" for i in range(200))
        poly.update(f"asset-{i}" for i in range(400))
        self.assertEqual(len(sx.watched), 200)
        self.assertEqual(len(poly.assets), 400)
        self.assertEqual(sx.coverage()["omitted"], 0)
        self.assertEqual(poly.coverage()["omitted"], 0)
        sx.signal("market-199")
        poly.mark("asset-399")
        self.assertEqual(set(sx.take_events()), {"market-199"})
        self.assertEqual(set(poly.take_events()), {"asset-399"})

    def test_catalogue_order_changes_do_not_force_reconnections(self):
        sx, poly = SXBookHints(key="test"), PolyBookHints()
        sx.update(["a", "b"])
        poly.update(["a", "b"])
        socket = Mock()
        poly.socket = socket
        with patch.object(poly, "_request_close_locked") as close:
            sx.update(["b", "a", "b"])
            poly.update(["b", "a", "a"])
        self.assertEqual(sx.watched, ("a", "b"))
        self.assertEqual(poly.assets, ("a", "b"))
        close.assert_not_called()

    def test_over_limit_rotates_without_unbounded_watchlist(self):
        sx, poly = SXBookHints(key="test", max_watched=3), PolyBookHints(max_assets=3)
        sx_seen, poly_seen = set(), set()
        for _ in range(4):
            sx.update([str(i) for i in range(10)])
            poly.update([str(i) for i in range(10)])
            self.assertEqual(len(sx.watched), 3)
            self.assertEqual(len(poly.assets), 3)
            self.assertEqual(sx.coverage()["omitted"], 7)
            sx_seen.update(sx.watched)
            poly_seen.update(poly.assets)
        self.assertEqual(sx_seen, {str(i) for i in range(10)})
        self.assertEqual(poly_seen, sx_seen)

    def test_new_membership_closes_only_through_nonblocking_path(self):
        poly = PolyBookHints()
        poly.update(["a"])
        socket = Mock()
        poly.socket = socket
        with patch.object(poly, "_request_close_locked") as close:
            poly.update(["b"])
        close.assert_called_once_with(socket)
        socket.close.assert_not_called()

    def test_limits_reject_invalid_or_above_sx_protocol_limit(self):
        for limit in (0, -1, 513, True):
            with self.assertRaises(ValueError):
                SXBookHints(key="test", max_watched=limit)
            with self.assertRaises(ValueError):
                PolyBookHints(max_assets=limit)
        with patch.dict("os.environ", {"TEST_WS_BOUND": "513"}):
            with self.assertRaises(ValueError):
                read_limit("TEST_WS_BOUND", 128, 512)

    def test_empty_and_duplicates_have_accurate_coverage(self):
        self.assertEqual(select_identifiers(["a", "a", None, "b", ""], 2),
                         (("a", "b"), 0, 2))
        watcher = SXBookHints(key="test")
        watcher.update(["a"])
        watcher.update([])
        self.assertEqual(watcher.coverage()["selected"], 0)
        self.assertEqual(watcher.coverage()["available"], 0)
        watcher.signal("a")
        self.assertEqual(watcher.take_events(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
