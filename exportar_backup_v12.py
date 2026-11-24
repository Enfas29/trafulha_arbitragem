"""Exporta snapshots SQLite consistentes, fora do volume persistente.
Sem limpeza, sem apostas, sem alterações ao plano.
Executar no serviço em manutenção e descarregar a pasta antes de redeploy.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

MIB = 1024 * 1024


def snapshot(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file() or target.exists():
        raise RuntimeError("Origem inexistente ou destino já existente")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            dst.commit()
            if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
                raise RuntimeError("Não foi possível consolidar o snapshot")
            check = dst.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise RuntimeError("Snapshot com erro de integridade")
        dst.close()
    src.close()
    with target.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    return digest


def main():
    if os.getenv("TRAFLUHA_STORAGE_MAINTENANCE", "").strip().lower() not in {"1", "true", "yes"}:
        raise SystemExit("BLOQUEADO: ativa primeiro o modo de manutenção.")
    monitor = Path(os.environ["MONITOR_DB"]).resolve()
    paper = Path(os.environ["PAPER_V12_DB"]).resolve()
    if monitor == paper or not all(p.is_file() for p in (monitor, paper)):
        raise RuntimeError("Bases de dados ausentes ou não separadas.")
    root = Path("/tmp").resolve()
    if any(root == p.parent or root.is_relative_to(p.parent) for p in (monitor, paper)):
        raise RuntimeError("Destino temporário inválido.")
    if any(root.stat().st_dev == p.parent.stat().st_dev for p in (monitor, paper)):
        raise RuntimeError("/tmp não está num sistema de ficheiros separado.")
    size = sum(f.stat().st_size for p in (monitor, paper)
               for f in (p, Path(str(p) + "-wal")) if f.exists())
    if shutil.disk_usage(root).free < size + 64 * MIB:
        raise RuntimeError("Espaço temporário insuficiente; nenhuma base original modificada.")
    folder = Path(tempfile.mkdtemp(prefix="trafulha-backup-", dir=root))
    records = []
    for src, name in ((monitor, "monitor.sqlite3"), (paper, "paper.sqlite3")):
        dst = folder / name
        digest = snapshot(src, dst)
        records.append(f"{digest}  {name}")
        print(f"{name}: {dst.stat().st_size / MIB:.2f} MiB; quick_check=ok; SHA256={digest}")
    (folder / "SHA256SUMS.txt").write_text("\n".join(records) + "\n", encoding="ascii")
    print(f"BACKUP_PRONTO: {folder}")
    print("Descarregar a pasta completa. Não limpar dados antes de verificar a cópia local.")


if __name__ == "__main__":
    main()
