"""Decision evidence, bounded history and wallet isolation; no live APIs."""
import json
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import decision_audit_v15 as audit


BASE = 1_795_800_000


def pair(number=1):
    return {
        "key": f"poly-{number}:sx-{number}:0",
        "poly": {"id": f"poly-{number}", "title": "Team A vs Team B"},
        "sx": {"hash": f"sx-{number}", "sport": "tennis",
               "teams": ("Team A", "Team B"), "start": BASE + 7200},
        "poly_label": "Team A", "sx_label": "Team B",
        # The complete pair must never be serialized as decision evidence.
        "request": {"headers": {"x-sx-api-key": "NEVER_STORE_THIS_KEY"}},
    }


class DecisionAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "monitor.sqlite3"
        self.conn = sqlite3.connect(self.path, timeout=20)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE wallets(platform TEXT PRIMARY KEY, balance INTEGER)")
        self.conn.execute("INSERT INTO wallets VALUES('poly',100000),('sx',99999)")
        self.conn.commit()
        audit.create_schema(self.conn)

    def record(self, number=1, at=0, roi=-1, reason="abaixo_margem", **kwargs):
        return audit.record_decision(
            self.conn, pair(number), "PRE_JOGO", roi, reason,
            now_epoch=BASE + at, **kwargs,
        )

    def rows(self):
        cursor = self.conn.execute(f"SELECT * FROM {audit.TABLE} ORDER BY id")
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]

    def test_schema_and_evidence_never_reset_wallets_or_existing_data(self):
        before = self.conn.execute("SELECT * FROM wallets ORDER BY platform").fetchall()
        audit.create_schema(self.conn)
        self.record(roi=8.08, reason="recheck_price_changed", details={"first_roi_pct": 8.08})
        audit.prune_decisions(self.conn, now_epoch=BASE + 100000)
        self.assertEqual(self.conn.execute("SELECT * FROM wallets ORDER BY platform").fetchall(), before)
        self.assertEqual(self.rows(), [])

    def test_pending_transaction_is_not_committed_or_rolled_back_by_audit(self):
        self.conn.execute("UPDATE wallets SET balance=7 WHERE platform='poly'")
        with self.assertRaises(ValueError):
            self.record()
        self.assertTrue(self.conn.in_transaction)
        with self.assertRaises(ValueError):
            audit.create_schema(self.conn)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT balance FROM wallets WHERE platform='poly'").fetchone()[0], 100000)

    def test_positive_decision_preserves_observed_reason_and_sizing(self):
        detail = {"first_roi_pct": 8.08, "final_roi_pct": .32,
                  "stake_eur": 27.53, "poly_stake_eur": 10.11,
                  "sx_stake_eur": 17.42, "net_profit_eur": .09,
                  "failure_stage": "recheck_margin_lost", "quote_age_ms": 19.5}
        self.assertTrue(self.record(roi=8.08, reason="validacao_sem_registo", details=detail))
        row = self.rows()[0]
        self.assertEqual(row["event"], "Team A vs Team B")
        self.assertEqual(row["poly_outcome"], "Team A")
        self.assertEqual(row["sx_outcome"], "Team B")
        self.assertEqual(row["game_start_epoch"], BASE + 7200)
        self.assertEqual(row["indicative_roi_pct"], 8.08)
        self.assertEqual(row["reason"], "validacao_sem_registo")
        self.assertEqual(json.loads(row["details_json"]), detail)
        self.assertIn("+00:00", row["observed_at"])

    def test_negative_sampling_is_once_per_minute_per_pair_reason(self):
        self.assertTrue(self.record(at=0))
        self.assertFalse(self.record(at=59))
        self.assertTrue(self.record(at=1, reason="sem_cotacao_valida"))
        self.assertFalse(self.record(at=2))
        self.assertTrue(self.record(number=2, at=2))
        self.assertTrue(self.record(at=60))
        self.assertEqual(len(self.rows()), 4)

    def test_positive_sampling_is_15s_but_new_reason_is_immediate(self):
        self.assertTrue(self.record(at=0, roi=1, reason="recheck_price_changed"))
        self.assertFalse(self.record(at=14.99, roi=2, reason="recheck_price_changed"))
        self.assertTrue(self.record(at=15, roi=1, reason="recheck_price_changed"))
        self.assertTrue(self.record(at=16, roi=1, reason="recheck_margin_lost"))
        self.assertTrue(self.record(at=17, roi=1, reason="recheck_price_changed"))
        self.assertEqual(len(self.rows()), 4)

    def test_positive_transition_is_retained_after_negative_same_reason(self):
        self.assertTrue(self.record(at=0, roi=-.1, reason="validacao_sem_registo"))
        self.assertTrue(self.record(at=1, roi=.1, reason="validacao_sem_registo"))
        self.assertEqual([row["importance"] for row in self.rows()], [0, 1])

    def test_stage_transition_is_retained_for_positive_signal(self):
        self.record(roi=1, reason="validacao_sem_registo")
        self.assertTrue(audit.record_decision(
            self.conn, pair(), "APOS_INICIO_NAO_CONFIRMADO", 1,
            "validacao_sem_registo", now_epoch=BASE + 1,
        ))
        self.assertEqual(len(self.rows()), 2)

    def test_live_waiting_does_not_flood_even_for_positive_margin(self):
        self.assertTrue(self.record(at=0, roi=2, reason="live_aguarda_atraso"))
        self.assertFalse(self.record(at=15, roi=2, reason="live_aguarda_atraso"))
        self.assertTrue(self.record(at=16, roi=2, reason="live_em_verificacao"))
        self.assertFalse(self.record(at=20, roi=2, reason="live_aguarda_atraso"))
        self.assertTrue(self.record(at=60, roi=2, reason="live_aguarda_atraso"))

    def test_booking_bypasses_sampling_and_replay_is_idempotent_after_reopen(self):
        self.record(roi=1, reason="validacao_sem_registo")
        self.assertTrue(self.record(at=.01, roi=1, reason="registada", trade_id=1))
        with sqlite3.connect(self.path) as reopened:
            audit.create_schema(reopened)
            self.assertFalse(audit.record_decision(
                reopened, pair(), "PRE_JOGO", 1, "registada", trade_id=1,
                now_epoch=BASE + 100,
            ))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[1]["trade_id"], 1)

    def test_sampling_survives_reopen(self):
        self.record()
        with sqlite3.connect(self.path) as reopened:
            self.assertFalse(audit.record_decision(
                reopened, pair(), "PRE_JOGO", -1, "abaixo_margem", now_epoch=BASE + 10,
            ))

    def test_unapproved_fields_and_secret_bearing_free_text_are_not_stored(self):
        details = {
            "headers": {"x-sx-api-key": "SECRET_A"},
            "api_key": "SECRET_B", "request": "SECRET_C", "note": "SECRET_D",
            "provider_reason": "https://example.test?key=SECRET_E",
            "exception_type": "RuntimeError", "failure_stage": "costs_unknown",
            "quote": {"total_cents": 1099, "token": "SECRET_F"},
        }
        self.record(roi=1, reason="costs_unknown", details=details)
        row = self.rows()[0]
        self.assertNotIn("SECRET", json.dumps(row))
        self.assertNotIn("NEVER_STORE_THIS_KEY", json.dumps(row))
        self.assertEqual(json.loads(row["details_json"]), {
            "exception_type": "RuntimeError", "failure_stage": "costs_unknown",
            "quote": {"total_cents": 1099},
        })

    def test_nonfinite_numbers_and_booleans_cannot_become_misleading_roi(self):
        details = {"final_roi_pct": math.inf, "stake_eur": math.nan,
                   "first_roi_pct": True, "verified": True}
        self.record(roi=math.nan, details=details)
        row = self.rows()[0]
        self.assertIsNone(row["indicative_roi_pct"])
        self.assertEqual(json.loads(row["details_json"]), {
            "final_roi_pct": None, "stake_eur": None, "first_roi_pct": None,
            "verified": True,
        })
        self.assertEqual(row["importance"], 0)

    def test_bounded_numeric_books_and_cyclic_details_do_not_expand_evidence(self):
        details = {"poly_levels": [[.4, 50]] * 10000,
                   "sx_levels": [[.5, 2], [math.nan, 1], [.6, -1], [1.1, 1], "SECRET"]}
        details["quote"] = details
        self.record(roi=12, reason="margem_anomala", details=details)
        encoded = self.rows()[0]["details_json"]
        result = json.loads(encoded)
        self.assertEqual(len(result["poly_levels"]), 8)
        self.assertEqual(result["sx_levels"], [[.5, 2]])
        self.assertLessEqual(len(encoded.encode("utf-8")), audit.MAX_DETAILS_BYTES)
        self.assertNotIn("SECRET", encoded)

    def test_retention_drops_routine_negative_before_positive_and_booking(self):
        self.record(number=1, at=0, roi=8.08, reason="recheck_price_changed")
        self.record(number=2, at=1, roi=1, reason="registada", trade_id=7)
        self.record(number=3, at=2, roi=-1)
        self.record(number=4, at=3, roi=-1)
        removed = audit.prune_decisions(self.conn, now_epoch=BASE + 4, max_rows=2)
        self.assertEqual(removed, 2)
        self.assertEqual([row["importance"] for row in self.rows()], [1, 2])
        audit.prune_decisions(self.conn, now_epoch=BASE + 5, max_rows=1)
        self.assertEqual(self.rows()[0]["trade_id"], 7)

    def test_retention_expires_even_positive_rows_and_bookings_after_12h(self):
        self.record(number=1, roi=3, reason="registada", trade_id=1)
        self.record(number=2, at=100, roi=2)
        audit.prune_decisions(self.conn, now_epoch=BASE + 43201)
        self.assertEqual([row["pair_key"] for row in self.rows()], [pair(2)["key"]])

    def test_each_insert_enforces_configured_cap(self):
        from unittest.mock import patch
        with patch.object(audit, "MAX_ROWS", 3):
            for number in range(7):
                self.record(number=number, at=number)
                self.assertLessEqual(len(self.rows()), 3)
        self.assertEqual([row["pair_key"] for row in self.rows()], [pair(n)["key"] for n in (4, 5, 6)])

    def test_summary_is_sampled_and_separates_before_and_after_reset(self):
        self.record(number=1, at=0)
        self.record(number=1, at=10, roi=8.08, reason="recheck_price_changed")
        self.record(number=2, at=20, roi=12, reason="margem_anomala")
        self.record(number=3, at=30, roi=1, reason="registada", trade_id=9)
        self.record(number=4, at=40)
        result = audit.summary_decisions(self.conn, BASE, reset_epoch=BASE + 15, now_epoch=BASE + 45)
        self.assertEqual(result["samples"], 5)
        self.assertEqual(result["positive_samples"], 3)
        self.assertEqual(result["anomalous_samples"], 1)
        self.assertEqual(result["booked_samples"], 1)
        self.assertEqual(result["positive_market_pairs"], 3)
        self.assertEqual(result["before_reset_samples"], 2)
        self.assertEqual(result["after_reset_samples"], 3)
        self.assertEqual(result["last_positive"]["trade_id"], 9)
        self.assertFalse(result["last_positive"]["before_reset"])
        report = audit.format_audit_summary(result)
        self.assertIn("amostras", report)
        self.assertIn("antes do reset", report)
        self.assertIn("1 registos virtuais auditados", report)

    def test_report_uses_concrete_rejection_and_lisbon_time(self):
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        audit.record_decision(
            self.conn, pair(), "PRE_JOGO", 8.08, "validacao_sem_registo",
            details={"failure_stage": "recheck_price_changed", "final_roi_pct": .32,
                     "stake_eur": 80}, now_epoch=summer,
        )
        report = audit.format_audit_summary(audit.summary_decisions(
            self.conn, summer - 1, reset_epoch=summer + 1, now_epoch=summer + 2,
        ))
        self.assertIn("01/07 13:00:00 Lisboa · antes do reset", report)
        self.assertIn("Preço mudou durante a confirmação", report)
        self.assertIn("Indicativa +8,08% · dimensionada +0,32% · 80,00 €", report)

    def test_unknown_reason_does_not_invent_a_specific_cause(self):
        self.record(roi=1, reason="a_new_provider_code")
        report = audit.format_audit_summary(audit.summary_decisions(self.conn, BASE, now_epoch=BASE + 1))
        self.assertIn("Motivo registado: a_new_provider_code", report)
        self.assertNotIn("Preço mudou", report)
        self.assertEqual(audit.reason_label("secret=https://example.test"), "Motivo individual não disponível")

    def test_failure_rolls_back_and_busy_timeout_is_restored(self):
        timeout = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.conn.execute(f"CREATE TRIGGER reject_audit BEFORE INSERT ON {audit.TABLE} BEGIN SELECT RAISE(ABORT,'test refusal'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.record(roi=1)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], timeout)

    def test_locked_database_wait_is_bounded_and_original_timeout_is_restored(self):
        holder = sqlite3.connect(self.path)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.record()
        finally:
            holder.rollback()
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 20000)
        self.assertFalse(self.conn.in_transaction)

    def test_two_connections_cannot_double_record_a_booked_trade(self):
        barrier = threading.Barrier(2)
        outcomes, failures = [], []
        def record():
            try:
                with sqlite3.connect(self.path) as db:
                    barrier.wait(timeout=5)
                    outcomes.append(audit.record_decision(
                        db, pair(), "PRE_JOGO", 1, "registada",
                        trade_id=55, now_epoch=BASE,
                    ))
            except Exception as exc:
                failures.append(exc)
        workers = [threading.Thread(target=record) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertFalse(failures)
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(len(self.rows()), 1)

    def test_empty_summary_and_invalid_inputs_do_not_change_data(self):
        summary = audit.summary_decisions(self.conn, BASE, now_epoch=BASE + 1)
        self.assertIn("ainda sem amostras", audit.format_audit_summary(summary))
        for invalid in (-1, math.nan, math.inf, "123", True):
            with self.assertRaises(ValueError):
                audit.record_decision(self.conn, pair(), "PRE_JOGO", 1, "registada", now_epoch=invalid)
        for invalid in (-1, 0, True, "1"):
            with self.assertRaises(ValueError):
                self.record(trade_id=invalid)
        with self.assertRaises(ValueError):
            audit.record_decision(self.conn, {"key": ""}, "PRE_JOGO", 1, "x")
        with self.assertRaises(ValueError):
            self.record(details="not an object")
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
