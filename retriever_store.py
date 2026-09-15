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

        for chunk in chunks:
            cid = chunk.get("chunk_id", "")
            if cid and cid in self._chunk_index:
                for idx in self._chunk_index[cid]:
                    self.chunks[idx] = chunk
            else:
                self.chunks.append(chunk)
                if cid:
                    if cid not in self._chunk_index:
                        self._chunk_index[cid] = []
                    self._chunk_index[cid].append(len(self.chunks) - 1)

        if embeddings is not None:
            if self.embeddings is not None and len(self.embeddings) > 0:
                self.embeddings = np.vstack([self.embeddings, embeddings])
            else:
                self.embeddings = embeddings

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
    当前为占位骨架，配置 QDRANT_URL 后自动激活。
    """

    def __init__(self, index_dir="index", collection_name="ecommerce_rag"):
        self.index_dir = Path(index_dir)
        self.collection_name = collection_name
        self._client = None
        self._encoder = None
        self._init_client()

    def _init_client(self):
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
            url = os.environ.get("QDRANT_URL", "")
            if not url:
                return
            self._client = QdrantClient(url=url)
            if not self._client.collection_exists(self.collection_name):
                self._client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=512, distance=Distance.COSINE),
                )
            print(f"[QdrantStore] 连接成功: {url}")
        except ImportError:
            print("[QdrantStore] qdrant-client 未安装，回退到 NumpyTfidfStore")
        except Exception as e:
            print(f"[QdrantStore] 连接失败: {e}")

    @property
    def available(self):
        return self._client is not None

    def add_chunks(self, chunks, embeddings=None):
        if not self.available or not chunks or embeddings is None:
            return
        from qdrant_client.models import PointStruct
        points = []
        for i, chunk in enumerate(chunks):
            points.append(PointStruct(
                id=hash(chunk.get("chunk_id", str(i))),
                vector=embeddings[i].tolist(),
                payload={
                    "text": chunk["text"],
                    "source": chunk.get("source", ""),
                    "source_id": chunk.get("source_id", ""),
                    "chunk_id": chunk.get("chunk_id", ""),
                    "meta": chunk.get("meta", {}),
                },
            ))
        self._client.upsert(collection_name=self.collection_name, points=points)

    def delete_by_source(self, source_id):
        if not self.available:
            return False
        self._client.delete(
            collection_name=self.collection_name,
            selector={"must": [{"key": "source_id", "match": {"value": source_id}}]},
        )
        return True

    def search(self, query_embedding=None, tokenized_query=None, top_k=5, threshold=0.0):
        if not self.available or query_embedding is None:
            return []
        results = self._client.search(
            collection_name=self.collection_name,
            query_vector=query_embedding.flatten().tolist(),
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
            "index": 0,
        } for r in results]

    def persist(self):
        pass

    def load(self):
        return self.available

    def count(self):
        if not self.available:
            return 0
        return self._client.count(collection_name=self.collection_name).count
