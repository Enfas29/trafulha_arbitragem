"""Bounded concurrent public book reads; accounting stays on the scanner thread.

A result belongs to one dispatch and one catalog generation. It is consumed
once, never reused as an odds cache, and must pass the normal freshness checks.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(frozen=True)
class BookRead:
    item: tuple
    generation: int
    started_mono: float
    finished_mono: float
    books: object = None
    error: Exception | None = None


class ParallelBooks:
    def __init__(self, reader, max_workers=3, *, executor=None, clock=time.monotonic):
        self.reader = reader
        self.max_workers = max(1, min(4, int(max_workers)))
        self.clock = clock
        self.executor = executor or ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="books-v15")
        self._owns_executor = executor is None
        self.jobs = {}
        self.closed = False

    @property
    def available(self):
        return 0 if self.closed else self.max_workers - len(self.jobs)

    @property
    def pending(self):
        return len(self.jobs)

    def _read(self, item, generation):
        started = self.clock()
        try:
            books = self.reader(item[0])
            return BookRead(item, generation, started, self.clock(), books=books)
        except Exception as exc:
            return BookRead(item, generation, started, self.clock(), error=exc)

    def submit(self, item, generation):
        key = item[0]["key"]
        # No executor backlog, and no two reads for the same direction.
        if self.available <= 0 or any(k == key for k, _ in self.jobs.values()):
            return False
        future = self.executor.submit(self._read, item, generation)
        self.jobs[future] = (key, generation)
        return True

    def poll(self):
        completed = []
        for future in list(self.jobs):
            if future.done():
                del self.jobs[future]
                if not future.cancelled():
                    completed.append(future.result())
        # Process the freshest available responses without blocking on a
        # slower market. LIVE proof expiry gets priority among finished reads.
        return sorted(completed, key=lambda r: (
            0 if r.item[2] == "live" else 1, r.finished_mono))

    def stop(self):
        self.closed = True
        for future in self.jobs:
            future.cancel()
        if self._owns_executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
