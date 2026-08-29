import os
import time
import uuid
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone

from database import init_db, get_db, close_db, User, Session

init_db()

JWT_SECRET = os.environ.get("JWT_SECRET", "ecommerce-cs-jwt-secret-2026")
JWT_ALGORITHM = "HS256"
JWT_TTL_HOURS = 24
BLACKLIST_CLEANUP_THRESHOLD = 86400


class AuthManager:

    def __init__(self):
        self._cleanup_blacklist()

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = bcrypt.gensalt(rounds=12)
        hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
        return hashed.decode("utf-8")

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                password_hash.encode("utf-8"),
            )
        except Exception:
            return False

    @staticmethod
    def _create_jwt(username, gender):
        now = datetime.now(timezone.utc)
        payload = {
            "sub": username,
            "gender": gender,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + timedelta(hours=JWT_TTL_HOURS),
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    @staticmethod
    def _decode_jwt(token):
        try:
            return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

    def _cleanup_blacklist(self):
        db = get_db()
        try:
            cutoff = int(time.time()) - BLACKLIST_CLEANUP_THRESHOLD
            db.query(Session).filter(Session.login_at < cutoff).delete()
            db.commit()
        except Exception:
            db.rollback()
        finally:
            close_db(db)

    def _is_blacklisted(self, jti):
        db = get_db()
        try:
            return db.query(Session).filter(Session.token == jti).first() is not None
        except Exception:
            return False
        finally:
            close_db(db)

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

        db = get_db()
        try:
            existing = db.query(User).filter(User.username == username).first()
            if existing:
                return {"success": False, "message": "用户名已存在"}

            user = User(
                username=username,
                password_hash=self._hash_password(password),
                gender=gender,
                created_at=int(time.time()),
            )
            db.add(user)
            db.commit()
            return {"success": True, "message": "注册成功", "username": username}
        except Exception as e:
            db.rollback()
            return {"success": False, "message": f"注册失败: {e}"}
        finally:
            close_db(db)

    def login(self, username, password):
        username = username.strip()
        db = get_db()
        try:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                return {"success": False, "message": "用户名不存在"}

            if not self._verify_password(password, user.password_hash):
                return {"success": False, "message": "密码错误"}

            token = self._create_jwt(username, user.gender)
            return {
                "success": True,
                "message": "登录成功",
                "token": token,
                "username": username,
                "gender": user.gender,
            }
        except Exception as e:
            return {"success": False, "message": f"登录失败: {e}"}
        finally:
            close_db(db)

    def validate(self, token):
        if not token:
            return None

        payload = self._decode_jwt(token)
        if not payload:
            return None

        if self._is_blacklisted(payload.get("jti", "")):
            return None

        iat = payload.get("iat", time.time())
        if hasattr(iat, "timestamp"):
            iat = int(iat.timestamp())
        else:
            iat = int(iat)

        return {
            "username": payload.get("sub", ""),
            "gender": payload.get("gender", ""),
            "login_at": iat,
        }

    def logout(self, token):
        payload = self._decode_jwt(token)
        if not payload:
            return {"success": True, "message": "已退出登录"}

        jti = payload.get("jti", "")
        db = get_db()
        try:
            existing = db.query(Session).filter(Session.token == jti).first()
            if not existing:
                entry = Session(
                    token=jti,
                    username=payload.get("sub", ""),
                    gender=payload.get("gender", ""),
                    login_at=int(time.time()),
                )
                db.add(entry)
                db.commit()
            return {"success": True, "message": "已退出登录"}
        except Exception:
            db.rollback()
            return {"success": True, "message": "已退出登录"}
        finally:
            close_db(db)
