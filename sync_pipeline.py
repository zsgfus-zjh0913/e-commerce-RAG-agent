"""
业务表与 data 文档 → 切块 → 向量/关键词索引 增量同步流水线。

核心逻辑：
  1. 对每张业务表（faq / shipping_policies / size_charts）
     读取当前所有行的 (id, updated_at, version) 快照。
  2. 与上次同步状态对比，识别：新增行 / 变更行 / 删除行。
  3. 对新增+变更行：切块 → 生成稳定 chunk_id → embed → 增量 upsert。
  4. 对删除行：按 source_id 删除旧 chunk。
  5. 将最新同步状态持久化到 sync_state.json。
  6. data 目录中的非数据库文档按文件指纹做同样的增量同步。

稳定 chunk_id 规则：
  {table_name}:{row_id}:{chunk_index}
  例如 product:1:0, faq:3:0, shipping_policy:2:1

source_id 规则：
  {table_name}:{row_id}
  例如 product:1, faq:3
  用于按来源整批删除/重建 chunk。
"""

import json
import time
from pathlib import Path

from database import (
    get_db, close_db, FAQ, ShippingPolicy, SizeChart, IS_SQLITE
)


SYNC_TABLES = {
    "faq": {
        "model": FAQ,
        "source_file": "faq.txt",
        "to_text": lambda r: r.to_text(),
    },
    "shipping_policy": {
        "model": ShippingPolicy,
        "source_file": "shipping_policy.md",
        "to_text": lambda r: r.content,
    },
    "size_chart": {
        "model": SizeChart,
        "source_file": "size_chart.csv",
        "to_text": lambda r: r.to_text(),
    },
}

DOCUMENT_STATE_KEY = "_documents"
MANAGED_SOURCE_FILES = {
    cfg["source_file"] for cfg in SYNC_TABLES.values()
}


