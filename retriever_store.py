"""
检索存储抽象层 — 供 RAGEngine 使用，隔离底层向量/关键词索引实现。

接口：
  - add_chunks(chunks, embeddings)       增量插入/更新
  - delete_by_source(source_id)          按来源删除
  - search(query_embedding, tokenized_query, top_k, threshold)  混合检索
  - persist() / load()                    磁盘持久化
  - count()                              当前 chunk 总数

默认实现：NumpyTfidfStore（numpy 向量 + sklearn TF-IDF）
预留实现：QdrantStore（Qdrant 向量数据库，生产环境启用）
"""

import os
import json
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class RetrieverStore(ABC):
    """检索存储抽象基类。"""

    @abstractmethod
    def add_chunks(self, chunks, embeddings=None):
        """增量添加 chunk。若 chunk_id 已存在则覆盖旧数据。"""

    @abstractmethod
    def delete_by_source(self, source_id):
        """删除指定 source_id 的所有 chunk。"""

    @abstractmethod
    def search(self, query_embedding=None, tokenized_query=None, top_k=5, threshold=0.0):
        """混合检索：向量 + TF-IDF。返回 [{text, source, score, meta, chunk_id}]。"""

    @abstractmethod
    def persist(self):
        """持久化到磁盘。"""

    @abstractmethod
    def load(self):
        """从磁盘加载。返回 True/False。"""

    @abstractmethod
    def count(self):
        """返回当前 chunk 总数。"""


class NumpyTfidfStore(RetrieverStore):
    """numpy 向量 + sklearn TF-IDF 的默认实现。"""

    def __init__(self, index_dir="index"):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.chunks = []
        self.embeddings = None
        self.vectorizer = None
        self.tfidf_matrix = None
        self._chunk_index = {}  # chunk_id -> list index
        self.supports_vector = True
        self.supports_lexical = True
        self.external_vectors = False

    def _rebuild_index_map(self):
        self._chunk_index = {}
        for i, c in enumerate(self.chunks):
            cid = c.get("chunk_id", "")
            if cid not in self._chunk_index:
                self._chunk_index[cid] = []
            self._chunk_index[cid].append(i)

    def add_chunks(self, chunks, embeddings=None):
        if not chunks:
            return

        new_emb_rows = []
        for i, chunk in enumerate(chunks):
            cid = chunk.get("chunk_id", "")
            emb = embeddings[i] if embeddings is not None else None
            existing = self._chunk_index.get(cid) if cid else None
            if cid and existing:
                # 更新：原地替换 chunk，并同步替换对应嵌入行（不增加行数）
                for idx in existing:
                    self.chunks[idx] = chunk
                    if (
                        emb is not None
                        and self.embeddings is not None
                        and idx < len(self.embeddings)
                    ):
                        self.embeddings[idx] = emb
            else:
                # 新增：chunk 与嵌入行同步追加
                self.chunks.append(chunk)
                if cid:
                    if cid not in self._chunk_index:
                        self._chunk_index[cid] = []
                    self._chunk_index[cid].append(len(self.chunks) - 1)
                if emb is not None:
                    new_emb_rows.append(emb)

        if new_emb_rows:
            if self.embeddings is not None and len(self.embeddings) > 0:
                self.embeddings = np.vstack([self.embeddings, np.array(new_emb_rows)])
            else:
                self.embeddings = np.array(new_emb_rows)

        self._build_tfidf()

    def delete_by_source(self, source_id):
        old_len = len(self.chunks)
        keep_mask = [c.get("source_id", c.get("source", "")) != source_id
                     for c in self.chunks]
        self.chunks = [c for i, c in enumerate(self.chunks) if keep_mask[i]]

        if self.embeddings is not None and len(self.embeddings) > 0:
            self.embeddings = self.embeddings[keep_mask]

        removed = old_len - len(self.chunks)
        if removed > 0:
            self._build_tfidf()
            self._rebuild_index_map()
        return removed > 0

    def _build_tfidf(self):
        if not self.chunks:
            self.vectorizer = None
            self.tfidf_matrix = None
            return
        from rag_engine import tokenize
        corpus = [tokenize(c["text"]) for c in self.chunks]
        self.vectorizer = TfidfVectorizer(ngram_range=(1, 2))
        self.tfidf_matrix = self.vectorizer.fit_transform(corpus)

    def search(self, query_embedding=None, tokenized_query=None, top_k=5, threshold=0.0):
        if not self.chunks:
            return []

        vec_scores = None
        if query_embedding is not None and self.embeddings is not None:
            vec_scores = (self.embeddings @ query_embedding.T).flatten()

        tfidf_scores = None
        if self.vectorizer is not None and tokenized_query is not None:
            q_vec = self.vectorizer.transform([tokenized_query])
            tfidf_scores = cosine_similarity(q_vec, self.tfidf_matrix).flatten()

        if vec_scores is not None and tfidf_scores is not None:
            scores = 0.7 * vec_scores + 0.3 * tfidf_scores
        elif vec_scores is not None:
            scores = vec_scores
        elif tfidf_scores is not None:
            scores = tfidf_scores
        else:
            return []

        candidate_k = min(top_k * 3, len(self.chunks))
        ranked = np.argsort(scores)[::-1]
        results = []
        for idx in ranked[:candidate_k]:
            score = float(scores[idx])
            if score < threshold:
                continue
            chunk = self.chunks[idx]
            results.append({
                "text": chunk["text"],
                "source": chunk.get("source", ""),
                "source_id": chunk.get("source_id", ""),
                "score": round(score, 4),
                "meta": chunk.get("meta", {}),
                "chunk_id": chunk.get("chunk_id", ""),
                "index": int(idx),
            })
        return results[:top_k]

    def persist(self):
        if self.embeddings is not None:
            np.save(str(self.index_dir / "embeddings.npy"), self.embeddings)
        (self.index_dir / "chunks.json").write_text(
            json.dumps(self.chunks, ensure_ascii=False), encoding="utf-8"
        )

    def load(self):
        chunks_path = self.index_dir / "chunks.json"
        emb_path = self.index_dir / "embeddings.npy"
        if not chunks_path.exists():
            return False
        self.chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
        if emb_path.exists():
            self.embeddings = np.load(str(emb_path))
        self._rebuild_index_map()
        self._build_tfidf()
        return True

    def count(self):
        return len(self.chunks)


