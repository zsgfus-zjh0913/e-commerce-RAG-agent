import json
import hashlib
import secrets
import time
from pathlib import Path

USERS_FILE = Path(__file__).parent / "users.json"
SESSIONS_FILE = Path(__file__).parent / "sessions.json"


def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt, hashed


def _load_users():
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_users(users):
    try:
        USERS_FILE.write_text(
            json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[Auth] 保存用户失败: {e}")


def _load_sessions():
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_sessions(sessions):
    try:
        SESSIONS_FILE.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[Auth] 保存会话失败: {e}")


class AuthManager:

    def __init__(self):
        self.users = _load_users()
        self.sessions = _load_sessions()

    def register(self, username, password, gender):
        username = username.strip()
        if not username or not password:
            return {"success": False, "message": "用户名和密码不能为空"}
        if len(username) < 2:
            return {"success": False, "message": "用户名至少2个字符"}
        if len(password) < 4:
            return {"success": False, "message": "密码至少4个字符"}
        if gender not in ("male", "female"):
            return {"success": False, "message": "请选择性别"}
        if username in self.users:
            return {"success": False, "message": "用户名已存在"}

        salt, hashed = _hash_password(password)
        self.users[username] = {
            "username": username,
            "salt": salt,
            "password": hashed,
            "gender": gender,
            "created_at": time.time(),
        }
        _save_users(self.users)
        return {"success": True, "message": "注册成功", "username": username}

    def login(self, username, password):
        username = username.strip()
        if username not in self.users:
            return {"success": False, "message": "用户名不存在"}

        user = self.users[username]
        salt = user["salt"]
        _, hashed = _hash_password(password, salt)
        if hashed != user["password"]:
            return {"success": False, "message": "密码错误"}

        token = secrets.token_hex(32)
        self.sessions[token] = {
            "username": username,
            "gender": user["gender"],
            "login_at": time.time(),
        }
        _save_sessions(self.sessions)
        return {
            "success": True,
            "message": "登录成功",
            "token": token,
            "username": username,
            "gender": user["gender"],
        }

    def validate(self, token):
        if not token:
            return None
        self.sessions = _load_sessions()
        if token not in self.sessions:
            return None
        session = self.sessions[token]
        if time.time() - session["login_at"] > 86400:
            del self.sessions[token]
            _save_sessions(self.sessions)
            return None
        return session

    def logout(self, token):
        self.sessions = _load_sessions()
        if token in self.sessions:
            del self.sessions[token]
            _save_sessions(self.sessions)
        return {"success": True, "message": "已退出登录"}
