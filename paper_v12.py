"""PAPER V12 — ledger de arbitragem simulada Polymarket + SX Bet.

SOMENTE paper trading. Não contém chaves de carteira nem endpoints de apostas.
O lucro é CONDICIONAL a ambos os mercados serem liquidados normalmente e a
ambas as pernas hipoteticamente serem preenchidas. Não é lucro garantido.

Usar com monitor_v11.py; teste: python paper_v12.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

INITIAL_CENTS = 100_000                 # 1.000 EUR em cada plataforma
MAX_TRADE_CENTS = 10_000                # 100 EUR TOTAL, incluindo custos
MAX_OPEN_EXPOSURE_CENTS = 50_000        # máximo 500 EUR expostos em conjunto
MIN_TRADE_CENTS = 500                  # mínimo de 5 EUR por operação
# Percentagem para papel: PAPER_V12_MIN_NET_ROI_PCT=0.5 significa 0,5%.
# A configuração não ativa ordens REAIS; só muda o limiar de registo virtual.
def read_min_roi():
    raw = os.getenv("PAPER_V12_MIN_NET_ROI_PCT", "1.5").strip().replace(",", ".")
    try:
        pct = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("PAPER_V12_MIN_NET_ROI_PCT deve ser uma percentagem, ex. 0.5") from exc
    if not (math.isfinite(pct) and 0.5 <= pct <= 15.0):
        raise ValueError("PAPER_V12_MIN_NET_ROI_PCT deve estar entre 0.5 e 15.0")
    return pct / 100.0


MIN_NET_ROI = read_min_roi()          # padrão 1,5%; piloto 0,5% no Railway
POLY_TAKER_FEE_RATE = 0.05             # parâmetro desportivo indicado no help center
ADVERSE_PRICE_BUFFER = 0.0125          # 1,25% por perna, reserva NÃO oficial
MAX_SNAPSHOT_GAP_SECONDS = 4.0
# Bloquear novas operações paper com margens invulgares, sujeitas a auditoria.
# Não altera nem liquida operações já registadas.
MAX_AUTO_PAPER_ROI = 0.10

PAPER_DB = os.getenv("PAPER_V12_DB", "/data/paper_v12.sqlite3")
ALLOWED_SPORTS = {"tennis", "basketball", "american_football",
                  "baseball", "cricket", "esports", "combat"}
# Observação: elegibilidade em paper não prova equivalência de liquidação.
# Variável antiga mantida apenas para compatibilidade das configurações.
# Nunca autoriza novas operações LIVE_NAO_CONFIRMADO.
ALLOW_LIVE_UNVERIFIED = os.getenv("PAPER_V12_LIVE_UNVERIFIED", "0") == "1"
PAPER_LIVE_ENABLED = os.getenv("PAPER_LIVE_ENABLED", "1").strip().lower() in {"1", "true"}
MAX_LIVE_EVIDENCE_SECONDS = 3.0
MAX_ENTRY_EVIDENCE_BYTES = 8192

SCHEMA_VERSION = 14
MIGRATION_RESERVE_BYTES = 32 * 1024 * 1024
LOG = logging.getLogger("paper_v13")
TRADE_COLUMNS_V13 = {
    "game_start_epoch": "INTEGER",
    "poly_token_id": "TEXT",
    "sx_outcome_one": "INTEGER",
    "settled_epoch": "INTEGER",
    "poly_won": "INTEGER",
    "sx_won": "INTEGER",
    "poly_credit_cents": "INTEGER",
    "sx_credit_cents": "INTEGER",
    "resolution_evidence_json": "TEXT",
    "entry_evidence_json": "TEXT",
}


def cents_up(value: float) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_CEILING))


def cents_down(value: float) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_FLOOR))


def walk_book(levels, payout):
    """Custo e distribuição por níveis para um mesmo payout em USD/USDC.

    Ambos os livros têm formato (preço/probabilidade, capacidade em payout).
    O livro SX já deve estar convertido do referencial maker para taker.
    """
    if payout <= 0 or not math.isfinite(payout):
        return None
    remaining, cost, poly_fee_units = payout, 0.0, 0.0
    for price, capacity in sorted(levels, key=lambda row: row[0]):
        price, capacity = float(price), float(capacity)
        if not (math.isfinite(price) and math.isfinite(capacity) and
                0 < price < 1 and capacity > 0):
            continue
        size = min(remaining, capacity)
        cost += size * price
        poly_fee_units += size * POLY_TAKER_FEE_RATE * price * (1 - price)
        remaining -= size
        if remaining <= 1e-7:
            return cost, poly_fee_units
    return None


@dataclass(frozen=True)
class ShadowQuote:
    payout_usdc: float
    fx_eur_per_usd: float
    poly_debit_cents: int
    sx_debit_cents: int
    poly_fee_cents: int
    total_cents: int
    payout_eur_cents: int
    conditional_profit_cents: int
    roi: float

    @property
    def total_eur(self):
        return self.total_cents / 100

    @property
    def profit_eur(self):
        return self.conditional_profit_cents / 100


def quote_for_payout(poly_levels, sx_levels, payout_usdc, fx):
    """Inclui fee de taker Poly, buffers, arredondamento a cêntimos."""
    if not (math.isfinite(fx) and 0 < fx < 10):
        return None
    poly = walk_book(poly_levels, payout_usdc)
    sx = walk_book(sx_levels, payout_usdc)
    if poly is None or sx is None:
        return None
    poly_stake, poly_fee = poly
    sx_stake, _ = sx
    # Reserva adversa independente em cada leg. Não simula a execução real.
    poly_debit = cents_up((poly_stake * (1 + ADVERSE_PRICE_BUFFER) + poly_fee) * fx)
    sx_debit = cents_up(sx_stake * (1 + ADVERSE_PRICE_BUFFER) * fx)
    fee_cents = cents_up(poly_fee * fx)
    total = poly_debit + sx_debit
    payout_cents = cents_down(payout_usdc * fx)
    if total <= 0:
        return None
    profit = payout_cents - total
    return ShadowQuote(
        payout_usdc, fx, poly_debit, sx_debit, fee_cents, total,
        payout_cents, profit, profit / total
    )


def select_shadow_quote(poly_levels, sx_levels, fx,
                        free_poly_cents, free_sx_cents):
    """Procura stake comum, respeitando profundidade, carteiras e 100 EUR."""
    if not (poly_levels and sx_levels and 0 < fx < 10):
        return None
    for book in (poly_levels, sx_levels):
        for price, capacity in book:
            if not (math.isfinite(price) and math.isfinite(capacity)
                    and 0 < price < 1 and capacity > 0):
                return None

    capacity = min(
        sum(cap for _, cap in poly_levels),
        sum(cap for _, cap in sx_levels),
        1_000.0 / fx,
    )
    if capacity <= 0:
        return None

    low, high = 0.0, capacity
    for _ in range(45):
        middle = (low + high) / 2
        quote = quote_for_payout(poly_levels, sx_levels, middle, fx)
        allowed = (
            quote is not None
            and quote.total_cents <= MAX_TRADE_CENTS
            and quote.poly_debit_cents <= free_poly_cents
            and quote.sx_debit_cents <= free_sx_cents
        )
        if allowed:
            low = middle
        else:
            high = middle

    # Reduz para 6 decimais e recalcula depois do arredondamento.
    payout = math.floor(low * 1_000_000) / 1_000_000
    quote = quote_for_payout(poly_levels, sx_levels, payout, fx)
    if quote is None:
        return None
    if not (MIN_TRADE_CENTS <= quote.total_cents <= MAX_TRADE_CENTS):
        return None
    if quote.poly_debit_cents > free_poly_cents or quote.sx_debit_cents > free_sx_cents:
        return None
    if (quote.roi < MIN_NET_ROI or quote.roi >= MAX_AUTO_PAPER_ROI
            or quote.conditional_profit_cents <= 0):
        return None
    return quote


def _table_names(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _trade_column_names(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(paper_v12_trades)")}


def _migration_needed(conn):
    tables = _table_names(conn)
    return (not set(TRADE_COLUMNS_V13).issubset(_trade_column_names(conn))
            or "paper_v12_settlement_notifications" not in tables
            or "paper_v13_migrations" not in tables
            or conn.execute("SELECT 1 FROM paper_v13_migrations WHERE version=?",
                            (SCHEMA_VERSION,)).fetchone() is None)


def _migration_snapshot(conn):
    """Invariantes lidas dentro do bloqueio de escrita da migração."""
    wallets = tuple(tuple(row) for row in conn.execute(
        "SELECT platform,free_cents FROM paper_v12_wallets ORDER BY platform"
    ))
    trades = int(conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0])
    return wallets, trades


def _backup_before_migration(conn, source_path):
    """Backup SQLite consistente, verificado, antes de qualquer DDL no ledger.

    Usa a API SQLite, incluindo alterações já confirmadas no WAL. Não copia
    cegamente o ficheiro principal nem apaga backups ou operações anteriores.
    """
    source_path = Path(source_path).resolve()
    configured = os.getenv("PAPER_V13_BACKUP_DIR", "").strip()
    backup_dir = Path(configured).expanduser().resolve() if configured else source_path.parent / "backups"
    existing_parent = backup_dir
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    estimated_bytes = max(source_path.stat().st_size, pages * page_size)
    required = math.ceil(estimated_bytes * 1.2) + MIGRATION_RESERVE_BYTES
    if shutil.disk_usage(existing_parent).free < required:
        raise RuntimeError(
            "Migração PAPER bloqueada: espaço insuficiente para backup "
            "consistente e reserva de 32 MiB. Carteiras e histórico preservados."
        )
    if shutil.disk_usage(source_path.parent).free < MIGRATION_RESERVE_BYTES:
        raise RuntimeError("Migração PAPER bloqueada: menos de 32 MiB livres no volume original")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{source_path.stem}-pre-v{SCHEMA_VERSION}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    partial = Path(str(target) + ".partial")
    deadline = time.monotonic() + 30

    def check_progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError("Backup PAPER demorou demasiado; migração não iniciada")
        if shutil.disk_usage(backup_dir).free < MIGRATION_RESERVE_BYTES:
            raise RuntimeError("Backup PAPER interrompido para preservar a reserva de espaço")

    try:
        destination = sqlite3.connect(str(partial), timeout=20)
        try:
            conn.backup(destination, pages=128, progress=check_progress, sleep=0.05)
            check = destination.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError("Backup PAPER não passou a verificação de integridade")
            if not {"paper_v12_wallets", "paper_v12_trades"}.issubset(_table_names(destination)):
                raise RuntimeError("Backup PAPER sem as tabelas esperadas")
        finally:
            destination.close()
        partial.chmod(0o600)
        os.replace(partial, target)
        return str(target)
    except Exception:
        # Apenas remove o ficheiro parcial criado por esta tentativa.
        partial.unlink(missing_ok=True)
        raise


def open_database(path=PAPER_DB):
    memory = str(path) == ":memory:"
    source_path = None if memory else Path(path).expanduser().resolve()
    existing_file = bool(source_path and source_path.is_file() and source_path.stat().st_size > 0)
    if source_path is not None:
        source_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(":memory:" if memory else str(source_path), timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    backup_path = None
    try:
        if existing_file:
            tables = _table_names(conn)
            if not {"paper_v12_wallets", "paper_v12_trades"}.issubset(tables):
                raise RuntimeError("Base PAPER existente inesperada; não inicializar ou substituir carteiras")
            existing_wallets = dict(conn.execute(
                "SELECT platform, free_cents FROM paper_v12_wallets"
            ))
            if set(existing_wallets) != {"poly", "sx"}:
                raise RuntimeError("Carteiras PAPER incompletas; revisão necessária antes de continuar")
            if _migration_needed(conn):
                backup_path = _backup_before_migration(conn, source_path)
        conn.execute("BEGIN IMMEDIATE")
        before_migration = (
            _migration_snapshot(conn) if existing_file and _migration_needed(conn) else None
        )
        schema = """
      CREATE TABLE IF NOT EXISTS paper_v12_wallets(
        platform TEXT PRIMARY KEY, free_cents INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS paper_v12_notifications(
        trade_id INTEGER PRIMARY KEY,
        sent_epoch INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS paper_v12_open_notifications(
        trade_id INTEGER PRIMARY KEY,
        message TEXT NOT NULL,
        sent_epoch INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_epoch INTEGER
      );
      CREATE TABLE IF NOT EXISTS paper_v12_trades(
        id INTEGER PRIMARY KEY,
        event_key TEXT UNIQUE NOT NULL,
        created_epoch INTEGER NOT NULL,
        sport TEXT NOT NULL,
        event TEXT NOT NULL,
        poly_market TEXT NOT NULL,
        sx_market TEXT NOT NULL,
        poly_side TEXT NOT NULL,
        sx_side TEXT NOT NULL,
        stage TEXT NOT NULL,
        payout_usdc REAL NOT NULL,
        fx_eur_per_usd REAL NOT NULL,
        poly_debit_cents INTEGER NOT NULL,
        sx_debit_cents INTEGER NOT NULL,
        poly_fee_cents INTEGER NOT NULL,
        conditional_payout_cents INTEGER NOT NULL,
        conditional_profit_cents INTEGER NOT NULL,
        status TEXT NOT NULL,
        settled_side TEXT,
        realized_profit_cents INTEGER,
        note TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS paper_v12_settlement_notifications(
        trade_id INTEGER PRIMARY KEY,
        message TEXT NOT NULL,
        sent_epoch INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_epoch INTEGER
      );
      CREATE TABLE IF NOT EXISTS paper_v13_migrations(
        version INTEGER PRIMARY KEY,
        applied_epoch INTEGER NOT NULL,
        backup_path TEXT
      );
        """
        for statement in schema.split(";"):
            if statement.strip():
                conn.execute(statement)
        existing_columns = _trade_column_names(conn)
        for name, kind in TRADE_COLUMNS_V13.items():
            if name not in existing_columns:
                conn.execute(f"ALTER TABLE paper_v12_trades ADD COLUMN {name} {kind}")
        for name in ("poly", "sx"):
            conn.execute(
                "INSERT OR IGNORE INTO paper_v12_wallets(platform, free_cents) VALUES (?,?)",
                (name, INITIAL_CENTS),
            )
        conn.execute(
            "INSERT OR IGNORE INTO paper_v13_migrations(version,applied_epoch,backup_path) VALUES(?,?,?)",
            (SCHEMA_VERSION, int(time.time()), backup_path),
        )
        if before_migration is not None and _migration_snapshot(conn) != before_migration:
            raise RuntimeError(
                "Migração PAPER revertida: saldos ou contagem de operações alterados inesperadamente"
            )
        conn.commit()
        if before_migration is not None:
            LOG.info(
                "PAPER_SCHEMA | versão=%d | carteiras_preservadas=true | operações_preservadas=%d | backup=%s",
                SCHEMA_VERSION, before_migration[1], backup_path,
            )
        return conn
    except Exception:
        conn.rollback()
        conn.close()
        raise


def free_balances(conn):
    return dict(conn.execute(
        "SELECT platform, free_cents FROM paper_v12_wallets"
    ).fetchall())


def event_key(pair):
    poly_id = str(pair["poly"]["id"])
    sx_hash = str(pair["sx"]["hash"])
    return hashlib.sha256(f"{poly_id}|{sx_hash}".encode()).hexdigest()


def open_exposure(conn):
    row = conn.execute("""
        SELECT COALESCE(SUM(poly_debit_cents + sx_debit_cents),0)
        FROM paper_v12_trades WHERE status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED')
    """).fetchone()
    return int(row[0])


def _epoch_or_none(value):
    try:
        parsed = float(value)
        return int(parsed) if math.isfinite(parsed) and parsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _portugal_time(value):
    epoch = _epoch_or_none(value)
    if epoch is None:
        return "não disponível"
    try:
        return datetime.fromtimestamp(epoch, ZoneInfo("Europe/Lisbon")).strftime(
            "%d/%m/%Y %H:%M (Portugal)"
        )
    except (ValueError, TypeError, OSError, OverflowError, KeyError):
        try:
            return datetime.fromtimestamp(epoch, timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        except (ValueError, TypeError, OSError, OverflowError):
            return "não disponível"


def _finite_number(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _exact_identifier(value, expected):
    """Identifiers are exact; numeric API IDs may be canonical decimal text."""
    return (isinstance(value, str) and 0 < len(value) <= 512
            and type(expected) in (str, int) and not isinstance(expected, bool)
            and value == str(expected))


def _live_evidence_valid(pair, evidence, valid_until_mono):
    """Validate a coordinator proof, never infer live from scheduled time.

    The price deadline is supplied by the fresh-book coordinator. It must
    also fit within the stricter lifetime of the official phase checks.
    No network call or claimed execution is made by the ledger.
    """
    if not PAPER_LIVE_ENABLED or type(evidence) is not dict:
        return False
    if evidence.get("method") != "official_live_v1":
        return False
    try:
        identities = {
            "pair_key": pair["key"],
            "poly_event_id": pair["poly"]["event_id"],
            "poly_market_id": pair["poly"]["id"],
            "poly_asset_id": pair["poly_asset"],
            "poly_side": pair["poly_label"],
            "sx_event_id": pair["sx"]["event_id"],
            "sx_market_hash": pair["sx"]["hash"],
            "sx_side": pair["sx_label"],
        }
        if any(not _exact_identifier(evidence.get(name), expected)
               for name, expected in identities.items()):
            return False
        target = pair["sx_target"]
        if (type(target) is not int or target not in (0, 1)
                or type(evidence.get("sx_target")) is not int
                or evidence["sx_target"] != target):
            return False
        for field in ("poly_live", "poly_active", "poly_accepting_orders",
                      "sx_live_enabled", "verified_after_delay"):
            if evidence.get(field) is not True:
                return False
        for field in ("poly_ended", "poly_closed", "sx_reported"):
            if evidence.get(field) is not False:
                return False
        if (type(evidence.get("sx_fixture_status")) is not int
                or evidence["sx_fixture_status"] != 2
                or evidence.get("sx_status") != "ACTIVE"):
            return False

        delay_fields = ("sx_delay_ms", "poly_delay_ms", "required_delay_ms",
                        "initial_required_delay_ms")
        if any(type(evidence.get(field)) is not int or evidence[field] < 0
               for field in delay_fields):
            return False
        required_ms = max(evidence["sx_delay_ms"], evidence["poly_delay_ms"])
        if evidence["required_delay_ms"] != required_ms:
            return False
        times = {}
        for field in ("candidate_started_mono", "delay_started_mono",
                      "delay_ready_mono", "checks_started_mono", "checked_mono",
                      "checked_epoch", "valid_until_mono"):
            value = _finite_number(evidence.get(field))
            if value is None:
                return False
            times[field] = value
        quote_deadline = _finite_number(valid_until_mono)
        now_mono, now_epoch = time.monotonic(), time.time()
        if quote_deadline is None or not all(map(math.isfinite, (now_mono, now_epoch))):
            return False
        if not (times["candidate_started_mono"] <= times["delay_started_mono"]
                <= times["delay_ready_mono"] <= times["checks_started_mono"]
                <= times["checked_mono"] <= now_mono
                <= quote_deadline <= times["valid_until_mono"]):
            return False
        if not math.isclose(
                times["valid_until_mono"],
                times["checks_started_mono"] + MAX_LIVE_EVIDENCE_SECONDS,
                rel_tol=0, abs_tol=1e-6):
            return False
        # checked_epoch may be stored as whole seconds; the monotonic bound
        # above remains exact and is never enlarged by that rounding.
        if not (times["checked_epoch"] > 0
                and 0 <= now_epoch - times["checked_epoch"] <= MAX_LIVE_EVIDENCE_SECONDS + 1):
            return False
        delay_seconds = max(required_ms, evidence["initial_required_delay_ms"]) / 1000
        if not math.isfinite(delay_seconds):
            return False
        if times["delay_ready_mono"] - times["delay_started_mono"] + 1e-6 < delay_seconds:
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def _freeze_live_evidence(evidence):
    """Persist exactly the bounded proof used by the in-transaction guards."""
    if type(evidence) is not dict:
        return None, None
    try:
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_ENTRY_EVIDENCE_BYTES:
            return None, None
        return json.loads(encoded), encoded
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None, None


def _entry_guard_valid(pair, stage, valid_until_mono, live_evidence=None):
    """A guarded observation must remain fresh until its virtual commit.

    ``None`` preserves pre-game legacy/manual fixtures. Confirmed live always
    requires official evidence and a monotonic final-book deadline.
    """
    if stage == "LIVE_CONFIRMADO":
        return _live_evidence_valid(pair, live_evidence, valid_until_mono)
    if stage != "PRE_JOGO":
        return False
    if valid_until_mono is None:
        return True
    if isinstance(valid_until_mono, (str, bytes, bool)):
        return False
    try:
        deadline = float(valid_until_mono)
        if not math.isfinite(deadline) or time.monotonic() > deadline:
            return False
        if stage == "PRE_JOGO":
            start = float(pair["sx"]["start"])
            if not math.isfinite(start) or start <= time.time():
                return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def book_shadow_trade(conn, pair, quote, stage, now_epoch=None,
                      opening_odds=None, *, valid_until_mono=None,
                      live_evidence=None):
    """Duas pernas virtuais numa única transação SQLite. Nunca faz apostas.

    Uma única simulação por par de mercados, mesmo que a direção mude.
    Não marca como REAL_EXECUTED nem como garantia de arbitragem.
    """
    evidence_json = None
    if stage == "LIVE_CONFIRMADO":
        live_evidence, evidence_json = _freeze_live_evidence(live_evidence)
    if not _entry_guard_valid(pair, stage, valid_until_mono, live_evidence):
        return None
    if pair["sx"]["sport"] not in ALLOWED_SPORTS:
        return None
    if str(pair["poly"].get("type") or "").lower() != "moneyline":
        return None
    # Uma diferença grande de horário é indício de mercado diferente.
    if abs(float(pair["poly"].get("start", 0)) -
           float(pair["sx"].get("start", 0))) > 1800:
        return None
    if pair["poly"].get("sport") != pair["sx"].get("sport"):
        return None
    if str(pair["poly_label"]).strip().lower() == str(pair["sx_label"]).strip().lower():
        return None
    if (quote.total_cents > MAX_TRADE_CENTS
            or quote.roi < MIN_NET_ROI
            or not math.isfinite(quote.roi)
            or quote.roi >= MAX_AUTO_PAPER_ROI):
        return None

    eid = event_key(pair)
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    game_start = _epoch_or_none(pair["sx"].get("start"))
    poly_token_id = str(pair.get("poly_asset") or "").strip() or None
    sx_target = pair.get("sx_target")
    sx_outcome_one = int(sx_target == 0) if type(sx_target) is int and sx_target in (0, 1) else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        # A busy SQLite writer can outlive an otherwise fresh quote. Check
        # after obtaining the lock, before any debit or outbox insert.
        if not _entry_guard_valid(pair, stage, valid_until_mono, live_evidence):
            conn.rollback()
            return None
        if valid_until_mono is not None:
            now_epoch = int(time.time())
        if conn.execute("SELECT 1 FROM paper_v12_trades WHERE event_key=?", (eid,)).fetchone():
            conn.rollback()
            return None
        free = free_balances(conn)
        if free["poly"] < quote.poly_debit_cents or free["sx"] < quote.sx_debit_cents:
            conn.rollback()
            return None
        if open_exposure(conn) + quote.total_cents > MAX_OPEN_EXPOSURE_CENTS:
            conn.rollback()
            return None

        conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents-? WHERE platform='poly'",
                     (quote.poly_debit_cents,))
        conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents-? WHERE platform='sx'",
                     (quote.sx_debit_cents,))
        result = conn.execute("""
          INSERT INTO paper_v12_trades(
            event_key, created_epoch, sport, event, poly_market, sx_market,
            poly_side, sx_side, stage, payout_usdc, fx_eur_per_usd,
            poly_debit_cents, sx_debit_cents, poly_fee_cents,
            conditional_payout_cents, conditional_profit_cents, status, note,
            game_start_epoch, poly_token_id, sx_outcome_one, entry_evidence_json
          ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN_CONDITIONAL',?,?,?,?,?)
        """, (
            eid, now_epoch, pair["sx"]["sport"],
            " vs ".join(pair["sx"]["teams"]), str(pair["poly"]["id"]),
            str(pair["sx"]["hash"]), pair["poly_label"], pair["sx_label"],
            stage, quote.payout_usdc, quote.fx_eur_per_usd,
            quote.poly_debit_cents, quote.sx_debit_cents,
            quote.poly_fee_cents, quote.payout_eur_cents,
            quote.conditional_profit_cents,
            "Só simulação; fills presumidos. Lucro apenas se ambos os "
            "mercados resolverem normalmente; exceções exigem revisão.",
            game_start, poly_token_id, sx_outcome_one, evidence_json,
        ))
        trade_id = result.lastrowid
        if opening_odds is not None:
            # A mensagem é persistida NA MESMA transação que os dois débitos
            # fictícios. Se o processo terminar antes de enviar ao Telegram,
            # outro ciclo pode enviá-la sem registar nova operação.
            odds_poly, odds_sx = opening_odds
            frozen_message = format_open_message(
                conn, trade_id, odds_poly=odds_poly, odds_sx=odds_sx,
                game_start=pair["sx"].get("start"),
            )
            conn.execute(
                "INSERT INTO paper_v12_open_notifications(trade_id, message) "
                "VALUES (?,?)", (trade_id, frozen_message),
            )
        # Formatting/writing the operation can also cross the freshness or
        # pre-game boundary. Roll back both wallets, the trade and its notice.
        if not _entry_guard_valid(pair, stage, valid_until_mono, live_evidence):
            conn.rollback()
            return None
        conn.commit()
        return trade_id
    except Exception:
        conn.rollback()
        raise


def paper_summary(conn):
    # Um único statement mantém carteiras e agregados no mesmo snapshot,
    # mesmo quando o worker confirma uma liquidação noutra ligação SQLite.
    poly_free, sx_free, open_count, settled, reviews, exposure, realized = conn.execute("""
        SELECT
          (SELECT free_cents FROM paper_v12_wallets WHERE platform='poly'),
          (SELECT free_cents FROM paper_v12_wallets WHERE platform='sx'),
          COALESCE(SUM(CASE WHEN status='OPEN_CONDITIONAL' THEN 1 ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN status='REVIEW_REQUIRED' THEN 1 ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED')
                            THEN poly_debit_cents+sx_debit_cents ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN status='SETTLED' THEN realized_profit_cents ELSE 0 END),0)
        FROM paper_v12_trades
    """).fetchone()
    return {
        "poly_free_eur": poly_free / 100,
        "sx_free_eur": sx_free / 100,
        "open_trades": open_count,
        "settled": settled,
        "requires_review": reviews,
        "exposure_eur": exposure / 100,
        "realized_eur": realized / 100,
    }



def eur(value_cents):
    """Apresentação portuguesa dos cêntimos de uma carteira virtual."""
    return f"{int(value_cents) / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + " €"


def trade_details(conn, trade_id):
    """Linha completa da operação, sem alterar saldos ou estados."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM paper_v12_trades WHERE id=?", (int(trade_id),)
    ).fetchone()
    return row


def format_wallets(conn):
    """Mostra apenas saldo livre; capital aberto continua reservado."""
    balances = free_balances(conn)
    return (
        f"💰 Carteira Polymarket: {eur(balances['poly'])}\n"
        f"💰 Carteira SX Bet: {eur(balances['sx'])}"
    )


def format_open_message(conn, trade_id, odds_poly=None, odds_sx=None,
                        demo=False, game_start=None):
    """Mensagem enviada apenas para uma OPERAÇÃO VIRTUAL efetivamente registada."""
    record = trade_details(conn, trade_id)
    if record is None or record["status"] != "OPEN_CONDITIONAL":
        raise ValueError("Operação virtual aberta não encontrada")
    def odds(value):
        return f" | odd {value:.2f}".replace(".", ",") if value is not None else ""

    game_time = _portugal_time(game_start if game_start is not None else record["game_start_epoch"])
    balances = free_balances(conn)
    phase = "EM DIRETO confirmado" if record["stage"] == "LIVE_CONFIRMADO" else record["stage"]
    live_lines = []
    if record["stage"] == "LIVE_CONFIRMADO":
        try:
            evidence = json.loads(record["entry_evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if isinstance(evidence, dict):
            for label, names in (("Marcador observado", ("score", "poly_score")),
                                 ("Período observado", ("period", "poly_period"))):
                value = next((evidence.get(name) for name in names
                              if evidence.get(name) is not None), None)
                if type(value) in (str, int) and str(value).strip():
                    text = " ".join(str(value).split())[:120]
                    live_lines.append(f"🏟️ {label}: {text}")
            delay_poly = _finite_number(evidence.get("poly_delay_ms"))
            delay_sx = _finite_number(evidence.get("sx_delay_ms"))
            if delay_poly is not None and delay_sx is not None:
                poly_text = f"{delay_poly / 1000:.2f}".replace(".", ",")
                sx_text = f"{delay_sx / 1000:.2f}".replace(".", ",")
                live_lines.append(
                    f"⏱️ Atrasos considerados: Polymarket {poly_text} s · SX Bet {sx_text} s"
                )
        live_lines.append("ℹ️ Hipótese após revalidação; não execução real.")
    message = [
        f"🧪 {'DEMONSTRAÇÃO FICTÍCIA' if demo else 'APOSTA VIRTUAL'} #{trade_id}",
        f"🏟️ {record['event']}",
        f"📅 Início previsto: {game_time}",
        f"🕒 Registo: {time.strftime('%d/%m/%Y %H:%M:%S UTC', time.gmtime(record['created_epoch']))}",
        f"🏷️ Modalidade: {record['sport']} | fase: {phase}",
        *live_lines,
        "",
        "1️⃣ POLYMARKET",
        f"Equipa: {record['poly_side']}{odds(odds_poly)}",
        f"Valor retirado: {eur(record['poly_debit_cents'])}",
        "",
        "2️⃣ SX BET",
        f"Equipa: {record['sx_side']}{odds(odds_sx)}",
        f"Valor retirado: {eur(record['sx_debit_cents'])}",
        "",
        "ℹ️ Odds indicadas: melhor nível de compra REST, NÃO execução confirmada.",
        f"💶 Total aplicado: {eur(record['poly_debit_cents'] + record['sx_debit_cents'])}",
        "💰 SALDOS (antes → depois)",
        f"Carteira Polymarket: {eur(balances['poly'] + record['poly_debit_cents'])} → {eur(balances['poly'])}",
        f"Carteira SX Bet: {eur(balances['sx'] + record['sx_debit_cents'])} → {eur(balances['sx'])}",
        "",
        "⏳ Resultado: AGUARDA LIQUIDAÇÃO",
        f"📈 Lucro condicional previsto: {eur(record['conditional_profit_cents'])}",
        f"💵 Pagamento virtual condicional: {eur(record['conditional_payout_cents'])}",
        f"📊 ROI condicional: {100 * record['conditional_profit_cents'] / (record['poly_debit_cents'] + record['sx_debit_cents']):+.2f}%"
        + (" · 🧪 MARGEM BAIXA / EXPERIMENTAL" if
           record["conditional_profit_cents"] /
           (record["poly_debit_cents"] + record["sx_debit_cents"]) < 0.015
           else ""),
        "🔒 Apenas dinheiro fictício. Sem apostas reais.",
    ]
    return "\n".join(message)


def _settlement_message(record, balances_after, balances_before=None, demo=False):
    """Texto do momento do fecho; os saldos congelados vêm da transação."""
    winner_platform = record["settled_side"]
    if winner_platform not in ("poly", "sx", "both", "neither"):
        raise ValueError("Liquidação sem resultados reconhecidos")
    poly_won = bool(record["poly_won"]) if record["poly_won"] is not None else winner_platform in ("poly", "both")
    sx_won = bool(record["sx_won"]) if record["sx_won"] is not None else winner_platform in ("sx", "both")
    payout = int(record["conditional_payout_cents"])
    poly_credit = int(record["poly_credit_cents"]) if record["poly_credit_cents"] is not None else payout if poly_won else 0
    sx_credit = int(record["sx_credit_cents"]) if record["sx_credit_cents"] is not None else payout if sx_won else 0
    net = int(record["realized_profit_cents"])
    lines = [
        f"🏁 {'RESULTADO DEMONSTRATIVO' if demo else 'RESULTADO VIRTUAL CONFIRMADO'} #{record['id']}",
        f"🏟️ {record['event']}",
        f"📅 Início previsto: {_portugal_time(record['game_start_epoch'])}",
        f"🕒 Liquidação: {_portugal_time(record['settled_epoch'])}",
    ]
    if winner_platform in ("poly", "sx"):
        winner = record["poly_side"] if poly_won else record["sx_side"]
        platform = "POLYMARKET" if poly_won else "SX BET"
        loser = "SX BET" if poly_won else "POLYMARKET"
        lines.extend([
            f"🏆 Venceu: {winner}",
            f"✅ Paga a carteira: {platform}",
            f"❌ A outra aposta ({loser}) perdeu",
        ])
    else:
        lines.append("ℹ️ As duas seleções venceram." if poly_won else "ℹ️ As duas seleções perderam.")
        lines.append("Resultados tratados separadamente por plataforma.")
    lines.extend([
        f"Polymarket: {'vitória' if poly_won else 'derrota'} | crédito {eur(poly_credit)}",
        f"SX Bet: {'vitória' if sx_won else 'derrota'} | crédito {eur(sx_credit)}",
        f"💸 Valor creditado: {eur(poly_credit + sx_credit)}",
        f"{'✅' if net >= 0 else '🔴'} Resultado líquido: {'+' if net > 0 else ''}{eur(net)}",
        "",
    ])
    if balances_before is not None:
        lines.extend([
            "💰 SALDOS (antes → depois)",
            f"Carteira Polymarket: {eur(balances_before['poly'])} → {eur(balances_after['poly'])}",
            f"Carteira SX Bet: {eur(balances_before['sx'])} → {eur(balances_after['sx'])}",
        ])
    else:
        # Operações liquidadas antes desta migração não têm snapshot histórico.
        lines.extend([
            "💰 SALDOS ATUAIS (snapshot do fecho antigo indisponível)",
            f"Carteira Polymarket: {eur(balances_after['poly'])}",
            f"Carteira SX Bet: {eur(balances_after['sx'])}",
        ])
    lines.append("🔒 Resultado simulado, não houve apostas reais.")
    return "\n".join(lines)


def format_settlement_message(conn, trade_id, demo=False):
    """Devolve sempre o snapshot persistido; não recalcula saldos passados."""
    record = trade_details(conn, trade_id)
    if record is None or record["status"] != "SETTLED":
        raise ValueError("Operação ainda não liquidada/verificada")
    frozen = conn.execute(
        "SELECT message FROM paper_v12_settlement_notifications WHERE trade_id=?",
        (int(trade_id),),
    ).fetchone()
    if frozen:
        message = frozen[0]
        return message.replace("RESULTADO VIRTUAL CONFIRMADO", "RESULTADO DEMONSTRATIVO", 1) if demo else message
    return _settlement_message(record, free_balances(conn), demo=demo)


def send_telegram_notice(message):
    """Enviar Telegram sem expor token de API em erros ou logs."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print("ℹ️ Telegram não configurado: só foi gerado texto.")
        return False
    try:
        import requests
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message,
                  "disable_web_page_preview": True},
            timeout=12,
        )
        return response.status_code == 200 and response.json().get("ok") is True
    except Exception:
        return False


def send_open_once(conn, trade_id, now_epoch=None, retry_seconds=60):
    """Enviar aviso de abertura da outbox. NÃO cria novas operações.

    Timeout HTTP após receção pelo Telegram pode gerar duplicação do aviso
    numa tentativa posterior. Nunca gera um segundo débito.
    """
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    row = conn.execute("""
        SELECT message, sent_epoch, last_attempt_epoch
        FROM paper_v12_open_notifications WHERE trade_id=?
    """, (int(trade_id),)).fetchone()
    if row is None:
        return False  # operação antiga ou teste sem snapshot de odds
    message, sent_epoch, last_attempt = row
    if sent_epoch is not None:
        return True
    if last_attempt is not None and now_epoch - int(last_attempt) < retry_seconds:
        return False
    # Marcar tentativa ANTES do pedido HTTP para evitar tempestades após crash.
    with conn:
        claimed = conn.execute("""
            UPDATE paper_v12_open_notifications
            SET attempts=attempts+1, last_attempt_epoch=?
            WHERE trade_id=? AND sent_epoch IS NULL
              AND (last_attempt_epoch IS NULL OR last_attempt_epoch<=?)
        """, (now_epoch, int(trade_id), now_epoch - retry_seconds)).rowcount
    if claimed != 1:
        return False
    if not send_telegram_notice(message):
        return False
    with conn:
        conn.execute("""
            UPDATE paper_v12_open_notifications
            SET sent_epoch=?
            WHERE trade_id=? AND sent_epoch IS NULL
        """, (int(time.time()), int(trade_id)))
    return True


def retry_pending_open_notices(conn, limit=3):
    """Chamado periodicamente pelo monitor principal; exclui trades legados."""
    pending = conn.execute("""
        SELECT n.trade_id FROM paper_v12_open_notifications n
        JOIN paper_v12_trades t ON t.id=n.trade_id
        WHERE n.sent_epoch IS NULL
          AND t.status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED','SETTLED')
          AND (n.last_attempt_epoch IS NULL
               OR n.last_attempt_epoch <= ?)
        ORDER BY n.trade_id LIMIT ?
    """, (int(time.time()) - 60, limit)).fetchall()
    delivered = 0
    for (ident,) in pending:
        if send_open_once(conn, ident):
            delivered += 1
    return delivered


def send_settlement_once(conn, trade_id, now_epoch=None, retry_seconds=60):
    """Outbox durável: uma contabilização e entrega pelo menos uma vez.

    Um timeout depois de o Telegram aceitar pode duplicar o aviso numa nova
    tentativa. Não volta a liquidar nem a creditar carteiras.
    """
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    record = trade_details(conn, trade_id)
    if record is None or record["status"] != "SETTLED":
        raise ValueError("Só é possível notificar uma liquidação confirmada")
    if conn.execute(
        "SELECT 1 FROM paper_v12_notifications WHERE trade_id=?",
        (trade_id,),
    ).fetchone():
        return True
    row = conn.execute(
        "SELECT message,sent_epoch,last_attempt_epoch FROM paper_v12_settlement_notifications WHERE trade_id=?",
        (int(trade_id),),
    ).fetchone()
    if row is None:
        # Compatibilidade com fechos antigos: congela um texto que identifica
        # os saldos como atuais, sem inventar um snapshot do passado.
        message = format_settlement_message(conn, trade_id)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO paper_v12_settlement_notifications(trade_id,message) VALUES(?,?)",
                (int(trade_id), message),
            )
        row = conn.execute(
            "SELECT message,sent_epoch,last_attempt_epoch FROM paper_v12_settlement_notifications WHERE trade_id=?",
            (int(trade_id),),
        ).fetchone()
    message, sent_epoch, last_attempt = row
    if sent_epoch is not None:
        return True
    if last_attempt is not None and now_epoch - int(last_attempt) < retry_seconds:
        return False
    if conn.execute(
        "SELECT 1 FROM paper_v12_open_notifications WHERE trade_id=? AND sent_epoch IS NULL",
        (int(trade_id),),
    ).fetchone():
        return False  # a abertura pendente é entregue antes do respetivo fecho
    with conn:
        claimed = conn.execute(
            "UPDATE paper_v12_settlement_notifications SET attempts=attempts+1,last_attempt_epoch=? "
            "WHERE trade_id=? AND sent_epoch IS NULL "
            "AND (last_attempt_epoch IS NULL OR last_attempt_epoch<=?)",
            (now_epoch, int(trade_id), now_epoch - retry_seconds),
        ).rowcount
    if claimed != 1:
        return False
    if not send_telegram_notice(message):
        return False
    with conn:
        conn.execute(
            "UPDATE paper_v12_settlement_notifications SET sent_epoch=? WHERE trade_id=? AND sent_epoch IS NULL",
            (int(time.time()), int(trade_id)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO paper_v12_notifications(trade_id, sent_epoch) "
            "VALUES(?,?)",
            (trade_id, int(time.time())),
        )
    return True


def retry_pending_settlement_notices(conn, limit=3, now_epoch=None):
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    pending = conn.execute(
        "SELECT n.trade_id FROM paper_v12_settlement_notifications n "
        "JOIN paper_v12_trades t ON t.id=n.trade_id "
        "WHERE n.sent_epoch IS NULL AND t.status='SETTLED' "
        "AND (n.last_attempt_epoch IS NULL OR n.last_attempt_epoch<=?) "
        "AND NOT EXISTS(SELECT 1 FROM paper_v12_notifications done WHERE done.trade_id=n.trade_id) "
        "ORDER BY n.trade_id LIMIT ?",
        (now_epoch - 60, max(0, int(limit))),
    ).fetchall()
    return sum(bool(send_settlement_once(conn, ident, now_epoch=now_epoch)) for (ident,) in pending)


def _validate_resolution_evidence(record, poly_won, sx_won, evidence, allow_manual=False):
    if type(poly_won) is not bool or type(sx_won) is not bool:
        raise ValueError("Resultados devem ser booleanos explícitos; void e resultados ambíguos exigem revisão")
    if not isinstance(evidence, dict):
        raise ValueError("Prova de liquidação obrigatória")
    if allow_manual and evidence.get("method") == "manual_verified" and evidence.get("verified_both_rules") is True:
        return
    if evidence.get("method") != "official_api":
        raise ValueError("O fecho automático exige resultados confirmados nas duas fontes oficiais")
    poly, sx = evidence.get("poly"), evidence.get("sx")
    if not isinstance(poly, dict) or not isinstance(sx, dict):
        raise ValueError("Prova das duas plataformas obrigatória")
    for source, allowed_hosts in (
        (poly, {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com"}),
        (sx, {"api.sx.bet"}),
    ):
        url = urlparse(str(source.get("source_url") or ""))
        if (url.scheme != "https" or url.hostname not in allowed_hosts
                or url.username or url.password or url.fragment
                or source.get("resolved") is not True):
            raise ValueError("Fonte oficial ou resolução final inválida")
    if poly.get("resolution_status") != "onchain_payout" or sx.get("resolution_status") != "reported":
        raise ValueError("Estado de resolução final não confirmado")
    if (str(poly.get("market_id")) != str(record["poly_market"])
            or not record["poly_token_id"]
            or str(poly.get("token_id")) != str(record["poly_token_id"])
            or str(sx.get("market_hash", "")).lower() != str(record["sx_market"]).lower()
            or record["sx_outcome_one"] not in (0, 1)
            or type(sx.get("outcome_one")) is not bool
            or sx["outcome_one"] is not bool(record["sx_outcome_one"])
            or poly.get("won") is not poly_won or sx.get("won") is not sx_won):
        raise ValueError("A prova não corresponde aos mercados e às seleções guardados na operação")
    total = int(record["poly_debit_cents"]) + int(record["sx_debit_cents"])
    if total <= 0 or int(record["conditional_profit_cents"]) / total >= MAX_AUTO_PAPER_ROI:
        raise ValueError("Operação com margem histórica invulgar exige revisão manual")


def _settle_outcomes(conn, trade_id, poly_won, sx_won, evidence, now_epoch=None, allow_manual=False):
    """Só movimenta cêntimos virtuais, na mesma transação que a prova e outbox."""
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = trade_details(conn, trade_id)
        if row is not None and row["status"] == "SETTLED":
            previous_poly = bool(row["poly_won"]) if row["poly_won"] is not None else row["settled_side"] in ("poly", "both")
            previous_sx = bool(row["sx_won"]) if row["sx_won"] is not None else row["settled_side"] in ("sx", "both")
            if type(poly_won) is bool and type(sx_won) is bool and (previous_poly, previous_sx) == (poly_won, sx_won):
                conn.rollback()
                return None
            raise ValueError("Resultados contradizem uma liquidação já registada; nenhum saldo alterado")
        if row is None or row["status"] != "OPEN_CONDITIONAL":
            raise ValueError("Operação inexistente, bloqueada para revisão ou já liquidada")
        _validate_resolution_evidence(row, poly_won, sx_won, evidence, allow_manual=allow_manual)
        evidence_json = json.dumps(evidence, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(evidence_json.encode("utf-8")) > 50_000:
            raise ValueError("Prova de liquidação excessivamente grande")
        payout, poly_debit, sx_debit = (int(row[name]) for name in (
            "conditional_payout_cents", "poly_debit_cents", "sx_debit_cents"
        ))
        if min(payout, poly_debit, sx_debit) < 0:
            raise ValueError("Montantes virtuais inválidos; revisão necessária")
        poly_credit, sx_credit = (payout if won else 0 for won in (poly_won, sx_won))
        net = poly_credit + sx_credit - poly_debit - sx_debit
        before = free_balances(conn)
        for platform, credit in (("poly", poly_credit), ("sx", sx_credit)):
            conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents+? WHERE platform=?", (credit, platform))
        settled_side = "both" if poly_won and sx_won else "poly" if poly_won else "sx" if sx_won else "neither"
        conn.execute("""
          UPDATE paper_v12_trades
          SET status='SETTLED', settled_side=?, realized_profit_cents=?,
              settled_epoch=?, poly_won=?, sx_won=?, poly_credit_cents=?,
              sx_credit_cents=?, resolution_evidence_json=?
          WHERE id=?
        """, (settled_side, net, now_epoch, int(poly_won), int(sx_won),
              poly_credit, sx_credit, evidence_json, int(trade_id)))
        message = _settlement_message(trade_details(conn, trade_id), free_balances(conn), before)
        conn.execute(
            "INSERT INTO paper_v12_settlement_notifications(trade_id,message) VALUES(?,?)",
            (int(trade_id), message),
        )
        conn.commit()
        return net / 100
    except Exception:
        conn.rollback()
        raise


def settle_verified_outcomes(conn, trade_id, poly_won: bool, sx_won: bool,
                             evidence: dict, now_epoch=None):
    """Resultados oficiais independentes. Replay idêntico devolve None.

    O pagamento e câmbio usados são os guardados na simulação; não existe
    qualquer pedido de execução ou de movimentação de dinheiro real.
    """
    return _settle_outcomes(conn, trade_id, poly_won, sx_won, evidence, now_epoch)


def settle_normal(conn, trade_id, winning_platform, verified=False):
    """Compatibilidade: fecho manual após verificar resultado e regras."""
    if not verified:
        raise ValueError("É obrigatório confirmar as regras e o resultado em ambas as plataformas")
    if winning_platform not in ("poly", "sx"):
        raise ValueError("Vencedor deve ser 'poly' ou 'sx'. Exceções exigem revisão.")
    result = _settle_outcomes(
        conn, trade_id, winning_platform == "poly", winning_platform == "sx",
        {"method": "manual_verified", "verified_both_rules": True,
         "note": "Resultado e regras confirmados manualmente nas duas plataformas."},
        allow_manual=True,
    )
    if result is None:
        raise ValueError("Operação já liquidada; nenhum crédito repetido")
    return result


def flag_exception(conn, trade_id, reason):
    """Suspender a liquidação quando há desistência, anulação, adiamento, etc."""
    reason = str(reason).strip()
    if len(reason) < 5:
        raise ValueError("Motivo de revisão obrigatório")
    with conn:
        cursor = conn.execute("""
          UPDATE paper_v12_trades
          SET status='REVIEW_REQUIRED', note=?
          WHERE id=? AND status='OPEN_CONDITIONAL'
        """, (reason[:500], trade_id))
    if cursor.rowcount != 1:
        raise ValueError("Operação não aberta ou inexistente")


def try_paper_entry(conn, pair, poly_levels, sx_levels, fx, stage, skew,
                    *, valid_until_mono=None, live_evidence=None):
    """Um candidato de paper: NÃO chama nenhuma API de escrita."""
    if not _entry_guard_valid(pair, stage, valid_until_mono, live_evidence):
        return None
    if skew > MAX_SNAPSHOT_GAP_SECONDS:
        return None
    if pair["sx"]["sport"] not in ALLOWED_SPORTS:
        return None
    if fx <= 0 or fx >= 10:
        return None
    eid = event_key(pair)
    if conn.execute("SELECT 1 FROM paper_v12_trades WHERE event_key=?", (eid,)).fetchone():
        return None
    free = free_balances(conn)
    quote = select_shadow_quote(poly_levels, sx_levels, fx, free["poly"], free["sx"])
    if not quote:
        return None
    # Best asks na 2.ª leitura REST: não confundir com odd média garantida
    # de execução do stake. A mensagem explicita essa limitação.
    best_poly = min(price for price, _ in poly_levels)
    best_sx = min(price for price, _ in sx_levels)
    trade_id = book_shadow_trade(
        conn, pair, quote, stage,
        opening_odds=(1.0 / best_poly, 1.0 / best_sx),
        valid_until_mono=valid_until_mono,
        live_evidence=live_evidence,
    )
    return (trade_id, quote) if trade_id is not None else None


def self_test():
    import tempfile

    # Um livro sintético com ROI entre 0,5 e 1,5% tem de passar SOMENTE
    # no piloto, continuando a aplicar taxa, buffer e limite de 100 EUR.
    global MIN_NET_ROI
    saved_min = MIN_NET_ROI
    try:
        MIN_NET_ROI = 0.005
        pilot = select_shadow_quote(
            [(0.45, 300)], [(0.516, 300)], 0.91,
            100_000, 100_000,
        )
        assert pilot is not None and 0.005 <= pilot.roi < 0.015, pilot
        assert pilot.total_cents <= MAX_TRADE_CENTS
        MIN_NET_ROI = 0.015
        assert select_shadow_quote(
            [(0.45, 300)], [(0.516, 300)], 0.91,
            100_000, 100_000,
        ) is None
    finally:
        MIN_NET_ROI = saved_min

    # +47% deve ser rejeitado mesmo com liquidez aparente.
    assert select_shadow_quote(
        [(0.51, 300)], [(0.14625, 300)], 0.91, 100_000, 100_000
    ) is None

    q = select_shadow_quote([(0.43, 300)], [(0.50, 300)], 0.91, 100_000, 100_000)
    assert q and q.total_cents <= 10_000 and q.profit_eur > 0
    assert q.total_eur >= 5 and q.roi >= MIN_NET_ROI
    assert select_shadow_quote([(0.55, 300)], [(0.55, 300)], 0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 0.1)], [(0.50, 300)], 0.91, 100_000, 100_000) is None
    assert quote_for_payout([(0.43, 10)], [(0.50, 10)], 10, 0.91).poly_fee_cents > 0
    # Falha da segunda perna, perda de liquidez e spread adverso não podem
    # criar duas apostas fictícias com lucro.
    assert select_shadow_quote([(0.43, 300)], [], 0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 300)], [(0.60, 300)],
                               0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 300)], [(0.50, 300)],
                               0, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 300)], [(0.50, 300)],
                               0.91, 0, 100_000) is None

    sample = {
        "poly": {"id": "poly-1", "type": "moneyline",
                 "sport": "basketball", "start": 1800000000},
        "sx": {"hash": "sx-1", "sport": "basketball",
               "teams": ("Lakers", "Celtics"), "start": 1800000000},
        "poly_label": "Lakers", "sx_label": "Celtics",
    }
    with tempfile.TemporaryDirectory() as temp:
        db = open_database(str(Path(temp) / "paper.sqlite3"))
        ident = book_shadow_trade(
            db, sample, q, "PRE_JOGO", now_epoch=1000,
            opening_odds=(2.33, 2.0),
        )
        assert ident == 1
        assert book_shadow_trade(db, sample, q, "PRE_JOGO") is None
        # Duplicação, modalidade diferente, horário e tipo de mercado
        # nunca podem ser admitidos.
        bad = {**sample, "poly": {**sample["poly"], "type": "spread"}}
        assert book_shadow_trade(db, bad, q, "PRE_JOGO") is None
        bad = {**sample, "poly": {**sample["poly"], "sport": "tennis"}}
        assert book_shadow_trade(db, bad, q, "PRE_JOGO") is None
        bad = {**sample, "poly": {**sample["poly"], "start": 1800004000}}
        assert book_shadow_trade(db, bad, q, "PRE_JOGO") is None
        balances = free_balances(db)
        assert balances["poly"] == INITIAL_CENTS - q.poly_debit_cents
        assert balances["sx"] == INITIAL_CENTS - q.sx_debit_cents
        assert paper_summary(db)["open_trades"] == 1
        opening = format_open_message(db, ident, odds_poly=2.33, odds_sx=2.0)
        assert "Polymarket" in opening and "SX Bet" in opening
        assert "Valor retirado" in opening and "AGUARDA LIQUIDAÇÃO" in opening
        assert "Carteira Polymarket" in opening and "Carteira SX Bet" in opening
        stored = db.execute(
            "SELECT message, sent_epoch, attempts FROM paper_v12_open_notifications "
            "WHERE trade_id=?", (ident,)
        ).fetchone()
        assert stored and "Lakers vs Celtics" in stored[0]
        assert "odd 2,33" in stored[0] and stored[1] is None and stored[2] == 0
        original_send = send_telegram_notice
        attempts = []
        try:
            def mocked_send(msg):
                attempts.append(msg)
                return len(attempts) >= 2
            globals()["send_telegram_notice"] = mocked_send
            assert not send_open_once(db, ident, now_epoch=1001, retry_seconds=0)
            assert db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0] == 1
            assert send_open_once(db, ident, now_epoch=1002, retry_seconds=0)
            assert send_open_once(db, ident, now_epoch=1003, retry_seconds=0)
            assert len(attempts) == 2
            assert db.execute(
                "SELECT sent_epoch IS NOT NULL FROM paper_v12_open_notifications "
                "WHERE trade_id=?", (ident,)
            ).fetchone()[0] == 1
            assert retry_pending_open_notices(db) == 0
        finally:
            globals()["send_telegram_notice"] = original_send

        try:
            settle_normal(db, ident, "poly")
        except ValueError:
            pass
        else:
            raise AssertionError("Liquidação sem confirmação devia falhar")
        realized = settle_normal(db, ident, "poly", verified=True)
        assert round(realized * 100) == q.conditional_profit_cents
        assert paper_summary(db)["open_trades"] == 0
        assert paper_summary(db)["settled"] == 1
        assert paper_summary(db)["realized_eur"] == realized
        settling = format_settlement_message(db, ident)
        assert "Venceu: Lakers" in settling and "POLYMARKET" in settling
        assert "Resultado líquido" in settling
        assert "Carteira SX Bet" in settling
        assert db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0] == 1

        # Outro jogo: desta vez vence a SX Bet; apenas a SX recebe o prémio.
        opposite = {
            **sample,
            "poly": {**sample["poly"], "id": "poly-2"},
            "sx": {**sample["sx"], "hash": "sx-2"},
        }
        ident2 = book_shadow_trade(db, opposite, q, "PRE_JOGO")
        assert ident2 is not None
        before = free_balances(db)
        settle_normal(db, ident2, "sx", verified=True)
        after = free_balances(db)
        assert after["poly"] == before["poly"]
        assert after["sx"] == before["sx"] + q.payout_eur_cents
        text_result = format_settlement_message(db, ident2)
        assert "Venceu: Celtics" in text_result
        assert "Paga a carteira: SX BET" in text_result
        assert "SALDOS (antes → depois)" in text_result
        db.close()
    print("✅ PAPER V12 | matemática, carteiras, limite 100€, duplicados e liquidação OK")


def cli():
    parser = argparse.ArgumentParser(description="Shadow Paper V12 (sem apostas reais)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--settle-id", type=int)
    parser.add_argument("--winner", choices=["poly", "sx"])
    parser.add_argument("--verified-both-rules", action="store_true")
    parser.add_argument("--notify-open-id", type=int,
                        help="Tentar reenviar aviso de abertura pendente (sem duplicar aposta)")
    parser.add_argument("--pending-open-notices", action="store_true",
                        help="Ver avisos de abertura ainda não confirmados")
    parser.add_argument("--notify-settlement-id", type=int,
                        help="Reenviar notificação pendente (sem duplicações)")
    parser.add_argument("--list-open", action="store_true",
                        help="Listar operações simuladas abertas")
    parser.add_argument("--flag-exception", type=int)
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    conn = open_database()
    try:
        if args.settle_id is not None:
            gain = settle_normal(
                conn, args.settle_id, args.winner,
                verified=args.verified_both_rules
            )
            print(format_settlement_message(conn, args.settle_id))
            if not send_settlement_once(conn, args.settle_id):
                print("⚠️ Telegram não confirmou a mensagem. "
                      "Pode repetir com --notify-settlement-id.")
        elif args.notify_open_id is not None:
            ok = send_open_once(conn, args.notify_open_id, retry_seconds=0)
            print("✅ Aviso enviado ou já confirmado." if ok else
                  "⚠️ Aviso não confirmado. Confirma id e Telegram nos logs.")
        elif args.pending_open_notices:
            pending = conn.execute("""
                SELECT trade_id, attempts, last_attempt_epoch
                FROM paper_v12_open_notifications
                WHERE sent_epoch IS NULL ORDER BY trade_id
            """).fetchall()
            for trade_id, attempts, last in pending:
                print(f"#{trade_id} | tentativas={attempts} | último={last}")
            if not pending:
                print("✅ Sem avisos de abertura pendentes (não inclui trades antigos).")
        elif args.notify_settlement_id is not None:
            if not send_settlement_once(conn, args.notify_settlement_id):
                print("⚠️ Telegram indisponível. O resultado foi preservado no SQLite.")
            else:
                print("✅ Liquidação já notificada ou agora enviada.")
        elif args.list_open:
            for row in conn.execute(
                "SELECT id, event, poly_side, sx_side, status "
                "FROM paper_v12_trades "
                "WHERE status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED') "
                "ORDER BY id"
            ):
                print(f"#{row[0]} | {row[1]} | Poly={row[2]} | SX={row[3]} | {row[4]}")
        elif args.flag_exception is not None:
            flag_exception(conn, args.flag_exception, args.reason or "")
            print(f"Operação #{args.flag_exception} bloqueada para revisão")
        else:
            print(paper_summary(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
