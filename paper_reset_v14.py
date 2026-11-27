"""One explicit virtual bankroll reset, before the scanner/workers start.

PAPER_RESET_REQUEST_ID is a unique request, not a boolean toggle. A committed
request is never applied again, including after a restart or another reset.
Old trades and notices remain in the database, with their original rows in
an archive. No real wallet, platform order or transfer is involved.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

import paper_v12 as paper


LOG = logging.getLogger("paper_reset_v14")
RESET_CENTS = 100_000
ARCHIVED_STATUS = "RESET_ARCHIVED"
REQUEST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,95}\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _previous_reset(conn, request_id):
    if "paper_v14_resets" not in _tables(conn):
        return None
    row = conn.execute(
        "SELECT initial_cents,result_json FROM paper_v14_resets WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    if row[0] != RESET_CENTS:
        raise ValueError("Pedido de reset já usado com outro saldo inicial")
    result = json.loads(row[1])
    if (not isinstance(result, dict) or result.get("request_id") != request_id
            or result.get("balances_after_cents") != {"poly": RESET_CENTS, "sx": RESET_CENTS}):
        raise ValueError("Registo de reset inconsistente; saldos preservados")
    return {**result, "applied": False}


def _backup_locked_database(conn):
    """Read the committed snapshot through a separate connection under lock.

    SQLite backup on the *writer* holding BEGIN IMMEDIATE can remain BUSY.
    The read-only connection sees the exact pre-reset state while that writer
    excludes changes. No reset mutation has happened when this is called.
    """
    path = next((row[2] for row in conn.execute("PRAGMA database_list")
                 if row[1] == "main"), "")
    if not path:
        raise ValueError("Reset exige uma base persistente com backup verificável")
    source_path = Path(path).resolve()
    reader = sqlite3.connect("file:" + quote(str(source_path), safe="/") + "?mode=ro",
                             uri=True, timeout=20)
    try:
        backup_path = Path(paper._backup_before_migration(reader, source_path))
    finally:
        reader.close()
    target = backup_path.with_name(backup_path.name.replace(
        f"-pre-v{paper.SCHEMA_VERSION}-", "-pre-reset-", 1))
    if target != backup_path:
        os.replace(backup_path, target)
    return str(target)


def reset_virtual_bankroll(conn, request_id, *, now_epoch=None):
    """Archive the current cycle and set both fictitious wallets to 1,000 EUR.

    Call at process startup before workers. Any backup, archive or balance
    failure rolls back the entire reset. A successful backup may remain after
    rollback, but the request is not marked completed until the same commit
    that sets both balances and archives the old trades.
    """
    if not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id):
        raise ValueError("PAPER_RESET_REQUEST_ID inválido; usar um identificador único de 8 a 96 caracteres")
    if conn.in_transaction:
        raise ValueError("Reset só pode começar sem outra transação em curso")
    stamp = time.time() if now_epoch is None else now_epoch
    if type(stamp) not in (int, float) or not 0 < stamp < 100_000_000_000:
        raise ValueError("Instante de reset inválido")
    previous = _previous_reset(conn, request_id)
    if previous is not None:
        return previous
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Two simultaneous startup attempts must still apply this request once.
        previous = _previous_reset(conn, request_id)
        if previous is not None:
            conn.rollback()
            return previous
        before = paper.free_balances(conn)
        if (set(before) != {"poly", "sx"}
                or any(type(value) is not int or value < 0 for value in before.values())):
            raise ValueError("Carteiras virtuais inválidas; reset não aplicado")
        backup_path = _backup_locked_database(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_v14_resets (
                request_id TEXT PRIMARY KEY,
                applied_epoch INTEGER NOT NULL,
                initial_cents INTEGER NOT NULL,
                backup_path TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_v14_reset_trades (
                request_id TEXT NOT NULL REFERENCES paper_v14_resets(request_id),
                trade_id INTEGER NOT NULL REFERENCES paper_v12_trades(id),
                original_status TEXT NOT NULL,
                original_event_key TEXT NOT NULL,
                original_row_json TEXT NOT NULL,
                PRIMARY KEY (request_id, trade_id)
            )
        """)
        cursor = conn.execute(
            "SELECT * FROM paper_v12_trades WHERE status<>? ORDER BY id",
            (ARCHIVED_STATUS,),
        )
        names = [item[0] for item in cursor.description]
        records = [dict(zip(names, row)) for row in cursor.fetchall()]
        open_count = sum(row["status"] in {"OPEN_CONDITIONAL", "REVIEW_REQUIRED"}
                         for row in records)
        after = {"poly": RESET_CENTS, "sx": RESET_CENTS}
        result = {
            "request_id": request_id, "applied": True,
            "applied_epoch": int(stamp), "initial_cents": RESET_CENTS,
            "balances_before_cents": before, "balances_after_cents": after,
            "archived_trades": len(records), "cancelled_open_trades": open_count,
            "backup_path": backup_path,
        }
        conn.execute(
            "INSERT INTO paper_v14_resets(request_id,applied_epoch,initial_cents,backup_path,result_json) "
            "VALUES(?,?,?,?,?)",
            (request_id, int(stamp), RESET_CENTS, backup_path, _json(result)),
        )
        for row in records:
            conn.execute(
                "INSERT INTO paper_v14_reset_trades(request_id,trade_id,original_status,"
                "original_event_key,original_row_json) VALUES(?,?,?,?,?)",
                (request_id, row["id"], row["status"], row["event_key"], _json(row)),
            )
            changed = conn.execute(
                "UPDATE paper_v12_trades SET status=?,event_key=? WHERE id=? AND status=?",
                (ARCHIVED_STATUS, f"reset:{request_id}:{row['id']}", row["id"], row["status"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Operação alterada durante o reset; rollback aplicado")
        changed = conn.execute(
            "UPDATE paper_v12_wallets SET free_cents=? WHERE platform IN ('poly','sx')",
            (RESET_CENTS,),
        ).rowcount
        if changed != 2 or paper.free_balances(conn) != after:
            raise RuntimeError("As duas carteiras não foram repostas; rollback aplicado")
        if paper.open_exposure(conn) != 0:
            raise RuntimeError("Exposição antiga ainda ativa; rollback aplicado")
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def apply_requested_reset(conn, environ=None):
    """No request means no reset. Leaving the same request set is harmless."""
    source = os.environ if environ is None else environ
    request_id = source.get("PAPER_RESET_REQUEST_ID", "").strip()
    if not request_id:
        return None
    result = reset_virtual_bankroll(conn, request_id)
    LOG.info("PAPER_RESET | %s | exclusivamente_virtual", _json(result))
    return result
