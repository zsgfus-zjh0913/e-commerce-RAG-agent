import re
import html
import time
from functools import wraps
from collections import defaultdict

from flask import request, jsonify, g


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
        "img-src 'self' data:; "
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
        self._requests = defaultdict(list)
        self._auth_attempts = defaultdict(list)

    def check(self, key=None, is_auth=False):
        ip = request.remote_addr or "unknown"
        check_key = key or ip
        now = time.time()
        limit = self.auth_max if is_auth else self.max_requests

        store = self._auth_attempts if is_auth else self._requests
        store[check_key] = [t for t in store[check_key] if now - t < self.window]

        if len(store[check_key]) >= limit:
            return False

        store[check_key].append(now)
        return True

    def reset(self, key=None):
        ip = request.remote_addr or "unknown"
        check_key = key or ip
        if check_key in self._requests:
            del self._requests[check_key]
        if check_key in self._auth_attempts:
            del self._auth_attempts[check_key]


rate_limiter = RateLimiter()


def rate_limit(max_requests=None, window=60, is_auth=False):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            limit = max_requests or (10 if is_auth else 60)

            now = time.time()
            key = f"{ip}:{f.__name__}"
            store = rate_limiter._auth_attempts if is_auth else rate_limiter._requests
            store[key] = [t for t in store[key] if now - t < window]

            if len(store[key]) >= limit:
                return jsonify({
                    "error": "请求过于频繁，请稍后再试"
                }), 429

            store[key].append(now)
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
