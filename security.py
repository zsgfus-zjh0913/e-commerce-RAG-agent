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
