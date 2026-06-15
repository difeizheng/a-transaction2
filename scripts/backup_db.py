"""SQLite 数据库在线备份脚本。

用 sqlite3.backup() API 做在线备份（源库可继续读写，不阻塞 Streamlit/批量更新），
比直接 cp 文件更安全（cp 可能拷到写入中途的不一致状态）。

用法：
    python scripts/backup_db.py                    # 默认备份 data/stock.db，保留 10 份
    python scripts/backup_db.py --keep 20          # 保留 20 份
    python scripts/backup_db.py --db custom.db     # 指定数据库

建议配合 Windows 任务计划程序 / cron 定时执行（如每日收盘后）。
"""
import argparse
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = _PROJECT_ROOT / "data" / "stock.db"
DEFAULT_BACKUP_DIR = _PROJECT_ROOT / "data" / "backup"
DEFAULT_KEEP = 10


def backup_database(
    db_path: Path = DEFAULT_DB,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    keep: int = DEFAULT_KEEP,
) -> Path:
    """在线备份 SQLite 数据库，保留最近 keep 份。

    :returns: 新备份文件路径
    :raises FileNotFoundError: 源数据库不存在
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)

    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"stock_{stamp}.db"

    # 在线备份：源库连接期间可继续被其他进程读写
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    size_kb = dest.stat().st_size / 1024
    logger.info(f"备份完成: {dest.name} ({size_kb:.0f} KB)")

    # 清理旧备份，按文件名时间戳排序，保留最近 keep 份
    backups = sorted(backup_dir.glob("stock_*.db"))
    if len(backups) > keep:
        for old in backups[: len(backups) - keep]:
            old.unlink()
            logger.info(f"清理旧备份: {old.name}")

    return dest


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="SQLite 数据库在线备份")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="源数据库路径")
    parser.add_argument("--dir", default=str(DEFAULT_BACKUP_DIR), help="备份目录")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="保留备份数")
    args = parser.parse_args()
    backup_database(Path(args.db), Path(args.dir), args.keep)


if __name__ == "__main__":
    main()
