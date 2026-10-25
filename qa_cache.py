"""
自学习问答缓存：把「问题 → 答案」沉淀下来，下次命中直接复用。
- 精确匹配：规范化（去空格/标点/大小写）后逐条比对
- 语义兜底：用现有嵌入模型（rag.encoder）计算余弦相似度
"""

import re
import time
import threading

import numpy as np

from database import get_db, close_db, LearnedQA


def _normalize(text):
    """规范化问题文本，用于精确匹配。仅保留中英文与数字。"""
    if not text:
        return ""
    return re.sub(r"[\s\W_]+", "", text.lower().strip())


def _is_negative_answer(answer):
    """判断回答是否属于“未检索到/无此商品”类的兜底或否定型回答。

    这类回答时效性强（知识库/库存/活动随时可能变化），一旦固化会造成
    「明明有货却答没有」的缓存污染，因此命中否定模式时既不缓存、也不复用。
    """
    if not answer:
        return False
    return bool(re.search(
        r"没有查到|未查到|未检索到|无法为您准确解答|暂不可用|"
        r"没有[^。！？]{0,12}(商品|咖啡|产品|货|售卖|在售|销售|出售|收录|库存)|"
        r"暂无[^。！？]{0,12}(商品|货|售卖|在售|优惠|活动|数据|收录|库存)|"
        r"不售卖|不销售|未在售|不在售|缺货|无货|不存在|未收录|"
        r"没有收录|没有相关|没有该|没有提到|未提到|没有记录",
        answer,
    ))


class QACache:
    """内存索引 + 数据库持久化的问答缓存。"""

    SEMANTIC_THRESHOLD = 0.9

    def __init__(self, encoder_provider):
        self._encoder_provider = encoder_provider
        self._items = []  # 每条含 id/question/answer/source/hit_count/normalized/embedding
        self._lock = threading.Lock()
        self._load()

    @property
    def encoder(self):
        try:
            return self._encoder_provider()
        except Exception:
            return None

    def _to_item(self, row):
        return {
            "id": row.id,
            "question": row.question,
            "answer": row.answer,
            "source": row.source,
            "hit_count": row.hit_count or 0,
            "last_hit_at": row.last_hit_at,
            "normalized": _normalize(row.question),
            "embedding": None,
        }

    def _load(self):
        db = get_db()
        try:
            rows = db.query(LearnedQA).all()
            self._items = [self._to_item(r) for r in rows]
        except Exception as e:
            print(f"[QACache] 加载失败: {e}")
            self._items = []
        finally:
            close_db(db)
        self._build_embeddings()

    def _build_embeddings(self):
        enc = self.encoder
        if enc is None or not self._items:
            return
        try:
            embs = enc.encode(
                [it["question"] for it in self._items],
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            for it, emb in zip(self._items, embs):
                it["embedding"] = emb
        except Exception as e:
            print(f"[QACache] 语义索引构建失败（仅保留精确匹配）: {e}")

    def _encode(self, text):
        enc = self.encoder
        if enc is None:
            return None
        try:
            return enc.encode(
                [text], normalize_embeddings=True, show_progress_bar=False
            )[0]
        except Exception:
            return None

    def find_match(self, question):
        norm = _normalize(question)
        if not norm:
            return None

        with self._lock:
            # 1) 精确匹配（跳过工具类缓存，避免跨用户数据泄漏）
            for it in self._items:
                if it.get("source") == "tool":
                    continue
                if it["normalized"] == norm:
                    if _is_negative_answer(it["answer"]):
                        continue
                    return it

            # 2) 语义兜底
            q_emb = self._encode(question)
            if q_emb is None:
                return None
            best, best_score = None, -1.0
            for it in self._items:
                if it.get("source") == "tool":
                    continue
                if it["embedding"] is None:
                    continue
                score = float(np.dot(q_emb, it["embedding"]))
                if score > best_score:
                    best, best_score = it, score
            if (
                best
                and best_score >= self.SEMANTIC_THRESHOLD
                and not _is_negative_answer(best["answer"])
            ):
                return best
            return None

    def store(self, question, answer, source=""):
        norm = _normalize(question)
        # 工具类回答（订单/物流/售后）依赖实时数据且含用户特定信息，
        # 不可跨用户缓存，直接跳过。
        if source == "tool":
            return None
        if not norm or not answer or _is_negative_answer(answer):
            return None

        with self._lock:
            for it in self._items:
                if it["normalized"] == norm:
                    if _is_negative_answer(it["answer"]):
                        now = int(time.time())
                        db = get_db()
                        try:
                            row = db.query(LearnedQA).filter(
                                LearnedQA.id == it["id"]
                            ).first()
                            if row:
                                row.answer = answer
                                row.source = source
                                row.hit_count = 0
                                row.last_hit_at = None
                                db.commit()
                            it["answer"] = answer
                            it["source"] = source
                            it["hit_count"] = 0
                            it["last_hit_at"] = None
                            it["embedding"] = self._encode(question)
                            return it
                        except Exception as e:
                            db.rollback()
                            print(f"[QACache] 更新失败: {e}")
                            return None
                        finally:
                            close_db(db)
                    return it  # 已存在同问题，跳过

            now = int(time.time())
            db = get_db()
            try:
                row = LearnedQA(
                    question=question,
                    answer=answer,
                    source=source,
                    hit_count=0,
                    created_at=now,
                    last_hit_at=None,
                )
                db.add(row)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[QACache] 存储失败: {e}")
                return None
            finally:
                close_db(db)

            item = self._to_item(row)
            emb = self._encode(question)
            if emb is not None:
                item["embedding"] = emb
            self._items.append(item)
            return item

    def record_hit(self, qa_id):
        now = int(time.time())
        with self._lock:
            for it in self._items:
                if it["id"] == qa_id:
                    it["hit_count"] += 1
                    it["last_hit_at"] = now

        db = get_db()
        try:
            row = db.query(LearnedQA).filter(LearnedQA.id == qa_id).first()
            if row:
                row.hit_count = (row.hit_count or 0) + 1
                row.last_hit_at = now
                db.commit()
        except Exception as e:
            db.rollback()
            print(f"[QACache] 命中计数失败: {e}")
        finally:
            close_db(db)

    def remove(self, qa_id):
        with self._lock:
            self._items = [it for it in self._items if it["id"] != qa_id]

        db = get_db()
        try:
            row = db.query(LearnedQA).filter(LearnedQA.id == qa_id).first()
            if row:
                db.delete(row)
                db.commit()
                return True
            return False
        except Exception as e:
            db.rollback()
            print(f"[QACache] 删除失败: {e}")
            return False
        finally:
            close_db(db)


# ── 模块级单例 ──────────────────────────────────────────

_cache = None
_cache_lock = threading.Lock()


def init_cache(encoder_provider):
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = QACache(encoder_provider)
    return _cache


def get_cache():
    return _cache
