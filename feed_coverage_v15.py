"""Bounded identifier selection for read-only websocket hints.

No quotes, account information or execution state are stored here. Catalogues
within the limit are fully selected; larger catalogues rotate on each refresh.
"""
from __future__ import annotations

import os


def read_limit(name, default, maximum):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} deve ser um inteiro entre 1 e {maximum}") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} deve estar entre 1 e {maximum}")
    return value


def select_identifiers(items, limit, cursor=0):
    """Return (selected, next cursor, unique candidate count)."""
    if type(limit) is not int or limit < 1:
        raise ValueError("Limite de cobertura inválido")
    pool = tuple(dict.fromkeys(str(item) for item in items if item))
    if len(pool) <= limit:
        return pool, 0, len(pool)
    start = int(cursor) % len(pool)
    selected = tuple(pool[(start + i) % len(pool)] for i in range(limit))
    return selected, (start + limit) % len(pool), len(pool)
