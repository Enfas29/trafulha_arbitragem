"""Bounded, thread-safe latency measurements without I/O or identifiers.

All durations are milliseconds. ``poly`` and ``sx`` measure completed source
calls, including failed calls whose duration is known. ``queue``, ``rest`` and
``decision`` measure local processing stages; they do not imply an order fill.

``n`` counts every valid measurement in the snapshot window. Quantiles use the
last ``max_samples`` measurements (the returned ``samples`` count), with linear
interpolation at ``(samples - 1) * percentile``. ``max_ms`` covers the entire
window, including values evicted from the bounded buffer. Invalid durations
never enter the quantiles; call/error counters still record the attempt.

The class makes no network requests, writes no logs and retains no market,
account, token, URL or message identifiers. Snapshot times use the supplied
monotonic clock, not calendar time.
"""

from collections import deque
import math
import threading
import time


_METRICS = ("poly", "sx", "queue", "rest", "decision")
_SOURCE_ALIASES = {
    "poly": "poly",
    "polymarket": "poly",
    "sx": "sx",
    "sxbet": "sx",
    "sx_bet": "sx",
    "sx bet": "sx",
}


def _source_name(value):
    if not isinstance(value, str):
        return None
    return _SOURCE_ALIASES.get(value.strip().lower())


def _duration(value):
    # Strings and bools commonly indicate instrumentation mistakes.
    if isinstance(value, (str, bytes, bool)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _quantile(values, fraction):
    if not values:
        return None
    rank = (len(values) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


class _Samples:
    __slots__ = ("values", "n", "invalid", "maximum")

    def __init__(self, max_samples):
        self.values = deque(maxlen=max_samples)
        self.n = 0
        self.invalid = 0
        self.maximum = None

    def record(self, value):
        if value is None:
            self.invalid += 1
            return
        self.values.append(value)
        self.n += 1
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def capture(self):
        return self.n, self.invalid, self.maximum, list(self.values)


class LatencyMetrics:
    """Collect measurements with atomic snapshot/reset boundaries.

    Source names are case-insensitive and restricted to Polymarket/SX aliases.
    Unknown sources are ignored, without retaining their names.

    ``record_source`` returns whether the source was recognised. ``errors``
    counts attempts with false ``ok`` or an integer HTTP status >= 400;
    ``http_429`` counts integer HTTP 429 statuses and is a subset of errors.
    A recognised call increments ``calls`` even with an invalid duration.

    ``record_decision`` validates its three durations independently. Sources
    attribute the triggering event only: each recognised source is counted at
    most once in ``decision_sources`` per call. They do not produce additional
    source latency samples. Unknown labels are discarded.
    """

    def __init__(self, clock=time.monotonic, max_samples=4096):
        if (isinstance(max_samples, bool) or not isinstance(max_samples, int)
                or max_samples < 1):
            raise ValueError("max_samples must be a positive integer")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._max_samples = max_samples
        self._lock = threading.Lock()
        self._started = self._clock()
        self._reset_state()

    def _reset_state(self):
        self._metrics = {name: _Samples(self._max_samples) for name in _METRICS}
        self._source_counts = {
            source: {"calls": 0, "errors": 0, "http_429": 0}
            for source in ("poly", "sx")
        }
        self._decision_calls = 0
        self._decision_sources = {"poly": 0, "sx": 0}

    def record_source(self, source, elapsed_ms, ok=True, status_code=None):
        source = _source_name(source)
        if source is None:
            return False
        duration = _duration(elapsed_ms)
        http_status = (status_code if isinstance(status_code, int)
                       and not isinstance(status_code, bool) else None)
        error = not ok or (http_status is not None and http_status >= 400)
        with self._lock:
            counts = self._source_counts[source]
            counts["calls"] += 1
            counts["errors"] += int(bool(error))
            counts["http_429"] += int(http_status == 429)
            self._metrics[source].record(duration)
        return True

    def record_decision(self, queue_ms, rest_ms, decision_ms, sources=()):
        values = tuple(_duration(value) for value in (queue_ms, rest_ms, decision_ms))
        if isinstance(sources, str):
            sources = (sources,)
        recognised = {_source_name(source) for source in (sources or ())}
        recognised.discard(None)
        with self._lock:
            self._decision_calls += 1
            for name, value in zip(("queue", "rest", "decision"), values):
                self._metrics[name].record(value)
            for source in recognised:
                self._decision_sources[source] += 1

    def snapshot(self, reset=True):
        """Return JSON-safe statistics; optionally begin a new atomic window.

        Missing quantiles/maxima are ``None``. ``snapshot(reset=False)`` is
        observational and does not consume any counters or retained samples.
        Computation of quantiles occurs outside the recording lock.
        """
        with self._lock:
            ended = self._clock()
            started = self._started
            captured = {name: values.capture() for name, values in self._metrics.items()}
            source_counts = {name: counts.copy() for name, counts in self._source_counts.items()}
            decision_calls = self._decision_calls
            decision_sources = self._decision_sources.copy()
            if reset:
                self._reset_state()
                self._started = ended
        result = {
            "window": {
                "start_monotonic": started,
                "end_monotonic": ended,
                "seconds": max(0.0, ended - started),
            },
            "max_samples": self._max_samples,
            "decision_calls": decision_calls,
            "decision_sources": decision_sources,
        }
        for name, (count, invalid, maximum, values) in captured.items():
            values.sort()
            result[name] = {
                "n": count,
                "samples": len(values),
                "invalid": invalid,
                "p50_ms": _quantile(values, 0.50),
                "p95_ms": _quantile(values, 0.95),
                "max_ms": maximum,
            }
            if name in source_counts:
                result[name].update(source_counts[name])
        return result
