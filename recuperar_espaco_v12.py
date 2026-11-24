"""Recuperacao CONTROLADA do historico do monitor V11/V12.

Por defeito apenas inspeciona. NUNCA modifica a base Paper V12.
Executar --apply APENAS depois de criar/verificar backup Railway do volume.
Executar sem outro monitor a escrever na mesma SQLite.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = os.getenv("MONITOR_DB", "/data/monitor_v11.sqlite3")
PAPER_DB = os.getenv("PAPER_V12_DB", "/data/paper_v12.sqlite3")
MAX_ROWS = 25000
KEEP_HOURS = 12
BATCH = 1000
MIN_FREE_BYTES = 150 * 1024 * 1024


def files_and_space(path: Path):
    candidates = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
    existing = [(p.name, p.stat().st_size) for p in candidates if p.exists()]
    disk = shutil.disk_usage(path.parent)
    return existing, disk.free, disk.total


def observations_info(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'").fetchone() is None:
        raise RuntimeError("Tabela observations ausente: base de dados inesperada")
    n, oldest, newest = conn.execute(
        "SELECT COUNT(*), MIN(observed_at), MAX(observed_at) FROM observations"
    ).fetchone()
    return int(n), oldest, newest


def clean_database(db_path, *, apply=False, backup_confirmed=False,
                   keep_hours=KEEP_HOURS, max_rows=MAX_ROWS,
                   min_free=MIN_FREE_BYTES):
    path = Path(db_path).expanduser().resolve()
    if path == Path(PAPER_DB).expanduser().resolve():
        raise RuntimeError("BLOQUEADO: nunca usar a base PAPER_V12_DB")
    if not path.is_file():
        raise FileNotFoundError(f"Monitor DB nao encontrada: {path}")
    sizes, free, total = files_and_space(path)
    print(f"MONITOR DB: {path}")
    print(f"ESPACO: livre={free/1048576:.1f} MiB / total={total/1048576:.1f} MiB")
    for name, size in sizes:
        print(f"  {name}: {size/1048576:.1f} MiB")
    if apply and not backup_confirmed:
        raise RuntimeError("BLOQUEADO: criar e verificar backup Railway antes de --apply")
    if apply and free < min_free:
        raise RuntimeError(
            f"BLOQUEADO: livres {free/1048576:.1f} MiB; "
            f"precisa de pelo menos {min_free/1048576:.0f} MiB. "
            "Aumentar capacidade do volume antes da limpeza."
        )
    # Abertura read-only no dry-run: sem criar ficheiros nem alterar WAL.
    if apply:
        conn = sqlite3.connect(str(path), timeout=30)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        n, oldest, newest = observations_info(conn)
        print(f"OBSERVATIONS: {n} leituras; mais antiga={oldest}; mais recente={newest}")
        print(f"POLITICA: ultimas {keep_hours} horas, maximo {max_rows} linhas")
        if not apply:
            print("MODO SOMENTE LEITURA. Nada apagado nem compactado.")
            return {"before": n, "after": n, "deleted": 0}
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_hours)).isoformat(
            timespec="seconds"
        )
        deleted = 0
        # Evitar transacoes enormes; checkpoint entre blocos para limitar WAL.
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA wal_autocheckpoint=100")
        for _ in range(50000):
            excess_id = conn.execute(
                "SELECT id FROM observations ORDER BY id DESC LIMIT 1 OFFSET ?",
                (max_rows,),
            ).fetchone()
            # Eliminar apenas linhas antigas OU excedentes ao limite.
            boundary = excess_id[0] if excess_id else -1
            with conn:
                cur = conn.execute(
                    "DELETE FROM observations WHERE id IN ("
                    "SELECT id FROM observations "
                    "WHERE observed_at < ? OR id <= ? ORDER BY id LIMIT ?"
                    ")",
                    (cutoff, boundary, BATCH),
                )
            removed = max(0, cur.rowcount)
            deleted += removed
            if removed == 0:
                break
            # No arranque de recuperacao nao deve haver segundo escritor.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            raise RuntimeError("Limite de blocos ultrapassado; verificar integridade do DB")

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_vacuum = path.stat().st_size
        _, available, _ = files_and_space(path)
        # VACUUM precisa de espaço temporario; nao executar se insuficiente.
        if available >= int(before_vacuum * 1.5) + (50 * 1024 * 1024):
            print("VACUUM: a compactar historico (Paper V12 NAO sera tocado)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            print("VACUUM: adiado por falta de espaco temporario; nenhum risco extra")
        conn.execute("PRAGMA optimize")
        after, *_ = observations_info(conn)
        verify = conn.execute("PRAGMA quick_check").fetchone()[0]
        if verify != "ok":
            raise RuntimeError(f"PRAGMA quick_check falhou: {verify}")
        _, final_free, _ = files_and_space(path)
        print(
            f"RECUPERACAO OK: linhas {n} -> {after} (removidas {deleted}); "
            f"livre {final_free/1048576:.1f} MiB; quick_check=ok"
        )
        return {"before": n, "after": after, "deleted": deleted}
    finally:
        conn.close()


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "monitor.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE observations (id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL)"
        )
        old = (datetime.now(timezone.utc)-timedelta(hours=20)).isoformat(timespec="seconds")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.executemany("INSERT INTO observations(observed_at) VALUES (?)",
                         [(old,)] * 20 + [(now,)] * 30)
        conn.commit()
        conn.close()
        dry = clean_database(path, keep_hours=12, max_rows=10)
        assert dry["before"] == 50 and dry["after"] == 50
        try:
            clean_database(path, apply=True, keep_hours=12, max_rows=10, min_free=0)
        except RuntimeError as err:
            assert "backup" in str(err)
        else:
            raise AssertionError("Deveria recusar limpeza sem backup")
        result = clean_database(path, apply=True, backup_confirmed=True,
                                keep_hours=12, max_rows=10, min_free=0)
        assert result["after"] == 10 and result["deleted"] == 40
    print("OK: recuperacao SQLite, limite, dry-run e bloqueio sem backup")


def main():
    parser = argparse.ArgumentParser(description="Limpeza segura do histórico do monitor")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    clean_database(args.db, apply=args.apply,
                   backup_confirmed=args.backup_confirmed)


if __name__ == "__main__":
    main()
