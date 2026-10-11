"""
RAG 检索服务 HTTP 客户端 — 主应用侧代理。

接口与 RAGEngine 对齐（query / add_document / delete_document /
list_documents / get_stats / invalidate_query_cache），保证
app.py 与 agent_routes.py 的调用点零改动。

服务不可用时：
  - query 降级返回 []（聊天链路自带兜底）
  - 文档管理接口抛出异常（坐席端可见明确错误）
"""

import os
import time

import requests


class RAGServiceClient:
    """指向 rag_service.py 的远程代理。"""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10, pool_maxsize=20, max_retries=0
        )
        self._session.mount("http://", adapter)
        self._connect_timeout = 2.0
        self._read_timeout = 20.0
        # qa_cache 需要 encoder 属性；远程模式下语义兜底不可用，置 None
        self.encoder = None

    def _post(self, path, json=None, files=None, expect_status=200):
        try:
            resp = self._session.post(
                f"{self.base_url}{path}",
                json=json,
                files=files,
                timeout=(self._connect_timeout, self._read_timeout),
            )
        except requests.exceptions.Timeout:
            raise RuntimeError("RAG 服务响应超时")
        except requests.exceptions.ConnectionError:
            raise RuntimeError("RAG 服务不可用")
        if resp.status_code != expect_status:
            try:
                err = resp.json().get("error", resp.text[:200])
            except Exception:
                err = resp.text[:200]
            raise RuntimeError(f"RAG 服务返回 {resp.status_code}: {err}")
        return resp.json()

    def _get(self, path):
        try:
            resp = self._session.get(
                f"{self.base_url}{path}",
                timeout=(self._connect_timeout, self._read_timeout),
            )
        except requests.exceptions.Timeout:
            raise RuntimeError("RAG 服务响应超时")
        except requests.exceptions.ConnectionError:
            raise RuntimeError("RAG 服务不可用")
        if resp.status_code != 200:
            raise RuntimeError(f"RAG 服务返回 {resp.status_code}")
        return resp.json()

    def query(self, question, top_k=5, threshold=0.0, use_cache=True):
        try:
            data = self._post(
                "/query",
                json={
                    "question": question,
                    "top_k": top_k,
                    "threshold": threshold,
                    "use_cache": use_cache,
                },
            )
        except Exception as e:
            print(f"[RAGClient] query 失败: {e}")
            return []
        return data.get("results", [])

    def add_document(self, filename, content_bytes):
        try:
            data = self._post(
                "/upload",
                files={"file": (filename, content_bytes)},
            )
        except Exception as e:
            print(f"[RAGClient] add_document 失败: {e}")
            raise
        return data.get("filename", filename)

    def delete_document(self, filename):
        try:
            data = self._post("/delete", json={"filename": filename})
            return bool(data.get("ok"))
        except Exception as e:
            print(f"[RAGClient] delete_document 失败: {e}")
            raise

    def list_documents(self):
        try:
            data = self._get("/documents")
            return data.get("documents", [])
        except Exception as e:
            print(f"[RAGClient] list_documents 失败: {e}")
            return []

    def get_stats(self):
        try:
            return self._get("/stats")
        except Exception as e:
            print(f"[RAGClient] get_stats 失败: {e}")
            return {}

    def invalidate_query_cache(self):
        try:
            self._post("/invalidate", json={})
        except Exception as e:
            print(f"[RAGClient] invalidate 失败: {e}")
