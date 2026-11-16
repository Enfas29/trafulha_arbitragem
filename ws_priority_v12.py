"""Prioridade de eventos WS com cache apenas de *identificadores e tempos*.

Nunca guarda odds, não consulta APIs, não executa nem simula ordens.
A leitura REST fresca continua obrigatória no monitor para validar preço.
"""
from __future__ import annotations

import time
from collections import defaultdict


class PriorityCache:
    """Fila de eventos curtos por chave de direção, coalescidos por confronto."""

    def __init__(self, max_priority=4, ttl_seconds=20.0, max_pending=256):
        self.max_priority = max(1, int(max_priority))
        self.ttl_seconds = float(ttl_seconds)
        self.max_pending = max(8, int(max_pending))
        self.pending = {}  # chave -> (primeiro timestamp monotónico, fontes set)
        self.by_poly = {}
        self.by_sx = {}
        self.keys = set()

    def rebuild(self, pairs):
        by_poly = defaultdict(set)
        by_sx = defaultdict(set)
        keys = set()
        for p in pairs:
            key = p["key"]
            keys.add(key)
            by_poly[str(p["poly_asset"])].add(key)
            by_sx[str(p["sx"]["hash"])].add(key)
        self.by_poly = dict(by_poly)
        self.by_sx = dict(by_sx)
        self.keys = keys
        # Não transportar sinais para direções que deixaram de existir.
        self.pending = {k: v for k, v in self.pending.items() if k in keys}

    def expire(self, now=None):
        now = time.monotonic() if now is None else float(now)
        self.pending = {
            key: item for key, item in self.pending.items()
            if 0 <= now - item[0] <= self.ttl_seconds
            and key in self.keys
        }

    def ingest(self, poly_events=None, sx_events=None, now=None):
        """recebe {'asset/hash': timestamp monotónico}; não contém preços."""
        now = time.monotonic() if now is None else float(now)
        self.expire(now)
        added = 0
        for source, events, index in (
            ("POLY", poly_events or {}, self.by_poly),
            ("SX", sx_events or {}, self.by_sx),
        ):
            for token, timestamp in events.items():
                try:
                    timestamp = float(timestamp)
                except (TypeError, ValueError):
                    continue
                if not (0 <= now - timestamp <= self.ttl_seconds):
                    continue
                for key in index.get(str(token), ()):
                    if key not in self.pending:
                        self.pending[key] = (timestamp, {source})
                        added += 1
                    else:
                        old_time, sources = self.pending[key]
                        sources.add(source)
                        self.pending[key] = (min(old_time, timestamp), sources)
        if len(self.pending) > self.max_pending:
            # Descartar sinais mais antigos; nunca guardar uma fila ilimitada.
            oldest = sorted(self.pending, key=lambda k: self.pending[k][0])
            for key in oldest[:len(self.pending) - self.max_pending]:
                del self.pending[key]
        return added

    def choose(self, pairs, offset, limit, now=None):
        """Retorna (direções, novo_cursor, eventos_associados).

        Reserva sempre pelo menos uma posição para rotação normal;
        direções com WS pendente têm prioridade e não se repetem.
        """
        now = time.monotonic() if now is None else float(now)
        self.expire(now)
        if not pairs:
            return [], 0, {}
        capacity = min(len(pairs), max(1, int(limit)))
        by_key = {p["key"]: p for p in pairs}
        max_hot = min(self.max_priority, capacity - 1) if capacity > 1 else 0
        hot_keys = [
            key for key in sorted(self.pending, key=lambda k: (self.pending[k][0], k))
            if key in by_key
        ][:max_hot]
        chosen = [by_key[key] for key in hot_keys]
        selected = set(hot_keys)
        steps = 0
        normal_needed = capacity - len(chosen)
        normal = []
        while len(normal) < normal_needed and steps < len(pairs):
            pair = pairs[(offset + steps) % len(pairs)]
            steps += 1
            if pair["key"] not in selected:
                normal.append(pair)
                selected.add(pair["key"])
        chosen.extend(normal)
        events = {
            key: self.pending.pop(key)
            for key in selected if key in self.pending
        }
        return chosen, (offset + steps) % len(pairs), events


def self_test():
    pairs = [{
        "key": str(i), "poly_asset": "poly" + str(i),
        "sx": {"hash": "sx" + str(i)},
    } for i in range(15)]
    cache = PriorityCache(max_priority=3, ttl_seconds=10.0, max_pending=32)
    cache.rebuild(pairs)
    cache.ingest({"poly10": 99.1}, {"sx11": 99.2}, now=100.0)
    cache.ingest({"poly10": 99.8}, {"sx10": 99.9}, now=100.0)
    assert cache.pending["10"][0] == 99.1
    assert cache.pending["10"][1] == {"POLY", "SX"}
    assert cache.pending["11"][1] == {"SX"}
    chosen, cursor, events = cache.choose(pairs, 0, 5, now=100.0)
    assert [p["key"] for p in chosen][:2] == ["10", "11"]
    assert [p["key"] for p in chosen][2:] == ["0", "1", "2"]
    assert cursor == 3 and len(chosen) == len(set(p["key"] for p in chosen))
    assert events["10"] == (99.1, {"POLY", "SX"})
    assert "10" not in cache.pending
    chosen2, cursor2, _ = cache.choose(pairs, cursor, 5, now=100.1)
    assert [p["key"] for p in chosen2] == ["3", "4", "5", "6", "7"]
    assert cursor2 == 8
    cache.ingest({"poly12": 90.0, "poly13": 100.2}, now=100.3)
    assert "12" not in cache.pending and "13" in cache.pending
    cache.expire(now=120.0)
    assert not cache.pending
    # Um único pedido não deve bloquear a rotação normal.
    chosen3, _, _ = cache.choose(pairs, 0, 1, now=120.0)
    assert chosen3[0]["key"] == "0"
    cache.ingest({"poly2": 120.1}, now=120.2)
    cache.rebuild([p for p in pairs if p["key"] != "2"])
    assert "2" not in cache.pending
    print("✅ WS Priority Cache: fila, TTL, coalescência e rotação OK")


if __name__ == "__main__":
    self_test()
