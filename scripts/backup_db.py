"""数据库定时备份脚本（阶段 4 运维）。

用法：
    # SQLite（默认，开发/单机部署）
    python scripts/backup_db.py --out-dir D:/backup/lawagent --keep-days 7

    # PostgreSQL（生产）
    python scripts/backup_db.py --pg --out-dir /backup/lawagent --keep-days 30

    # 只看会做哪些事，不落盘
    python scripts/backup_db.py --dry-run

设计要点：
- SQLite 用官方 `Connection.backup()` 取一致性快照（WAL 模式下也能拿对），
  再把快照 gzip；**绝不直接 copy 正在写的 .db 文件**（可能拷到半个事务）。
- 备份文件格式：`<库名>-YYYYmmdd-HHMMSS.db.gz`，恢复 = gunzip 回原文件名。
- `--keep-days` 按文件 mtime 清理，0 表示只留本次。
- 同时备份业务库和 LangGraph checkpointer 库（HITL 挂起状态在里面，
  只备份业务库的话重启后合伙人待审任务会丢）。

crontab 示例（每天 02:30）：
    30 2 * * * cd /srv/lawagent && /usr/bin/python3 scripts/backup_db.py --pg --keep-days 30 >> /var/log/lawagent-backup.log 2>&1
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 允许直接 python scripts/backup_db.py
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.config import DATA_DIR, settings  # noqa: E402


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _gzip_file(src: Path, dst: Path) -> Path:
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    return dst


def backup_sqlite_file(src: Path | str, out_dir: Path | str) -> Path | None:
    """一致性快照 + gzip。源文件不存在返回 None（首次启动还没建库时不报错）。"""
    src = Path(src)
    out_dir = Path(out_dir)
    if not src.exists():
        print(f"  跳过（库文件不存在）：{src}")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot = out_dir / f".{src.stem}.snapshot"
    # 用普通连接（不用 file: URI）：Windows 盘符路径在 URI 里容易解析出岔，
    # 而 backup() 本身是并发安全的，不需要只读模式。
    # 注意：连接必须显式 close —— `with sqlite3.connect()` 只提交事务不关连接，
    # Windows 上不关就删不掉快照文件（WinError 32）。
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(snapshot)
    try:
        src_conn.backup(dst_conn)  # 官方备份 API：拿到的是事务一致的状态
    finally:
        dst_conn.close()
        src_conn.close()

    target = out_dir / f"{src.stem}-{_stamp()}.db.gz"
    _gzip_file(snapshot, target)
    snapshot.unlink(missing_ok=True)
    print(f"  SQLite 备份完成：{target}（{target.stat().st_size / 1024:.0f} KB）")
    return target


def backup_postgres(out_dir: Path, database_url: str) -> Path | None:
    """pg_dump 自定义格式备份（恢复用 pg_restore）。缺 pg_dump 时给明确提示。"""
    if shutil.which("pg_dump") is None:
        print("  [失败] 未找到 pg_dump，请安装 postgresql-client 或把 pg_dump 加进 PATH")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{settings.db_name}-{_stamp()}.dump"
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--file", str(target), database_url]
    print(f"  执行：pg_dump → {target}")
    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if proc.returncode != 0:
        print(f"  [失败] pg_dump 返回 {proc.returncode}：{proc.stderr.strip()[:300]}")
        target.unlink(missing_ok=True)
        return None
    print(f"  PostgreSQL 备份完成：{target}（{target.stat().st_size / 1024:.0f} KB）")
    return target


def prune_old(out_dir: Path, keep_days: int) -> list[Path]:
    """删除 mtime 早于 keep_days 天的备份。

    keep_days <= 0 一律不删 —— 清理是在备份之后跑的，如果 0 表示「删掉所有旧的」，
    就会把刚生成的备份一起删掉（真实踩过的坑）。
    """
    if not out_dir.exists() or keep_days <= 0:
        return []
    cutoff = time.time() - keep_days * 86400
    removed: list[Path] = []
    for p in out_dir.iterdir():
        if p.is_file() and p.suffix in (".gz", ".dump") and p.stat().st_mtime < cutoff:
            p.unlink()
            removed.append(p)
    for p in removed:
        print(f"  清理过期备份：{p.name}")
    return removed


def _sqlite_paths() -> list[Path]:
    business = Path(settings.sqlite_path) if settings.sqlite_path else DATA_DIR / "lawagent.db"
    paths = [business]
    if settings.checkpointer_backend == "sqlite":
        ckpt = Path(settings.checkpointer_path) if settings.checkpointer_path else DATA_DIR / "checkpoints.db"
        paths.append(ckpt)
    # 去重且保持顺序
    return list(dict.fromkeys(paths))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LawAgent 数据库备份")
    ap.add_argument("--out-dir", default=str(PROJECT_ROOT / "backups"), help="备份输出目录")
    ap.add_argument("--keep-days", type=int, default=7, help="保留天数（<=0 表示不清理）")
    ap.add_argument("--pg", action="store_true", help="备份 PostgreSQL（用 pg_dump）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    print(f"=== LawAgent 备份 {_stamp()} ===")
    print(f"输出目录：{out_dir}｜保留：{args.keep_days} 天｜模式：{'PostgreSQL' if args.pg else 'SQLite'}")

    if args.dry_run:
        if args.pg:
            print(f"  [dry-run] 将执行 pg_dump → {out_dir}（库 {settings.db_name}）")
        else:
            for p in _sqlite_paths():
                print(f"  [dry-run] 将备份 {p} → {out_dir}")
        print(f"  [dry-run] 将清理 {args.keep_days} 天前的备份")
        return 0

    made: list[Path] = []
    if args.pg:
        url = settings.database_url
        got = backup_postgres(out_dir, url)
        if got:
            made.append(got)
    else:
        for p in _sqlite_paths():
            got = backup_sqlite_file(p, out_dir)
            if got:
                made.append(got)

    prune_old(out_dir, args.keep_days)

    if not made:
        print("=== 本次没有产生备份，请检查上面的提示 ===")
        return 1
    print(f"=== 完成：{len(made)} 个备份 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