class SyncPipeline:
    """增量同步：业务表 → 切块 → 检索存储。"""

    def __init__(self, rag_engine, index_dir="index"):
        self.rag = rag_engine
        self.index_dir = Path(index_dir)
        self.state_path = self.index_dir / "sync_state.json"
        self._state = {}
        self._load_state()

    def _load_state(self):
        if self.state_path.exists():
            self._state = json.loads(
                self.state_path.read_text(encoding="utf-8")
            )
        else:
            self._state = {}

    def _save_state(self):
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_row_fingerprint(self, row):
        """提取行的同步指纹：(updated_at, version)。"""
        return (
            getattr(row, "updated_at", 0) or 0,
            getattr(row, "version", 1) or 1,
        )

    def _make_chunks_for_row(self, table_key, row, split_fn):
        """将单行数据切块，生成带稳定 chunk_id 的 chunk 列表。"""
        cfg = SYNC_TABLES[table_key]
        text = cfg["to_text"](row)
        source_file = cfg["source_file"]
        source_id = f"{table_key}:{row.id}"

        raw_chunks = split_fn(text, source_file, {
            "source": source_file,
            "source_id": source_id,
            "type": "database",
            "table": table_key,
            "row_id": row.id,
        })

        chunks = []
        for i, chunk in enumerate(raw_chunks):
            chunk["chunk_id"] = f"{table_key}:{row.id}:{i}"
            chunk["source_id"] = source_id
            if "meta" not in chunk:
                chunk["meta"] = {}
            chunk["meta"]["table"] = table_key
            chunk["meta"]["row_id"] = row.id
            chunk["meta"]["chunk_index"] = i
            chunks.append(chunk)
        return chunks

    def sync_table(self, table_key):
        """同步单张表，返回 (added, updated, deleted) 计数。"""
        cfg = SYNC_TABLES[table_key]
        model = cfg["model"]
        table_state = self._state.get(table_key, {})

        db = get_db()
        added = updated = deleted = 0
        try:
            rows = db.query(model).all()
            current_ids = {str(r.id) for r in rows}
            known_ids = set(table_state.keys())

            for row in rows:
                fp = self._get_row_fingerprint(row)
                old_fp = table_state.get(str(row.id))

                if old_fp is None:
                    chunks = self._make_chunks_for_row(
                        table_key, row, self.rag._split_chunks
                    )
                    self.rag._add_chunks_to_store(chunks)
                    added += len(chunks)
                    table_state[str(row.id)] = list(fp)
                elif list(fp) != old_fp:
                    source_id = f"{table_key}:{row.id}"
                    self.rag.store.delete_by_source(source_id)
                    chunks = self._make_chunks_for_row(
                        table_key, row, self.rag._split_chunks
                    )
                    self.rag._add_chunks_to_store(chunks)
                    updated += len(chunks)
                    table_state[str(row.id)] = list(fp)

            deleted_ids = known_ids - current_ids
            for did in deleted_ids:
                source_id = f"{table_key}:{did}"
                if self.rag.store.delete_by_source(source_id):
                    deleted += 1
                if str(did) in table_state:
                    del table_state[str(did)]

        finally:
            close_db(db)

        self._state[table_key] = table_state
        return added, updated, deleted

    def sync_documents(self):
        """同步 data 目录中手工放入的文档，返回 (added, updated, deleted)。"""
        from rag_engine import SUPPORTED_EXTENSIONS

        file_state = self._state.setdefault(DOCUMENT_STATE_KEY, {})
        current_files = {}
        for filepath in sorted(self.rag.data_dir.iterdir()):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if filepath.name in MANAGED_SOURCE_FILES:
                continue
            stat = filepath.stat()
            current_files[filepath.name] = {
                "path": filepath,
                "fingerprint": [stat.st_mtime_ns, stat.st_size],
            }

        added = updated = deleted = 0
        for filename, current in current_files.items():
            old_fingerprint = file_state.get(filename)
            if old_fingerprint == current["fingerprint"]:
                continue

            filepath = current["path"]
            self.rag.store.delete_by_source(filename)
            try:
                docs = self.rag._load_file(filepath)
            except Exception as exc:
                print(f"[RAG] 文档解析失败，跳过 {filename}: {exc}")
                continue

            chunks = []
            for text, meta in docs:
                raw_chunks = self.rag._split_chunks(text, filename, meta)
                for i, chunk in enumerate(raw_chunks):
                    chunk["chunk_id"] = f"document:{filename}:{i}"
                    chunk["source_id"] = filename
                    chunk.setdefault("meta", {})["document_index"] = i
                    chunks.append(chunk)

            self.rag._add_chunks_to_store(chunks)
            chunk_count = len(chunks)
            if old_fingerprint is None:
                added += chunk_count
            else:
                updated += chunk_count
            file_state[filename] = current["fingerprint"]

        for filename in list(file_state):
            if filename in current_files:
                continue
            if self.rag.store.delete_by_source(filename):
                deleted += 1
            del file_state[filename]

        self._state[DOCUMENT_STATE_KEY] = file_state
        return added, updated, deleted

    def sync_all(self):
        """同步所有业务表和 data 目录文档。返回汇总报告。"""
        report = {}
        total_added = total_updated = total_deleted = 0

        valid_keys = set(SYNC_TABLES) | {DOCUMENT_STATE_KEY}
        for table_key in list(self._state):
            if table_key in valid_keys:
                continue
            table_state = self._state.pop(table_key, {}) or {}
            for row_id in table_state:
                if self.rag.store.delete_by_source(f"{table_key}:{row_id}"):
                    total_deleted += 1

        for table_key in SYNC_TABLES:
            a, u, d = self.sync_table(table_key)
            report[table_key] = {"added": a, "updated": u, "deleted": d}
            total_added += a
            total_updated += u
            total_deleted += d

        file_added, file_updated, file_deleted = self.sync_documents()
        total_added += file_added
        total_updated += file_updated
        total_deleted += file_deleted

        self._save_state()

        if total_added or total_updated or total_deleted:
            self.rag.store.persist()

        report["total"] = {
            "added": total_added,
            "updated": total_updated,
            "deleted": total_deleted,
            "chunks": self.rag.store.count(),
        }
        report["synced_at"] = int(time.time())
        return report

    def full_resync(self):
        """强制全量重同步：清空状态后重新同步。"""
        self._state = {}
        self._save_state()
        return self.sync_all()
