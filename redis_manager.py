"""
Redis 会话存储管理器
- 生产环境：Redis（支持多实例、持久化、过期自动清理）
- 开发环境：自动降级为内存字典（无需安装 Redis）
"""

import os
import json
import time
import threading

try:
    import redis as redis_lib
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

REDIS_URL = os.environ.get("REDIS_URL", "")

_redis_client = None
_redis_lock = threading.Lock()
_memory_store = {}
_memory_lock = threading.Lock()


def _get_redis():
    global _redis_client
    if not REDIS_AVAILABLE or not REDIS_URL:
        return None
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                try:
                    _redis_client = redis_lib.from_url(
                        REDIS_URL,
                        decode_responses=True,
                        socket_timeout=3,
                        socket_connect_timeout=3,
                    )
                    _redis_client.ping()
                except Exception:
                    _redis_client = None
    return _redis_client


def _is_redis_mode():
    return _get_redis() is not None


# ── Session 操作 ──

def set_session(session_id, data, ttl=86400):
    """存储用户会话，TTL 默认 24 小时"""
    if _is_redis_mode():
        r = _get_redis()
        r.setex(f"session:{session_id}", ttl, json.dumps(data, ensure_ascii=False))
    else:
        with _memory_lock:
            data["_expiry"] = time.time() + ttl
            _memory_store[f"session:{session_id}"] = data


def get_session(session_id):
    """获取用户会话"""
    if _is_redis_mode():
        r = _get_redis()
        val = r.get(f"session:{session_id}")
        if val:
            return json.loads(val)
        return None
    else:
        with _memory_lock:
            data = _memory_store.get(f"session:{session_id}")
            if data and data.get("_expiry", 0) > time.time():
                data.pop("_expiry", None)
                return data
            if data:
                _memory_store.pop(f"session:{session_id}", None)
            return None


def delete_session(session_id):
    """删除用户会话"""
    if _is_redis_mode():
        r = _get_redis()
        r.delete(f"session:{session_id}")
    else:
        with _memory_lock:
            _memory_store.pop(f"session:{session_id}", None)


# ── Chat Session 操作（对话上下文） ──

def set_chat_session(session_id, messages, ttl=7200):
    """存储对话上下文，TTL 默认 2 小时"""
    if _is_redis_mode():
        r = _get_redis()
        r.setex(f"chat:{session_id}", ttl, json.dumps(messages, ensure_ascii=False))
    else:
        with _memory_lock:
            _memory_store[f"chat:{session_id}"] = {
                "messages": messages,
                "_expiry": time.time() + ttl,
            }


def get_chat_session(session_id):
    """获取对话上下文"""
    if _is_redis_mode():
        r = _get_redis()
        val = r.get(f"chat:{session_id}")
        if val:
            return json.loads(val)
        return None
    else:
        with _memory_lock:
            data = _memory_store.get(f"chat:{session_id}")
            if data and data.get("_expiry", 0) > time.time():
                return data.get("messages", [])
            if data:
                _memory_store.pop(f"chat:{session_id}", None)
            return None


def delete_chat_session(session_id):
    """删除对话上下文"""
    if _is_redis_mode():
        r = _get_redis()
        r.delete(f"chat:{session_id}")
    else:
        with _memory_lock:
            _memory_store.pop(f"chat:{session_id}", None)


# ── 缓存操作 ──

def set_cache(key, value, ttl=300):
    if _is_redis_mode():
        r = _get_redis()
        r.setex(f"cache:{key}", ttl, json.dumps(value, ensure_ascii=False))
    else:
        with _memory_lock:
            _memory_store[f"cache:{key}"] = {
                "value": value,
                "_expiry": time.time() + ttl,
            }


def get_cache(key):
    if _is_redis_mode():
        r = _get_redis()
        val = r.get(f"cache:{key}")
        if val:
            return json.loads(val)
        return None
    else:
        with _memory_lock:
            data = _memory_store.get(f"cache:{key}")
            if data and data.get("_expiry", 0) > time.time():
                return data.get("value")
            if data:
                _memory_store.pop(f"cache:{key}", None)
            return None


def delete_cache(key):
    if _is_redis_mode():
        r = _get_redis()
        r.delete(f"cache:{key}")
    else:
        with _memory_lock:
            _memory_store.pop(f"cache:{key}", None)


# ── 健康检查 ──

def health_check():
    """返回存储状态"""
    return {
        "redis_enabled": _is_redis_mode(),
        "storage_mode": "redis" if _is_redis_mode() else "memory",
        "redis_url": REDIS_URL.split("@")[-1] if REDIS_URL and "@" in REDIS_URL else "",
    }


# ── 清理过期内存数据（开发模式） ──

def cleanup_memory():
    """清理过期的内存数据"""
    if _is_redis_mode():
        return
    with _memory_lock:
        now = time.time()
        expired = [k for k, v in _memory_store.items() if isinstance(v, dict) and v.get("_expiry", 0) < now]
        for k in expired:
            _memory_store.pop(k, None)
