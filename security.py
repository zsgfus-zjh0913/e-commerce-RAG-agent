import re
import html
from functools import wraps

from flask import request, jsonify, g

import redis_manager


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    ),
}


def apply_security_headers(response):
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


def sanitize_input(text, max_length=5000):
    if not text:
        return ""
    text = str(text).strip()
    if len(text) > max_length:
        text = text[:max_length]
    text = html.escape(text)
    return text


DANGEROUS_PATTERNS = [
    re.compile(r"<script.*?>.*?</script>", re.IGNORECASE | re.DOTALL),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"on\w+\s*=", re.IGNORECASE),
    re.compile(r"<iframe.*?>.*?</iframe>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<object.*?>.*?</object>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<embed.*?>", re.IGNORECASE),
]

SQL_INJECTION_PATTERNS = [
    re.compile(r"'.*?(OR|UNION|SELECT|INSERT|DELETE|DROP|UPDATE).*'", re.IGNORECASE),
    re.compile(r";\s*(DROP|DELETE|UPDATE|INSERT)", re.IGNORECASE),
]


def is_malicious_input(text):
    if not text:
        return False
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(text):
            return True
    for pattern in SQL_INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


class RateLimiter:

    def __init__(self, max_requests=60, window_seconds=60, auth_max=10):
        self.max_requests = max_requests
        self.window = window_seconds
        self.auth_max = auth_max

    def check(self, key=None, is_auth=False):
        ip = request.remote_addr or "unknown"
        check_key = key or ip
        limit = self.auth_max if is_auth else self.max_requests
        allowed, _, _ = redis_manager.check_rate_limit(
            check_key, limit, self.window
        )
        return allowed

    def reset(self, key=None):
        ip = request.remote_addr or "unknown"
        check_key = key or ip
        redis_manager.reset_rate_limit(check_key)


rate_limiter = RateLimiter()


def rate_limit(max_requests=None, window=60, is_auth=False):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            limit = max_requests or (10 if is_auth else 60)
            key = f"{ip}:{f.__name__}"
            allowed, retry_after, _ = redis_manager.check_rate_limit(
                f"{'auth' if is_auth else 'api'}:{key}",
                limit,
                window,
            )
            if not allowed:
                response = jsonify({
                    "error": "请求过于频繁，请稍后再试"
                })
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response
            return f(*args, **kwargs)
        return decorated
    return decorator


def validate_file_upload(filename, file_size, allowed_extensions):
    if not filename:
        return False, "文件名为空"
    if "/" in filename or "\\" in filename or ".." in filename:
        return False, "非法文件名"
    import os
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_extensions:
        return False, f"不支持的文件类型 {ext}"
    if file_size > 50 * 1024 * 1024:
        return False, "文件超过 50MB 限制"
    return True, ""


# ── 内容安全 / 情绪风险分级检测 ─────────────────────────
# 两层：内容安全（辱骂/违规/广告引流，硬拦截）+ 情绪风险（愤怒/绝望/威胁/自残，软触发）
# 三级响应：soft=安抚+继续；medium=预警+主动转人工；hard=立即转人工+暂停 AI 输出

HARD_BLOCK_WORDS = [
    # 辱骂 / 人身攻击
    "傻逼", "傻b", "脑残", "去死", "滚蛋", "贱人", "白痴", "废物",
    "你妈的", "操你", "妈的", "草泥马", "他妈",
    # 广告引流 / 违规
    "代开发票", "刷单兼职", "兼职刷单", "加我微信", "加微信", "加qq",
    "私聊转账", "红包返利", "博彩平台", "赌博网址", "包过", "办假证",
]

SELF_HARM_WORDS = [
    "想死", "自杀", "不想活", "活着没意思", "轻生", "抑郁", "自残", "结束生命",
]

THREAT_WORDS = [
    "我要投诉", "投诉到", "举报你", "曝光你", "曝光你们", "315", "12315",
    "维权", "起诉", "报警", "记者", "媒体曝光", "我要举报", "不处理就",
]

ANGER_WORDS = [
    "气死", "气炸", "太气人", "火大", "愤怒", "气人", "太差", "垃圾",
    "差评", "客服态度差", "凭什么", "不讲理", "生气", "崩溃", "绝望", "受不了",
]

_LEVEL_RANK = {"ok": 0, "soft": 1, "medium": 2, "hard": 3}


def classify_content_safety(text, history=None, llm_assess=None):
    """对用户输入做内容安全与情绪风险分级。

    返回 dict:
      {"level": ok|soft|medium|hard, "blocked": bool, "categories": [...], "tags": [...]}
    其中 blocked=True 表示命中内容安全硬拦截（辱骂/违规/引流），应拒绝处理。
    """
    if not text:
        return {"level": "ok", "blocked": False, "categories": [], "tags": []}

    lowered = text.lower()
    categories = []
    tags = []

    # 1) 内容安全：硬拦截
    if any(w in lowered for w in HARD_BLOCK_WORDS):
        return {
            "level": "hard", "blocked": True,
            "categories": ["content_violation"], "tags": ["违规/辱骂言论"],
        }

    # 2) 情绪/风险：软触发，结合多轮上下文
    self_harm = [w for w in SELF_HARM_WORDS if w in lowered]
    threat = [w for w in THREAT_WORDS if w in lowered]
    anger = [w for w in ANGER_WORDS if w in lowered]

    if self_harm:
        level = "hard"
        tags.extend(self_harm)
        categories.append("self_harm")
    elif threat:
        level = "medium"
        tags.extend(threat)
        categories.append("complaint_threat")
    elif anger:
        level = "soft"
        tags.extend(anger)
        categories.append("anger")
    else:
        level = "ok"

    # 3) LLM 情绪判断：仅对轻度信号做升级修正（soft → medium/hard），
    #    避免每条消息都多一次模型调用；中/重度已由可靠词表决定。
    if llm_assess is not None and level == "soft":
        try:
            llm_result = llm_assess(text, history) or {}
            llm_level = llm_result.get("level", "ok")
            llm_tags = llm_result.get("tags") or []
            if _LEVEL_RANK.get(llm_level, 0) > _LEVEL_RANK.get(level, 0):
                level = llm_level
            for t in llm_tags:
                if t and t not in tags:
                    tags.append(t)
            if llm_level == "hard":
                categories.append("llm_high_risk")
        except Exception:
            pass

    return {
        "level": level, "blocked": False,
        "categories": categories, "tags": tags,
    }
