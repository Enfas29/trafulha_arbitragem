"""Bounded, nonblocking live verification for the PAPER scanner.

Only the scanner thread owns the state below. Workers receive private catalog
copies and return public phase/delay evidence; they never access the ledger,
prices, balances or order APIs. A second independent check must start after the
largest observed delay, even when that delay is zero. Ready events contain only
a pair identifier and its original candidate timestamp.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import time


BACKOFF_SECONDS = 2.0
MAX_EVIDENCE_AGE_SECONDS = 3.0
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_SAFE_EXCEPTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_IDENTITY_FIELDS = ("key", "poly", "sx", "poly_asset", "sx_target",
                    "poly_label", "sx_label")
_EVIDENCE_FIELDS = (
    "method", "pair_key", "poly_event_id", "poly_market_id", "poly_asset_id",
    "poly_side", "sx_event_id", "sx_market_hash", "sx_target", "sx_side",
    "poly_live", "poly_ended", "poly_active", "poly_closed",
    "poly_accepting_orders", "sx_fixture_status", "sx_status",
    "sx_live_enabled", "sx_reported", "checks_started_mono", "checked_mono",
    "checked_epoch", "valid_until_mono", "sx_delay_ms", "poly_delay_ms",
    "required_delay_ms",
)


def _number(value):
    if type(value) not in (int, float):
        raise ValueError("Expected a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Expected a finite number")
    return number


def _clock_now(clock):
    return _number(clock())


def _fingerprint(pair):
    if (not isinstance(pair, dict)
            or not isinstance(pair.get("key"), str) or not pair["key"]
            or not isinstance(pair.get("poly"), dict)
            or not isinstance(pair.get("sx"), dict)):
        raise ValueError("Invalid catalog pair")
    # The complete source metadata participates in identity. A refreshed side,
    # asset, start time, source ID or rule invalidates any earlier verification.
    metadata = {key: pair.get(key) for key in _IDENTITY_FIELDS}
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _public_error(exc):
    # LiveCheckError is the only exception whose content is public by contract.
    # Import on use; merely importing this coordinator never loads the API code.
    try:
        from live_market_v13 import LiveCheckError
    except ImportError:
        LiveCheckError = ()
    if isinstance(exc, LiveCheckError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason):
            return reason
    name = type(exc).__name__
    return name if _SAFE_EXCEPTION.fullmatch(name) else "Exception"


@dataclass(frozen=True)
class _FetchResult:
    evidence: object
    completed_mono: float


def _fetch_result(fetch_snapshot, clock, pair):
    if fetch_snapshot is None:
        from live_market_v13 import fetch_live_snapshot
        evidence = fetch_live_snapshot(pair, clock=clock)
    else:
        evidence = fetch_snapshot(pair)
    # This is the actual worker completion, not a later scanner polling time.
    return _FetchResult(evidence, _clock_now(clock))


@dataclass(frozen=True)
class _Job:
    key: str
    generation: int
    stage: str
    submitted_mono: float


@dataclass
class _Candidate:
    key: str
    pair: dict
    fingerprint: bytes
    generation: int
    candidate_started_mono: float
    phase: str = "checking"
    future: object = None
    delay_started_mono: float | None = None
    delay_ready_mono: float | None = None
    initial_required_delay_ms: float | None = None
    largest_required_delay_ms: float = 0
    evidence: dict | None = None
    cooldown_until_mono: float | None = None
    reason: str | None = None


class _InvalidEvidence(ValueError):
    def __init__(self, reason="live_prova_invalida"):
        self.reason = reason
        super().__init__(reason)


class LiveCandidates:
    """Coordinate public live checks without waiting in the scanner thread.

    ``consider``, ``tick``, ``rebuild`` and all other public methods must be
    called by the scanner thread. An injected executor is caller-owned; an
    internally created pool is shut down without waiting for HTTP workers.

    ``max_pending`` bounds both candidate slots and all outstanding jobs,
    including running work discarded by a catalog change. Errors occupy a
    two-second cooldown slot, so repeatedly scanning one failing key cannot
    flood either provider. No candidate is retried without a new ``consider``.
    """

    def __init__(self, fetch_snapshot=None, clock=time.monotonic,
                 max_pending=16, max_workers=2, max_age_seconds=60,
                 executor=None):
        if fetch_snapshot is not None and not callable(fetch_snapshot):
            raise ValueError("fetch_snapshot must be callable")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if (type(max_pending) is not int or max_pending < 1
                or type(max_workers) is not int or max_workers < 1):
            raise ValueError("Worker and pending limits must be positive integers")
        self.max_age_seconds = _number(max_age_seconds)
        if self.max_age_seconds <= 0:
            raise ValueError("Candidate age must be positive")
        self.fetch_snapshot = fetch_snapshot
        self.clock = clock
        self.max_pending = max_pending
        self.max_workers = max_workers
        self._executor = executor
        self._owns_executor = executor is None
        self._states = {}
        self._jobs = {}
        self._ready_events = {}
        self._catalog = None
        self._generation = 0
        self._stopped = False
        self._metrics = Counter(dict(submitted=0, initial_checks=0, final_checks=0,
                                     initial_ok=0, verified=0, consumed=0,
                                     rejected=0, full=0, cancelled=0,
                                     stale_results=0, delay_extensions=0,
                                     expired=0, discarded=0, rebuild=0))
        self._failures = Counter()
        self._last_failure = None

    def _record_failure(self, reason):
        self._metrics["rejected"] += 1
        self._failures[reason] += 1
        self._last_failure = reason

    def _cancel(self, future):
        if future is None:
            return
        if future.cancel():
            self._metrics["cancelled"] += 1
        # A running call may not be cancellable. Keep counting it until done.
        if future.done():
            self._jobs.pop(future, None)

    def _remove(self, key):
        state = self._states.pop(key, None)
        self._ready_events.pop(key, None)
        if state is not None:
            self._cancel(state.future)
        return state is not None

    def _reject(self, state, reason, now):
        self._cancel(state.future)
        state.future = None
        state.phase = "backoff"
        state.reason = reason
        state.cooldown_until_mono = now + BACKOFF_SECONDS
        state.evidence = None
        self._ready_events.pop(state.key, None)
        self._record_failure(reason)

    def _orphan_jobs(self):
        return sum(1 for job in self._jobs.values()
                   if (job.key not in self._states
                       or self._states[job.key].generation != job.generation))

    def _slots(self):
        return len(self._states) + self._orphan_jobs()

    def _schedule(self, state, stage, now):
        if self._stopped or len(self._jobs) >= self.max_pending:
            return False
        try:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="live-candidates-v13")
            # A source verifier cannot modify either the catalog or the saved
            # metadata subsequently used for the independent second check.
            future = self._executor.submit(
                _fetch_result, self.fetch_snapshot, self.clock,
                deepcopy(state.pair))
        except Exception as exc:
            self._reject(state, _public_error(exc), _clock_now(self.clock))
            return False
        state.future = future
        state.phase = "checking" if stage == "initial" else "verifying"
        self._jobs[future] = _Job(state.key, state.generation, stage, now)
        self._metrics["submitted"] += 1
        self._metrics["initial_checks" if stage == "initial" else "final_checks"] += 1
        return True

    @staticmethod
    def _proof(result, state, job, now):
        """Fail closed on malformed, mismatched, cached or expired evidence."""
        if not isinstance(result, _FetchResult) or not isinstance(result.evidence, dict):
            raise _InvalidEvidence()
        raw = result.evidence
        try:
            evidence = {key: raw[key] for key in _EVIDENCE_FIELDS}
            if (raw["method"] != "official_live_v1"
                    or raw["pair_key"] != state.key):
                raise _InvalidEvidence()
            for key, expected in (("poly_live", True), ("poly_ended", False),
                                  ("poly_active", True), ("poly_closed", False),
                                  ("poly_accepting_orders", True),
                                  ("sx_live_enabled", True), ("sx_reported", False)):
                if raw[key] is not expected:
                    raise _InvalidEvidence()
            if (type(raw["sx_fixture_status"]) is not int
                    or raw["sx_fixture_status"] != 2 or raw["sx_status"] != "ACTIVE"):
                raise _InvalidEvidence()
            pair = state.pair
            for field, expected in (
                    ("poly_event_id", pair["poly"]["event_id"]),
                    ("poly_market_id", pair["poly"]["id"]),
                    ("poly_asset_id", pair["poly_asset"]),
                    ("sx_event_id", pair["sx"]["event_id"]),
                    ("sx_market_hash", pair["sx"]["hash"])):
                if (not isinstance(raw[field], str)
                        or type(expected) not in (str, int)
                        or not str(raw[field]) or str(raw[field]) != str(expected)):
                    raise _InvalidEvidence()
            if (raw["poly_side"] != pair["poly_label"]
                    or raw["sx_side"] != pair["sx_label"]
                    or not isinstance(raw["poly_side"], str)
                    or not isinstance(raw["sx_side"], str)
                    or type(raw["sx_target"]) is not int
                    or type(pair["sx_target"]) is not int
                    or raw["sx_target"] not in (0, 1)
                    or raw["sx_target"] != pair["sx_target"]):
                raise _InvalidEvidence()
            started = _number(raw["checks_started_mono"])
            checked = _number(raw["checked_mono"])
            deadline = _number(raw["valid_until_mono"])
            completed = _number(result.completed_mono)
            _number(raw["checked_epoch"])
            delays = [_number(raw[key]) for key in
                      ("poly_delay_ms", "sx_delay_ms", "required_delay_ms")]
            if (any(type(raw[key]) is not int for key in
                    ("poly_delay_ms", "sx_delay_ms", "required_delay_ms"))
                    or any(delay < 0 for delay in delays)
                    or delays[2] != max(delays[:2])
                    or not (job.submitted_mono <= started <= checked <= completed <= now)
                    or deadline != started + MAX_EVIDENCE_AGE_SECONDS):
                raise _InvalidEvidence()
            if deadline <= now:
                raise _InvalidEvidence("live_prova_expirada")
            if job.stage == "final" and started < state.delay_ready_mono:
                raise _InvalidEvidence("live_verificacao_antes_atraso")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, _InvalidEvidence):
                raise
            raise _InvalidEvidence() from None
        for key in ("score", "period"):
            if isinstance(raw.get(key), str):
                evidence[key] = " ".join(raw[key].split())[:80]
        return evidence

    def _receive(self, state, job, result, now):
        try:
            evidence = self._proof(result, state, job, now)
        except _InvalidEvidence as exc:
            if exc.reason == "live_prova_expirada":
                self._metrics["expired"] += 1
            self._reject(state, exc.reason, now)
            return
        required = evidence["required_delay_ms"]
        if job.stage == "initial":
            state.delay_started_mono = result.completed_mono
            state.initial_required_delay_ms = required
            state.largest_required_delay_ms = required
            state.delay_ready_mono = result.completed_mono + required / 1000.0
            if not math.isfinite(state.delay_ready_mono):
                self._reject(state, "live_prova_invalida", now)
                return
            state.phase = "waiting"
            self._metrics["initial_ok"] += 1
            return
        if required > state.largest_required_delay_ms:
            state.largest_required_delay_ms = required
            state.delay_ready_mono = max(
                state.delay_ready_mono,
                state.delay_started_mono + required / 1000.0)
            state.phase = "waiting"
            self._metrics["delay_extensions"] += 1
            # An increased delay always needs another newly started check,
            # even if scanner polling happened after the extended deadline.
            return
        evidence.update({
            "candidate_started_mono": state.candidate_started_mono,
            "delay_started_mono": state.delay_started_mono,
            "delay_ready_mono": state.delay_ready_mono,
            "initial_required_delay_ms": state.initial_required_delay_ms,
            "verified_after_delay": True,
        })
        state.evidence = evidence
        state.phase = "ready"
        self._ready_events[state.key] = state.candidate_started_mono
        self._metrics["verified"] += 1

    def tick(self):
        """Poll only completed futures, then submit checks whose delay is due."""
        for future, job in list(self._jobs.items()):
            if not future.done():
                continue
            self._jobs.pop(future, None)
            state = self._states.get(job.key)
            if (state is None or state.generation != job.generation
                    or state.future is not future or self._stopped):
                self._metrics["stale_results"] += 1
                continue
            state.future = None
            now = _clock_now(self.clock)
            if now >= state.candidate_started_mono + self.max_age_seconds:
                self._metrics["expired"] += 1
                self._reject(state, "live_candidato_expirado", now)
                continue
            try:
                result = future.result()  # done() above guarantees no wait.
            except Exception as exc:
                self._reject(state, _public_error(exc), now)
                continue
            self._receive(state, job, result, now)
        if self._stopped:
            return
        now = _clock_now(self.clock)
        for key, state in list(self._states.items()):
            if state.phase == "backoff":
                if now >= state.cooldown_until_mono:
                    self._remove(key)
                continue
            if now >= state.candidate_started_mono + self.max_age_seconds:
                self._metrics["expired"] += 1
                self._reject(state, "live_candidato_expirado", now)
                continue
            if (state.phase == "ready"
                    and now >= state.evidence["valid_until_mono"]):
                self._metrics["expired"] += 1
                self._reject(state, "live_prova_expirada", now)
        # First candidates get their original scanner order. Existing waiting
        # candidates are submitted in candidate age order, without sleeping.
        waiting = sorted((state for state in self._states.values()
                          if state.phase == "waiting"),
                         key=lambda state: (state.candidate_started_mono, state.key))
        for state in waiting:
            if now >= state.delay_ready_mono:
                self._schedule(state, "final", now)

    def consider(self, pair):
        """Return fresh verified evidence once, or a public pending/error token."""
        if self._stopped:
            return None, "live_parado"
        self.tick()
        now = _clock_now(self.clock)
        try:
            saved_pair = deepcopy(pair)
            fingerprint = _fingerprint(saved_pair)
            key = saved_pair["key"]
        except (TypeError, ValueError, OverflowError, RecursionError):
            self._record_failure("live_par_invalido")
            return None, "live_par_invalido"
        if self._catalog is not None:
            if key not in self._catalog:
                self._record_failure("live_par_fora_catalogo")
                return None, "live_par_fora_catalogo"
            if self._catalog[key] != fingerprint:
                self._record_failure("live_par_desatualizado")
                return None, "live_par_desatualizado"
        state = self._states.get(key)
        if state is not None and state.fingerprint != fingerprint:
            self._remove(key)
            self._metrics["rebuild"] += 1
            state = None
        if state is not None:
            if state.phase == "ready":
                # tick checked expiry; sample again after catalog validation so
                # a proof can never be returned after its original deadline.
                if _clock_now(self.clock) >= state.evidence["valid_until_mono"]:
                    self._metrics["expired"] += 1
                    self._reject(state, "live_prova_expirada", _clock_now(self.clock))
                    return None, state.reason
                evidence = state.evidence
                self._remove(key)
                self._metrics["consumed"] += 1
                return evidence, "live_confirmado"
            if state.phase == "backoff":
                return None, state.reason
            if state.phase == "waiting":
                return None, "live_aguarda_atraso"
            return None, "live_em_verificacao"
        if self._slots() >= self.max_pending or len(self._jobs) >= self.max_pending:
            self._metrics["full"] += 1
            self._record_failure("live_fila_cheia")
            return None, "live_fila_cheia"
        self._generation += 1
        state = _Candidate(key, saved_pair, fingerprint, self._generation, now)
        self._states[key] = state
        self._schedule(state, "initial", now)
        return None, state.reason or "live_em_verificacao"

    def take_ready_events(self):
        """Consume each notification once; never renew a proof's validity."""
        self.tick()
        events, self._ready_events = self._ready_events, {}
        return events

    def ready_deadlines(self):
        """Read original ready-proof deadlines before or after taking events."""
        self.tick()
        return {key: state.evidence["valid_until_mono"]
                for key, state in self._states.items() if state.phase == "ready"}

    def discard(self, pairkey):
        """Invalidate metadata/proof and cancel queued work for one direction."""
        removed = self._remove(pairkey)
        if removed:
            self._metrics["discarded"] += 1
        return removed

    def rebuild(self, pairs):
        """Drop orphaned/changed catalog identities, including ready proofs."""
        catalog = {}
        ambiguous = set()
        for pair in pairs:
            try:
                fingerprint = _fingerprint(pair)
                key = pair["key"]
            except (TypeError, ValueError, OverflowError, RecursionError):
                self._record_failure("live_par_invalido")
                continue
            if key in ambiguous:
                continue
            if key in catalog and catalog[key] != fingerprint:
                ambiguous.add(key)
                del catalog[key]
                self._record_failure("live_par_ambiguo")
                continue
            catalog[key] = fingerprint
        self._catalog = catalog
        for key, state in list(self._states.items()):
            if catalog.get(key) != state.fingerprint:
                self._remove(key)
                self._metrics["rebuild"] += 1

    def snapshot(self):
        """Public health counters; confirmed means proof consumed, not a bet."""
        now = _clock_now(self.clock)
        phases = Counter(state.phase for state in self._states.values())
        active = [state for state in self._states.values() if state.phase != "backoff"]
        return {
            "pending": len(self._states), "active": len(active),
            "jobs": len(self._jobs), "orphan_jobs": self._orphan_jobs(),
            "slots": self._slots(), "max_pending": self.max_pending,
            "phases": dict(phases), "ready_events": len(self._ready_events),
            "oldest_age_s": max((max(0.0, now - state.candidate_started_mono)
                                 for state in active), default=None),
            "confirmadas": self._metrics["consumed"],
            "rejeitadas": self._metrics["rejected"],
            "ultima_falha": self._last_failure,
            "falhas": dict(self._failures), "metrics": dict(self._metrics),
            "stopped": self._stopped,
        }

    def stop(self):
        """Stop new work immediately; never wait for a running HTTP request."""
        if self._stopped:
            return
        self._stopped = True
        for key in list(self._states):
            self._remove(key)
        for future in list(self._jobs):
            self._cancel(future)
        self._ready_events.clear()
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
