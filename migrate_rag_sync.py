"""
迁移脚本：给 products / faq / shipping_policies / size_charts 表
添加 updated_at 和 version 列，并初始化值。
同时清除旧的 RAG 索引，使下次启动时触发全量增量同步。
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "ecommerce.db"
INDEX_DIR = Path(__file__).parent / "index"


def migrate():
    if not DB_PATH.exists():
        print("[迁移] 数据库不存在，跳过")
        return

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    tables = ["products", "faq", "shipping_policies", "size_charts"]
    now = int(time.time())

    for table in tables:
        cols = [row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()]
        print(f"[迁移] {table} 现有列: {cols}")

        if "updated_at" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN updated_at INTEGER DEFAULT 0")
            c.execute(f"UPDATE {table} SET updated_at = created_at WHERE updated_at = 0")
            print(f"  -> 添加 updated_at 列，初始化为 created_at")
        else:
            print(f"  -> updated_at 列已存在")

        if "version" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN version INTEGER DEFAULT 1")
            print(f"  -> 添加 version 列，默认值 1")
        else:
            print(f"  -> version 列已存在")

    conn.commit()
    conn.close()
    print("[迁移] 数据库迁移完成")

    for f in ["chunks.json", "embeddings.npy", "doc_hashes.json", "sync_state.json"]:
        p = INDEX_DIR / f
        if p.exists():
            p.unlink()
            print(f"[迁移] 清除旧索引: {f}")

    print("[迁移] 旧索引已清除，下次启动将触发全量同步")
    print("[迁移] 完成!")


if __name__ == "__main__":
    migrate()
