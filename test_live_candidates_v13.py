"""Deterministic live candidate lifecycle tests; no network, money or sleep."""
from concurrent.futures import Future
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from live_candidates_v13 import LiveCandidates
from live_market_v13 import LiveCheckError


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ManualExecutor:
    """An explicit queue of real Future objects, with no worker threads."""

    def __init__(self):
        self.jobs = []
        self.shutdown_calls = []

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.jobs.append((future, fn, args, kwargs))
        return future

    def start(self, index):
        return self.jobs[index][0].set_running_or_notify_cancel()

    def run(self, index):
        future, fn, args, kwargs = self.jobs[index]
        if not future.running() and not future.set_running_or_notify_cancel():
            return False
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return True

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)


def pair(key="a"):
    return {
        "key": key,
        "poly": {"id": "poly-" + key, "event_id": "event-poly-" + key,
                 "type": "moneyline", "outcomes": ["A", "B"],
                 "assets": ["asset-" + key, "other-" + key]},
        "sx": {"hash": "sx-" + key, "event_id": "event-sx-" + key,
               "type": 52, "live_enabled": True, "start": 1700000000,
               "sport_id": 5, "league_id": 55,
               "outcomes": ["A", "B"], "teams": ["A", "B"]},
        "poly_asset": "asset-" + key, "sx_target": 1,
        "poly_label": "A", "sx_label": "B",
    }


def proof(candidate, now, delay=1000):
    return {
        "method": "official_live_v1", "pair_key": candidate["key"],
        "poly_event_id": str(candidate["poly"]["event_id"]),
        "poly_market_id": str(candidate["poly"]["id"]),
        "poly_asset_id": str(candidate["poly_asset"]),
        "poly_side": candidate["poly_label"],
        "sx_event_id": str(candidate["sx"]["event_id"]),
        "sx_market_hash": str(candidate["sx"]["hash"]),
        "sx_target": candidate["sx_target"], "sx_side": candidate["sx_label"],
        "poly_live": True, "poly_ended": False, "poly_active": True,
        "poly_closed": False, "poly_accepting_orders": True,
        "sx_fixture_status": 2, "sx_status": "ACTIVE",
        "sx_live_enabled": True, "sx_reported": False,
        "checks_started_mono": now, "checked_mono": now,
        "checked_epoch": 1700000100.0, "valid_until_mono": now + 3,
        "poly_delay_ms": 0, "sx_delay_ms": delay, "required_delay_ms": delay,
        "score": "0-1", "period": "2H",
    }


class Source:
    def __init__(self, clock, delay=1000):
        self.clock = clock
        self.delay = delay
        self.calls = []
        self.responses = []

    def __call__(self, candidate):
        self.calls.append((deepcopy(candidate), self.clock()))
        response = self.responses.pop(0) if self.responses else self.delay
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(candidate)
        return proof(candidate, self.clock(), response)


class LiveCandidateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.executor = ManualExecutor()
        self.source = Source(self.clock)
        self.queue = LiveCandidates(self.source, clock=self.clock,
                                    executor=self.executor)
        self.addCleanup(self.queue.stop)
        self.pair = pair()

    def initial(self):
        self.assertEqual(self.queue.consider(self.pair), (None, "live_em_verificacao"))
        self.executor.run(0)
        self.queue.tick()

    def ready(self, delay=1000):
        self.source.delay = delay
        self.initial()
        self.clock.advance(delay / 1000)
        self.queue.tick()
        self.executor.run(1)
        self.queue.tick()

    def test_two_fresh_checks_start_after_delay_without_blocking(self):
        self.assertEqual(self.queue.consider(self.pair), (None, "live_em_verificacao"))
        self.assertEqual(self.source.calls, [])
        self.clock.advance(.5)
        self.executor.run(0)
        self.queue.tick()
        self.assertEqual(self.queue.consider(self.pair), (None, "live_aguarda_atraso"))
        self.clock.now = 101.49
        self.queue.tick()
        self.assertEqual(len(self.executor.jobs), 1)
        self.clock.now = 101.5
        self.queue.tick()
        self.assertEqual(len(self.executor.jobs), 2)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(self.queue.consider(self.pair), (None, "live_em_verificacao"))
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.take_ready_events(), {"a": 100.0})
        self.assertEqual(self.queue.take_ready_events(), {})
        evidence, reason = self.queue.consider(self.pair)
        self.assertEqual(reason, "live_confirmado")
        self.assertEqual(evidence["candidate_started_mono"], 100.0)
        self.assertEqual(evidence["delay_started_mono"], 100.5)
        self.assertEqual(evidence["delay_ready_mono"], 101.5)
        self.assertEqual(evidence["checks_started_mono"], 101.5)
        self.assertEqual(evidence["initial_required_delay_ms"], 1000)
        self.assertIs(evidence["verified_after_delay"], True)
        self.assertEqual(self.queue.snapshot()["confirmadas"], 1)
        self.assertEqual(self.queue.snapshot()["pending"], 0)
        self.assertEqual(self.queue.consider(self.pair), (None, "live_em_verificacao"))
        self.assertEqual(len(self.executor.jobs), 3)

    def test_delay_starts_at_worker_completion_not_later_tick(self):
        self.queue.consider(self.pair)
        self.clock.now = 100.25
        self.executor.run(0)
        self.clock.now = 101.25
        self.queue.tick()
        self.assertEqual(len(self.executor.jobs), 2)
        self.executor.run(1)
        evidence, _ = self.queue.consider(self.pair)
        self.assertEqual(evidence["delay_started_mono"], 100.25)
        self.assertEqual(evidence["delay_ready_mono"], 101.25)

    def test_zero_delay_still_needs_independent_second_check(self):
        self.source.delay = 0
        self.initial()
        self.assertEqual(len(self.executor.jobs), 2)
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.consider(self.pair), (None, "live_em_verificacao"))
        self.executor.run(1)
        evidence, _ = self.queue.consider(self.pair)
        self.assertTrue(evidence["verified_after_delay"])
        self.assertEqual(len(self.source.calls), 2)

    def test_repeated_consider_deduplicates_all_pending_phases(self):
        for _ in range(20):
            self.queue.consider(self.pair)
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run(0)
        for _ in range(20):
            self.assertEqual(self.queue.consider(self.pair)[1], "live_aguarda_atraso")
        self.clock.advance(1)
        for _ in range(20):
            self.assertEqual(self.queue.consider(self.pair)[1], "live_em_verificacao")
        self.assertEqual(len(self.executor.jobs), 2)

    def test_delay_increase_extends_original_deadline_and_requires_new_check(self):
        self.source.responses = [1000, 4000, 500]
        self.initial()
        self.clock.now = 101
        self.queue.tick()
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.consider(self.pair)[1], "live_aguarda_atraso")
        self.clock.now = 103.99
        self.queue.tick()
        self.assertEqual(len(self.executor.jobs), 2)
        self.clock.now = 104
        self.queue.tick()
        self.executor.run(2)
        evidence, _ = self.queue.consider(self.pair)
        self.assertEqual(evidence["delay_started_mono"], 100)
        self.assertEqual(evidence["delay_ready_mono"], 104)
        self.assertEqual(evidence["checks_started_mono"], 104)
        self.assertEqual(evidence["initial_required_delay_ms"], 1000)
        self.assertEqual(evidence["required_delay_ms"], 500)
        self.assertEqual(self.queue.snapshot()["metrics"]["delay_extensions"], 1)

    def test_increased_delay_requires_new_check_even_when_already_elapsed(self):
        self.source.responses = [1000, 1500, 1500]
        self.initial()
        self.clock.now = 102
        self.queue.tick()
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(len(self.executor.jobs), 3)
        self.executor.run(2)
        evidence, _ = self.queue.consider(self.pair)
        self.assertEqual(evidence["delay_ready_mono"], 101.5)
        self.assertEqual(len(self.source.calls), 3)

    def test_lower_final_delay_never_shortens_initial_wait(self):
        self.source.responses = [5000, 1000]
        self.initial()
        self.clock.now = 104.9
        self.queue.tick()
        self.assertEqual(len(self.executor.jobs), 1)
        self.clock.now = 105
        self.queue.tick()
        self.executor.run(1)
        evidence, _ = self.queue.consider(self.pair)
        self.assertEqual(evidence["delay_ready_mono"], 105)
        self.assertEqual(evidence["initial_required_delay_ms"], 5000)

    def test_source_failure_has_safe_reason_and_two_second_backoff(self):
        self.source.responses = [LiveCheckError("poly_not_live")]
        self.initial()
        self.assertEqual(self.queue.consider(self.pair), (None, "poly_not_live"))
        self.clock.now = 101.99
        self.assertEqual(self.queue.consider(self.pair), (None, "poly_not_live"))
        self.assertEqual(len(self.executor.jobs), 1)
        status = self.queue.snapshot()
        self.assertEqual(status["ultima_falha"], "poly_not_live")
        self.assertEqual(status["falhas"], {"poly_not_live": 1})
        self.assertEqual(status["rejeitadas"], 1)
        self.clock.now = 102
        self.queue.tick()
        self.assertEqual(len(self.executor.jobs), 1)  # Never retries by itself.
        self.assertEqual(self.queue.consider(self.pair)[1], "live_em_verificacao")
        self.assertEqual(len(self.executor.jobs), 2)

    def test_exception_messages_and_untrusted_reason_never_escape(self):
        for exception, expected in (
                (RuntimeError("secret https://url?api_key=credentials"), "RuntimeError"),
                (LiveCheckError("secret https://url?api_key=credentials"), "LiveCheckError")):
            with self.subTest(expected=expected):
                executor = ManualExecutor()
                source = Source(self.clock)
                source.responses = [exception]
                queue = LiveCandidates(source, clock=self.clock, executor=executor)
                self.addCleanup(queue.stop)
                queue.consider(self.pair)
                executor.run(0)
                self.assertEqual(queue.consider(self.pair), (None, expected))
                self.assertNotIn("secret", json.dumps(queue.snapshot()))
                self.assertNotIn("credentials", json.dumps(queue.snapshot()))

    def test_final_failure_never_returns_initial_evidence(self):
        self.source.responses = [1000, LiveCheckError("sx_fixture_not_live")]
        self.initial()
        self.clock.advance(1)
        self.queue.tick()
        self.executor.run(1)
        self.assertEqual(self.queue.consider(self.pair), (None, "sx_fixture_not_live"))
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.ready_deadlines(), {})

    def test_expired_before_polling_is_rejected(self):
        self.queue.consider(self.pair)
        self.executor.run(0)
        self.clock.advance(3)
        self.assertEqual(self.queue.consider(self.pair), (None, "live_prova_expirada"))
        self.assertEqual(len(self.executor.jobs), 1)
        self.assertEqual(self.queue.take_ready_events(), {})

    def test_ready_proof_expires_at_exact_deadline_and_then_cools_down(self):
        self.ready()
        self.assertEqual(self.queue.ready_deadlines(), {"a": 104})
        self.clock.now = 104
        self.assertEqual(self.queue.consider(self.pair), (None, "live_prova_expirada"))
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.ready_deadlines(), {})
        self.clock.now = 105.99
        self.queue.consider(self.pair)
        self.assertEqual(len(self.executor.jobs), 2)
        self.clock.now = 106
        self.queue.consider(self.pair)
        self.assertEqual(len(self.executor.jobs), 3)

    def test_ready_deadlines_are_original_before_and_after_event_consumption(self):
        self.ready()
        deadlines = self.queue.ready_deadlines()
        self.assertEqual(deadlines, {"a": 104})
        self.assertEqual(self.queue.take_ready_events(), {"a": 100})
        self.clock.advance(.75)
        deadlines["a"] = 999999
        self.assertEqual(self.queue.ready_deadlines(), {"a": 104})
        self.assertEqual(self.queue.take_ready_events(), {})
        self.queue.consider(self.pair)
        self.assertEqual(self.queue.ready_deadlines(), {})

    def test_candidate_age_limit_discards_long_delays(self):
        self.queue.max_age_seconds = 2
        self.source.delay = 8000
        self.initial()
        self.clock.advance(2)
        self.assertEqual(self.queue.consider(self.pair), (None, "live_candidato_expirado"))
        self.assertEqual(len(self.executor.jobs), 1)
        self.assertEqual(self.queue.take_ready_events(), {})

    def test_capacity_includes_waiting_candidates(self):
        self.queue.max_pending = 2
        self.queue.consider(pair("a"))
        self.queue.consider(pair("b"))
        self.executor.run(0)
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.consider(pair("c")), (None, "live_fila_cheia"))
        self.assertEqual(self.queue.snapshot()["jobs"], 0)
        self.assertEqual(self.queue.snapshot()["slots"], 2)
        self.queue.discard("a")
        self.assertEqual(self.queue.consider(pair("c"))[1], "live_em_verificacao")

    def test_capacity_includes_discarded_running_jobs(self):
        self.queue.max_pending = 2
        for index in range(2):
            self.queue.consider(self.pair)
            self.executor.start(index)
            self.queue.discard("a")
        self.assertEqual(self.queue.snapshot()["pending"], 0)
        self.assertEqual(self.queue.snapshot()["orphan_jobs"], 2)
        self.assertEqual(self.queue.consider(self.pair), (None, "live_fila_cheia"))
        self.executor.run(0)
        self.queue.tick()
        self.assertEqual(self.queue.consider(self.pair)[1], "live_em_verificacao")
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.snapshot()["phases"], {"checking": 1})
        self.assertEqual(self.queue.snapshot()["metrics"]["stale_results"], 2)

    def test_expired_running_job_still_occupies_capacity_after_cooldown(self):
        self.queue.max_age_seconds = 1
        self.queue.max_pending = 1
        self.queue.consider(self.pair)
        self.executor.start(0)
        self.clock.now = 101
        self.queue.tick()
        self.clock.now = 103
        self.assertEqual(self.queue.consider(self.pair), (None, "live_fila_cheia"))
        self.executor.run(0)
        self.queue.tick()
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.consider(self.pair)[1], "live_em_verificacao")

    def test_rebuild_preserves_same_identity_and_removes_orphans(self):
        self.initial()
        self.queue.rebuild([deepcopy(self.pair)])
        self.assertEqual(self.queue.consider(self.pair)[1], "live_aguarda_atraso")
        self.assertEqual(len(self.executor.jobs), 1)
        self.queue.rebuild([])
        self.assertEqual(self.queue.snapshot()["pending"], 0)
        self.assertEqual(self.queue.consider(self.pair)[1], "live_par_fora_catalogo")

    def test_rebuild_generation_rejects_late_final_completion(self):
        self.initial()
        self.clock.now = 101
        self.queue.tick()
        self.executor.start(1)
        updated = deepcopy(self.pair)
        updated["sx"]["event_id"] = "replacement-event"
        self.queue.rebuild([updated])
        self.queue.consider(updated)
        self.executor.run(1)  # Old generation may finish, never resurrect.
        self.queue.tick()
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.snapshot()["phases"], {"checking": 1})
        self.assertEqual(self.queue.consider(self.pair)[1], "live_par_desatualizado")
        self.executor.run(2)
        self.queue.tick()
        self.clock.now = 102
        self.queue.tick()
        self.executor.run(3)
        evidence, _ = self.queue.consider(updated)
        self.assertEqual(evidence["sx_event_id"], "replacement-event")
        self.assertEqual(evidence["candidate_started_mono"], 101)

    def test_direct_identity_change_invalidates_ready_proof(self):
        self.ready()
        updated = deepcopy(self.pair)
        updated["poly"]["event_id"] = "replacement-event"
        self.assertEqual(self.queue.consider(updated)[1], "live_em_verificacao")
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.ready_deadlines(), {})

    def test_discard_removes_ready_events_and_cancels_queued_work(self):
        self.ready()
        self.assertTrue(self.queue.discard("a"))
        self.assertFalse(self.queue.discard("a"))
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.ready_deadlines(), {})
        self.queue.consider(self.pair)
        self.queue.discard("a")
        self.assertTrue(self.executor.jobs[2][0].cancelled())
        self.assertEqual(self.queue.snapshot()["jobs"], 0)

    def test_worker_cannot_mutate_catalog_or_next_check_metadata(self):
        original = deepcopy(self.pair)

        def mutating(candidate):
            result = proof(candidate, self.clock())
            candidate["poly"]["outcomes"].append("not a real side")
            candidate["sx"]["event_id"] = "changed by worker"
            return result

        self.source.responses = [mutating, mutating]
        self.ready()
        evidence, _ = self.queue.consider(self.pair)
        self.assertIsNotNone(evidence)
        self.assertEqual(self.pair, original)
        self.assertEqual(self.source.calls[0][0], original)
        self.assertEqual(self.source.calls[1][0], original)

    def test_incomplete_or_inconsistent_evidence_has_no_fallback(self):
        corruptions = [
            {"poly_live": False}, {"poly_accepting_orders": 1},
            {"sx_fixture_status": True}, {"sx_fixture_status": 1},
            {"sx_reported": True}, {"method": "scheduled_time_only"},
            {"poly_market_id": "wrong"}, {"poly_asset_id": 123},
            {"poly_side": "B"}, {"sx_target": True},
            {"required_delay_ms": True}, {"required_delay_ms": 1},
            {"sx_delay_ms": -1, "required_delay_ms": -1},
            {"checked_mono": float("nan")}, {"checked_mono": 9999},
            {"checks_started_mono": 99, "valid_until_mono": 102},
            {"valid_until_mono": 9999},
        ]
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                executor = ManualExecutor()
                source = Source(self.clock, delay=0)
                queue = LiveCandidates(source, clock=self.clock, executor=executor)
                self.addCleanup(queue.stop)
                queue.consider(self.pair)
                executor.run(0)
                queue.tick()
                source.responses = [lambda candidate, values=corruption:
                                    {**proof(candidate, self.clock(), 0), **values}]
                executor.run(1)
                evidence, reason = queue.consider(self.pair)
                self.assertIsNone(evidence)
                self.assertEqual(reason, "live_prova_invalida")
                self.assertEqual(queue.take_ready_events(), {})
        for incomplete in (None, {}, {"verified_after_delay": True}):
            with self.subTest(incomplete=incomplete):
                executor = ManualExecutor()
                queue = LiveCandidates(lambda _: incomplete, clock=self.clock,
                                       executor=executor)
                self.addCleanup(queue.stop)
                queue.consider(self.pair)
                executor.run(0)
                self.assertEqual(queue.consider(self.pair), (None, "live_prova_invalida"))

    def test_ready_evidence_contains_no_injected_book_or_price_cache(self):
        self.source.responses = [0, lambda candidate: {
            **proof(candidate, self.clock(), 0), "book": [[.3, 20]], "odds": 3.1,
            "verified_after_delay": False, "candidate_started_mono": -1000}]
        self.initial()
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.take_ready_events(), {"a": 100})
        evidence, _ = self.queue.consider(self.pair)
        self.assertNotIn("book", evidence)
        self.assertNotIn("odds", evidence)
        self.assertTrue(evidence["verified_after_delay"])
        self.assertEqual(evidence["candidate_started_mono"], 100)

    def test_submit_failure_is_safe_and_does_not_retry_during_backoff(self):
        with patch.object(self.executor, "submit", side_effect=RuntimeError("secret URL")) as submit:
            self.assertEqual(self.queue.consider(self.pair), (None, "RuntimeError"))
            self.assertEqual(self.queue.consider(self.pair), (None, "RuntimeError"))
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.queue.snapshot()["jobs"], 0)

    def test_default_fetch_is_lazy_and_uses_the_coordinator_clock(self):
        executor = ManualExecutor()
        queue = LiveCandidates(clock=self.clock, executor=executor)
        self.addCleanup(queue.stop)
        with patch("live_market_v13.fetch_live_snapshot", side_effect=
                   lambda candidate, clock: proof(candidate, clock())) as fetch:
            queue.consider(self.pair)
            fetch.assert_not_called()
            executor.run(0)
            fetch.assert_called_once()
            self.assertIs(fetch.call_args.kwargs["clock"], self.clock)

    def test_stop_never_waits_for_running_owned_workers(self):
        executor = ManualExecutor()
        with patch("live_candidates_v13.ThreadPoolExecutor", return_value=executor):
            queue = LiveCandidates(self.source, clock=self.clock)
            queue.consider(self.pair)
            executor.start(0)
            queue.stop()
        self.assertEqual(executor.shutdown_calls, [{"wait": False, "cancel_futures": True}])
        self.assertEqual(queue.consider(self.pair), (None, "live_parado"))
        self.assertEqual(queue.snapshot()["orphan_jobs"], 1)
        executor.run(0)
        queue.tick()
        self.assertEqual(queue.take_ready_events(), {})
        self.assertEqual(queue.snapshot()["jobs"], 0)
        queue.stop()
        self.assertEqual(len(executor.shutdown_calls), 1)

    def test_stop_cancels_queued_work_but_does_not_shutdown_injected_executor(self):
        self.queue.consider(self.pair)
        self.queue.stop()
        self.assertTrue(self.executor.jobs[0][0].cancelled())
        self.assertEqual(self.executor.shutdown_calls, [])
        self.assertEqual(self.queue.snapshot()["pending"], 0)

    def test_invalid_or_ambiguous_catalog_never_schedules_work(self):
        self.assertEqual(self.queue.consider({}), (None, "live_par_invalido"))
        conflicting = deepcopy(self.pair)
        conflicting["poly_asset"] = "other-a"
        self.queue.rebuild([self.pair, conflicting])
        self.assertEqual(self.queue.consider(self.pair), (None, "live_par_fora_catalogo"))
        self.assertEqual(len(self.executor.jobs), 0)


if __name__ == "__main__":
    unittest.main()
