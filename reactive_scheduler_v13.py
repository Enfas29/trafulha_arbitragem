"""Fair, bounded scheduling of market identifiers; never caches prices.

The scanner owns this object and calls it between individual market reads.
WebSocket callbacks keep using their existing thread-safe hint buffers.  A
dispatch consumes only the event known at that moment; ``completed`` never
removes a newer event received while that market was being processed.
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict


class ReactiveScheduler:
    def __init__(self, pairs=(), max_hot_burst=4, normal_interval=0.8,
                 min_key_interval=0.25, ttl_seconds=20, max_pending=256,
                 priority=None):
        self.max_hot_burst = max(1, int(max_hot_burst))
        self.normal_interval = self._nonnegative(normal_interval)
        self.min_key_interval = self._nonnegative(min_key_interval)
        self.ttl_seconds = self._nonnegative(ttl_seconds)
        self.max_pending = max(1, int(max_pending))
        # Optional urgency is scheduling metadata, never evidence that a game
        # is live or a quote is safe to trade. Smaller integers go first.
        self.priority = priority
        self.pending = {}  # key -> (first local receipt, set of sources)
        # A finished live check is actionable for its own short proof lifetime.
        # Its original timestamp still includes the official betting delay.
        self._ready_expiries = {}
        self.metrics = Counter(dict(ttl=0, overflow=0, unknown=0,
                                    invalid=0, rebuild=0))
        self.pairs = []
        self.cursor = 0
        self.by_key = {}
        self.by_poly = {}
        self.by_sx = {}
        self._last_completed = {}
        self._last_dispatched = {}
        self._inflight = set()
        self._hot_burst = 0
        self._next_normal = None  # Rotation is due on the first choose.
        self.rebuild(pairs)

    @staticmethod
    def _nonnegative(value):
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Scheduler intervals must be finite and nonnegative")
        return value

    @staticmethod
    def _now(now):
        value = time.monotonic() if now is None else float(now)
        if not math.isfinite(value):
            raise ValueError("Scheduler clock must be finite")
        return value

    def rebuild(self, pairs, now=None):
        """Replace the catalog atomically, preserving valid pending work."""
        now = self._now(now)
        next_key = (self.pairs[self.cursor % len(self.pairs)]["key"]
                    if self.pairs else None)
        unique = {}
        poly, sx = defaultdict(set), defaultdict(set)
        for pair in pairs:
            try:
                key = pair["key"]
                asset = str(pair["poly_asset"])
                market = str(pair["sx"]["hash"])
                if not isinstance(key, str) or not key:
                    raise ValueError("Missing market key")
            except (KeyError, TypeError, ValueError):
                self.metrics["invalid"] += 1
                continue
            if key in unique:
                continue
            unique[key] = pair
            poly[asset].add(key)
            sx[market].add(key)
        self.by_key = unique
        self.pairs = list(unique.values())
        self.by_poly, self.by_sx = dict(poly), dict(sx)
        keys = set(unique)
        removed = set(self.pending) - keys
        self.metrics["rebuild"] += len(removed)
        self.pending = {key: event for key, event in self.pending.items()
                        if key in keys}
        self._ready_expiries = {key: value for key, value in self._ready_expiries.items()
                               if key in self.pending}
        self._last_completed = {key: value for key, value in self._last_completed.items()
                                if key in keys}
        self._last_dispatched = {key: value for key, value in self._last_dispatched.items()
                                 if key in keys}
        self._inflight.intersection_update(keys)
        if next_key in keys:
            self.cursor = list(unique).index(next_key)
        else:
            self.cursor = self.cursor % len(self.pairs) if self.pairs else 0
        self._expire(now)

    def _expire(self, now):
        expired = [key for key, (stamp, _) in self.pending.items()
                   if (now > self._ready_expiries[key] if key in self._ready_expiries
                       else now - stamp > self.ttl_seconds)]
        self._count_ttl(expired)
        for key in expired:
            del self.pending[key]
            self._ready_expiries.pop(key, None)

    def _count_ttl(self, keys):
        """Count real drops by current urgency, without replacing total TTL."""
        keys = tuple(keys)
        self.metrics["ttl"] += len(keys)
        if self.priority is not None:
            for key in keys:
                rank = self._priority(self.by_key[key])
                group = str(rank) if rank in (0, 1, 2) else "other"
                self.metrics["ttl_priority_" + group] += 1

    def ingest(self, poly_events=None, sx_events=None, now=None):
        """Coalesce identifier hints, retaining the first unprocessed receipt.

        ``ttl``/``overflow``/``rebuild`` count discarded direction hints;
        ``unknown``/``invalid`` count rejected incoming identifiers.
        """
        now = self._now(now)
        self._expire(now)
        added = 0
        for source, events, index in (("POLY", poly_events, self.by_poly),
                                      ("SX", sx_events, self.by_sx)):
            if events is None:
                continue
            if not hasattr(events, "items"):
                self.metrics["invalid"] += 1
                continue
            for identifier, raw_stamp in events.items():
                try:
                    if isinstance(raw_stamp, bool):
                        raise ValueError("Boolean timestamp")
                    stamp = float(raw_stamp)
                    if not math.isfinite(stamp) or stamp > now:
                        raise ValueError("Invalid receipt timestamp")
                except (TypeError, ValueError, OverflowError):
                    self.metrics["invalid"] += 1
                    continue
                keys = index.get(str(identifier))
                if not keys:
                    self.metrics["unknown"] += 1
                    continue
                if now - stamp > self.ttl_seconds:
                    self._count_ttl(keys)
                    continue
                for key in sorted(keys):
                    previous = self.pending.get(key)
                    if previous is None:
                        self.pending[key] = (stamp, {source})
                        added += 1
                    else:
                        old_stamp, old_sources = previous
                        self.pending[key] = (min(old_stamp, stamp),
                                             old_sources | {source})
        self._enforce_cap()
        return added

    def _enforce_cap(self):
        excess = len(self.pending) - self.max_pending
        if excess > 0:
            # Legacy FIFO keeps recent hints. With urgency, remove background
            # before near/current games, and preserve ready LIVE proofs until
            # no ordinary hint remains. Keep the same receipt-based tie break.
            def eviction_order(key):
                stamp = self.pending[key][0]
                if self.priority is None:
                    return stamp, key
                if key in self._ready_expiries:
                    return 1, 0, stamp, key
                return 0, -self._priority(self.by_key[key]), stamp, key
            oldest = sorted(self.pending, key=eviction_order)
            for key in oldest[:excess]:
                del self.pending[key]
                self._ready_expiries.pop(key, None)
            self.metrics["overflow"] += excess

    def ingest_ready(self, events, deadlines, now=None):
        """Wake a live candidate after its delay without hiding that wait.

        Only identifiers are queued. The consumer must revalidate the proof,
        both order books and the wallet guards before any virtual debit.
        """
        now = self._now(now)
        self._expire(now)
        if not hasattr(events, "items") or not hasattr(deadlines, "get"):
            self.metrics["invalid"] += 1
            return 0
        added = 0
        for key, raw_stamp in events.items():
            try:
                if isinstance(raw_stamp, bool):
                    raise ValueError("Boolean timestamp")
                stamp = float(raw_stamp)
                if not math.isfinite(stamp) or not 0 <= now - stamp <= 60:
                    raise ValueError("Invalid candidate timestamp")
                raw_deadline = deadlines.get(key)
                if type(raw_deadline) not in (int, float):
                    raise ValueError("Invalid proof deadline")
                deadline = float(raw_deadline)
                if not math.isfinite(deadline) or deadline > now + 3.0:
                    raise ValueError("Invalid proof deadline")
            except (TypeError, ValueError, OverflowError):
                self.metrics["invalid"] += 1
                continue
            if key not in self.by_key:
                self.metrics["unknown"] += 1
                continue
            if deadline <= now:
                self._count_ttl((key,))
                continue
            previous = self.pending.get(key)
            if previous is None:
                self.pending[key] = (stamp, {"LIVE"})
                added += 1
            else:
                old_stamp, old_sources = previous
                self.pending[key] = (min(old_stamp, stamp), old_sources | {"LIVE"})
            self._ready_expiries[key] = min(
                self._ready_expiries.get(key, deadline), deadline
            )
        self._enforce_cap()
        return added

    def _eligible(self, key, now):
        last = self._last_completed.get(key)
        return (key not in self._inflight
                and (last is None or now >= last + self.min_key_interval))

    def _priority(self, pair):
        if self.priority is None:
            return 0
        try:
            value = self.priority(pair)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid scheduling priority")
            return value
        except Exception:
            # A malformed candidate or failed classifier must not stop the
            # scanner or elevate an unknown game above current live work.
            # Keep the reason count; never log an arbitrary callback payload.
            self.metrics["priority_error"] += 1
            return 2

    def _rotation_candidate(self, now):
        first_hot = None
        for step in range(len(self.pairs)):
            index = (self.cursor + step) % len(self.pairs)
            pair = self.pairs[index]
            key = pair["key"]
            if not self._eligible(key, now):
                continue
            if self.priority is not None:
                # The reserved rotation must also visit background games
                # whose continuous hints never win the urgency queue. A
                # preference for hint-free games could starve them forever.
                return index, pair
            if key not in self.pending:
                return index, pair
            if first_hot is None:
                first_hot = (index, pair)
        return first_hot

    def _dispatch(self, pair, kind, now, rotation_index=None):
        key = pair["key"]
        event = self.pending.pop(key, None)
        self._ready_expiries.pop(key, None)
        self._inflight.add(key)
        self._last_dispatched[key] = now
        if kind == "rotation":
            self.cursor = (rotation_index + 1) % len(self.pairs)
            self._next_normal = now + self.normal_interval
            self._hot_burst = 0
        else:
            self._hot_burst = min(self.max_hot_burst, self._hot_burst + 1)
        return pair, event, kind

    def choose(self, now=None):
        """Dispatch by urgency then FIFO, preserving normal coverage.

        Ready LIVE proofs retain their existing deadline priority and the
        reserved rotation still runs at the same burst/interval boundaries.
        Without an urgency callback this is the original FIFO policy.
        """
        now = self._now(now)
        self._expire(now)
        if not self.pairs:
            return None
        live = next((self.by_key[key] for key in sorted(
                        self._ready_expiries,
                        key=lambda key: (self._ready_expiries[key], key))
                     if self._eligible(key, now)), None)
        hot = live
        if hot is None:
            # Evaluate urgency once per eligible pending direction, in O(n).
            # Do not cache ranks: a fixture can start between catalog refreshes.
            key = min((key for key in self.pending if self._eligible(key, now)),
                      key=lambda key: (self._priority(self.by_key[key]),
                                       self.pending[key][0], key), default=None)
            hot = self.by_key[key] if key is not None else None
        normal_due = self._next_normal is None or now >= self._next_normal
        # Spare capacity belongs to rotation. The normal interval controls its
        # reserved quota only while eligible WS work is competing for service.
        if hot is None or (normal_due and self._hot_burst >= self.max_hot_burst):
            normal = self._rotation_candidate(now)
            if normal is not None:
                index, pair = normal
                return self._dispatch(pair, "rotation", now, index)
        if hot is not None:
            return self._dispatch(hot, "live" if live is not None else "ws", now)
        return None

    def completed(self, key, now=None):
        """Mark processing finished without consuming a newer WS hint."""
        now = self._now(now)
        self._inflight.discard(key)
        if key in self.by_key:
            self._last_completed[key] = now

    def idle_seconds(self, now=None, max_wait=0.05):
        """Bound idle polling while respecting cooldowns, without busy spins."""
        now = self._now(now)
        max_wait = self._nonnegative(max_wait)
        self._expire(now)
        waits = []
        for key in self.by_key:
            if key in self._inflight:
                continue
            last = self._last_completed.get(key)
            waits.append(0.0 if last is None else max(
                0.0, last + self.min_key_interval - now))
        return min(max_wait, min(waits)) if waits else max_wait

    def coverage(self, now=None):
        """Coverage is dispatch age, not quote freshness or API success.

        The maximum covers visited directions only; ``unvisited`` explicitly
        exposes newly discovered directions that have not been scanned yet.
        """
        now = self._now(now)
        ages = [max(0.0, now - stamp) for stamp in self._last_dispatched.values()]
        return {"directions": len(self.pairs), "visited": len(ages),
                "unvisited": len(self.pairs) - len(ages),
                "max_dispatch_age_s": max(ages) if ages else None}