class QdrantStore(RetrieverStore):
    """
    Qdrant 向量数据库实现（生产环境启用）。

    两种连接方式（按环境变量优先取用）：
      - QDRANT_URL          远程服务，如 http://localhost:6333
      - QDRANT_LOCAL_PATH   本地嵌入模式，无需 Docker，数据落本地目录

    向量存放在 Qdrant 内部，RAGEngine 的 add_chunks 传入 embeddings 时
    由 Qdrant 完成持久化，本进程不驻留内存向量。
    """

    def __init__(self, index_dir="index", collection_name="ecommerce_rag", vector_size=512):
        self.index_dir = Path(index_dir)
        self.collection_name = collection_name
        self.vector_size = vector_size
        self.chunks = []        # 接口对齐：仅内存镜像，检索以 Qdrant 为准
        self.embeddings = None  # 接口对齐：向量在 Qdrant 内，不驻留内存
        self.vectorizer = None
        self.tfidf_matrix = None
        self._client = None
        self.supports_vector = True
        self.supports_lexical = False
        self.external_vectors = True
        self._init_client()

    def _init_client(self):
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
            url = os.environ.get("QDRANT_URL", "")
            local_path = os.environ.get("QDRANT_LOCAL_PATH", "")
            if not url and not local_path:
                return
            if local_path:
                Path(local_path).mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=local_path)
                desc = f"本地嵌入模式: {local_path}"
            else:
                self._client = QdrantClient(url=url)
                desc = f"远程服务: {url}"
            if not self._client.collection_exists(self.collection_name):
                self._client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size, distance=Distance.COSINE
                    ),
                )
            print(f"[QdrantStore] 连接成功({desc}) collection={self.collection_name}")
        except ImportError:
            print("[QdrantStore] qdrant-client 未安装，回退到 NumpyTfidfStore")
        except Exception as e:
            print(f"[QdrantStore] 连接失败: {e}")

    @property
    def available(self):
        return self._client is not None

    @staticmethod
    def _point_id(chunk_id):
        # Python 内置 hash() 受 PYTHONHASHSEED 随机化影响跨进程不稳定，改用 md5 稳定映射
        return int(hashlib.md5(chunk_id.encode("utf-8")).hexdigest()[:16], 16)

    def add_chunks(self, chunks, embeddings=None):
        if not self.available or not chunks or embeddings is None:
            return
        from qdrant_client.models import PointStruct
        points = []
        for i, chunk in enumerate(chunks):
            cid = chunk.get("chunk_id") or f"{chunk.get('source', '')}:{i}"
            points.append(PointStruct(
                id=self._point_id(cid),
                vector=embeddings[i].tolist(),
                payload={
                    "text": chunk["text"],
                    "source": chunk.get("source", ""),
                    "source_id": chunk.get("source_id", ""),
                    "chunk_id": cid,
                    "meta": chunk.get("meta", {}),
                },
            ))
            self._mirror_chunk(chunk)
        self._client.upsert(collection_name=self.collection_name, points=points)

    def _mirror_chunk(self, chunk):
        """内存镜像：按 chunk_id 覆盖，保持 engine.chunks 接口语义。"""
        cid = chunk.get("chunk_id", "")
        if not cid:
            return
        for i, c in enumerate(self.chunks):
            if c.get("chunk_id") == cid:
                self.chunks[i] = chunk
                return
        self.chunks.append(chunk)

    def delete_by_source(self, source_id):
        if not self.available:
            return False
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        # 兼容两种写入路径：正规路径 chunk 带 source_id；
        # 历史/兜底路径只带 source 字段，OR 匹配避免删除漏数据
        self._client.delete(
            collection_name=self.collection_name,
            points_selector=Filter(
                should=[
                    FieldCondition(key="source_id", match=MatchValue(value=source_id)),
                    FieldCondition(key="source", match=MatchValue(value=source_id)),
                ]
            ),
        )
        self.chunks = [c for c in self.chunks
                       if c.get("source_id", c.get("source", "")) != source_id]
        return True

    def search(self, query_embedding=None, tokenized_query=None, top_k=5, threshold=0.0):
        if not self.available or query_embedding is None:
            return []
        results = self._client.query_points(
            collection_name=self.collection_name,
            query=query_embedding.flatten().tolist(),
            limit=top_k,
            score_threshold=threshold,
        )
        return [{
            "text": r.payload.get("text", ""),
            "source": r.payload.get("source", ""),
            "source_id": r.payload.get("source_id", ""),
            "score": round(r.score, 4),
            "meta": r.payload.get("meta", {}),
            "chunk_id": r.payload.get("chunk_id", ""),
            "index": i,
        } for i, r in enumerate(results.points)]

    def persist(self):
        pass

    def load(self):
        return self.available

    def count(self):
        if not self.available:
            return 0
        return self._client.count(collection_name=self.collection_name).count
