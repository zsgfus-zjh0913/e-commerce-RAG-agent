"""
独立 RAG 检索服务 — 与主应用进程解耦。

职责：
  - 模型（embedding / reranker）只在此进程加载一次
  - 承担全部 CPU 密集工作：查询改写后编码、TF-IDF 混合检索、重排序
  - 主应用通过 HTTP 调用，Web worker 不再被检索阻塞

启动（本地）：
  python rag_service.py
启动（生产，多 worker 共享模型）：
  gunicorn -w 4 --threads 8 --preload -b 127.0.0.1:5001 rag_service:app
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from flask import Flask, request, jsonify

from rag_engine import RAGEngine, SUPPORTED_EXTENSIONS

DATA_DIR = os.environ.get("RAG_DATA_DIR", str(BASE_DIR / "data"))
INDEX_DIR = os.environ.get("RAG_INDEX_DIR", str(BASE_DIR / "index"))
SERVICE_HOST = os.environ.get("RAG_SERVICE_HOST", "127.0.0.1")
SERVICE_PORT = int(os.environ.get("RAG_SERVICE_PORT", "5001"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

print(f"[RAG-SERVICE] 初始化 RAG 引擎 ...")
rag = RAGEngine(DATA_DIR, INDEX_DIR)
_ = rag.encoder
_ = rag.reranker
print(f"[RAG-SERVICE] 就绪: {rag.store.count()} chunks, mode={rag._mode}")


def _safe_filename(filename):
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or ".." in name:
        return None
    return name


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "chunks": rag.store.count(),
        "mode": rag._mode if rag._mode != "unknown" else "tfidf",
    })


@app.post("/query")
def query():
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question 不能为空"}), 400
    top_k = int(data.get("top_k", 5))
    threshold = float(data.get("threshold", 0.0))
    use_cache = bool(data.get("use_cache", True))
    results = rag.query(question, top_k=top_k, threshold=threshold, use_cache=use_cache)
    return jsonify({"results": results})


@app.get("/stats")
def stats():
    return jsonify(rag.get_stats())


@app.get("/documents")
def documents():
    return jsonify({"documents": rag.list_documents()})


@app.post("/upload")
def upload():
    file = request.files.get("file")
    if file is None:
        return jsonify({"error": "缺少文件"}), 400
    filename = _safe_filename(file.filename)
    if filename is None:
        return jsonify({"error": "非法文件名"}), 400
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": f"不支持的格式: {ext}"}), 400
    rag.add_document(filename, file.read())
    return jsonify({"ok": True, "filename": filename})


@app.post("/delete")
def delete():
    data = request.get_json(force=True, silent=True) or {}
    filename = _safe_filename(data.get("filename", ""))
    if filename is None:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    ok = rag.delete_document(filename)
    return jsonify({"ok": ok})


@app.post("/invalidate")
def invalidate():
    rag.invalidate_query_cache()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"[RAG-SERVICE] listening on {SERVICE_HOST}:{SERVICE_PORT}")
    from werkzeug.serving import run_simple
    run_simple(SERVICE_HOST, SERVICE_PORT, app, threaded=True, processes=1)
