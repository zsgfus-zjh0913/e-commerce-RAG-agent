"""
Redis 会话存储管理器
- 生产环境：Redis（支持多实例、持久化、过期自动清理）
- 开发环境：自动降级为内存字典（无需安装 Redis）
"""

import os
import json
import time
import uuid
import hashlib
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

_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest[2] then
        retry_after = math.max(1, math.ceil(window - (now - tonumber(oldest[2]))))
    end
    return {0, retry_after, count}
end

redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, math.ceil(window * 1000))
return {1, 0, count + 1}
"""


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
        key = f"chat:{session_id}"
        pipe = r.pipeline()
        pipe.delete(key)
        if messages:
            pipe.rpush(
                key,
                *[json.dumps(item, ensure_ascii=False) for item in messages],
            )
            pipe.expire(key, ttl)
        pipe.execute()
    else:
        with _memory_lock:
            _memory_store[f"chat:{session_id}"] = {
                "messages": list(messages),
                "_expiry": time.time() + ttl,
            }


def get_chat_session(session_id):
    """获取对话上下文"""
    if _is_redis_mode():
        r = _get_redis()
        key = f"chat:{session_id}"
        try:
            values = r.lrange(key, 0, -1)
        except Exception:
            r.delete(key)
            return None
        return [json.loads(value) for value in values]
    else:
        with _memory_lock:
            data = _memory_store.get(f"chat:{session_id}")
            if data and data.get("_expiry", 0) > time.time():
                return list(data.get("messages", []))
            if data:
                _memory_store.pop(f"chat:{session_id}", None)
            return None


def append_chat_message(session_id, message, max_items=12, ttl=7200):
    """原子追加一条会话消息，并裁剪历史长度。"""
    if _is_redis_mode():
        r = _get_redis()
        key = f"chat:{session_id}"
        pipe = r.pipeline()
        pipe.rpush(key, json.dumps(message, ensure_ascii=False))
        pipe.ltrim(key, -max_items, -1)
        pipe.expire(key, ttl)
        pipe.execute()
        return

    with _memory_lock:
        key = f"chat:{session_id}"
        data = _memory_store.get(key)
        if not data or data.get("_expiry", 0) <= time.time():
            data = {"messages": [], "_expiry": time.time() + ttl}
        messages = list(data.get("messages", []))
        messages.append(message)
        data["messages"] = messages[-max_items:]
        data["_expiry"] = time.time() + ttl
        _memory_store[key] = data


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


def delete_cache_prefix(prefix):
    """按 key 前缀批量删除缓存（用于知识库变更后失效检索缓存）。"""
    full_prefix = f"cache:{prefix}"
    if _is_redis_mode():
        r = _get_redis()
        keys = list(r.scan_iter(match=full_prefix + "*", count=200))
        if keys:
            r.delete(*keys)
    else:
        with _memory_lock:
            expired = [
                key for key in _memory_store
                if key.startswith(full_prefix)
            ]
            for key in expired:
                _memory_store.pop(key, None)


# ── 限流操作 ──

def check_rate_limit(key, limit, window):
    """滑动窗口限流，返回 (allowed, retry_after, current_count)。"""
    full_key = f"ratelimit:{key}"
    now = time.time()

    if _is_redis_mode():
        r = _get_redis()
        result = r.eval(
            _RATE_LIMIT_LUA,
            1,
            full_key,
            now,
            window,
            limit,
            f"{now}:{uuid.uuid4().hex}",
        )
        return bool(result[0]), int(result[1]), int(result[2])

    with _memory_lock:
        data = _memory_store.get(full_key, {})
        stored_entries = data.get("entries", []) if isinstance(data, dict) else []
        entries = [
            float(value)
            for value in stored_entries
            if now - float(value) < window
        ]
        if len(entries) >= limit:
            retry_after = max(1, int(window - (now - min(entries))))
            _memory_store[full_key] = {
                "entries": entries,
                "_expiry": now + window,
            }
            return False, retry_after, len(entries)

        entries.append(now)
        _memory_store[full_key] = {
            "entries": entries,
            "_expiry": now + window,
        }
        return True, 0, len(entries)


def reset_rate_limit(key):
    full_key = f"ratelimit:{key}"
    if _is_redis_mode():
        _get_redis().delete(full_key)
    else:
        with _memory_lock:
            _memory_store.pop(full_key, None)


# ── Agent Token 黑名单 ──

def _blacklist_key(token):
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"blacklist:agent:{digest}"


def blacklist_agent_token(token, ttl):
    if not token:
        return
    key = _blacklist_key(token)
    if _is_redis_mode():
        _get_redis().setex(key, max(1, int(ttl)), "1")
    else:
        with _memory_lock:
            _memory_store[key] = {
                "value": True,
                "_expiry": time.time() + max(1, ttl),
            }


def is_agent_token_blacklisted(token):
    if not token:
        return False
    key = _blacklist_key(token)
    if _is_redis_mode():
        return bool(_get_redis().exists(key))

    with _memory_lock:
        data = _memory_store.get(key)
        if not data:
            return False
        if data.get("_expiry", 0) <= time.time():
            _memory_store.pop(key, None)
            return False
        return bool(data.get("value"))


# ── 会话级互斥锁（防抢单/双写） ──

def acquire_lock(key, owner, ttl=30):
    """原子获取互斥锁，返回是否成功。Redis 用 SET NX EX，内存模式用字典模拟。"""
    if not key:
        return False
    full_key = f"lock:{key}"
    if _is_redis_mode():
        r = _get_redis()
        return bool(r.set(full_key, str(owner), nx=True, ex=max(1, int(ttl))))
    with _memory_lock:
        data = _memory_store.get(full_key)
        now = time.time()
        if isinstance(data, dict) and data.get("_expiry", 0) > now:
            return False
        _memory_store[full_key] = {
            "value": str(owner),
            "_expiry": now + max(1, ttl),
        }
        return True


def release_lock(key, owner=None):
    """释放互斥锁；owner 非空时仅释放属于自己的锁，避免误删他人锁。"""
    if not key:
        return
    full_key = f"lock:{key}"
    if _is_redis_mode():
        r = _get_redis()
        if owner is None:
            r.delete(full_key)
            return
        _RELEASE_LUA = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            r.eval(_RELEASE_LUA, 1, full_key, str(owner))
        except Exception:
            pass
        return
    with _memory_lock:
        data = _memory_store.get(full_key)
        if isinstance(data, dict) and (
            owner is None or data.get("value") == str(owner)
        ):
            _memory_store.pop(full_key, None)


# ── 人工会话接管状态（AI/人工 控制权） ──

def set_human_state(username, state, ttl=86400):
    """记录某用户当前的人工接管状态。state: {"ticket_id","session_id","mode","agent","taken_at"}"""
    if not username:
        return
    set_cache(f"human:{username}", state, ttl=ttl)


def get_human_state(username):
    if not username:
        return None
    return get_cache(f"human:{username}")


def clear_human_state(username):
    if not username:
        return
    delete_cache(f"human:{username}")


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
        expired = [
            key for key, value in _memory_store.items()
            if isinstance(value, dict)
            and value.get("_expiry", 0) < now
        ]
        for k in expired:
            _memory_store.pop(k, None)
