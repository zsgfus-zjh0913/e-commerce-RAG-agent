"""
结构化日志 + 审计追踪
"""

import json
import time
from datetime import datetime
from pathlib import Path

from database import get_db, close_db, AuditLog

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"app_{datetime.now().strftime('%Y%m%d')}.log"


def _write_log(level, module, message, extra=None):
    entry = {
        "ts": datetime.now().isoformat(),
        "level": level,
        "module": module,
        "message": message,
    }
    if extra:
        entry["extra"] = extra

    line = json.dumps(entry, ensure_ascii=False)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    return entry


def info(module, message, extra=None):
    return _write_log("INFO", module, message, extra)


def warning(module, message, extra=None):
    return _write_log("WARN", module, message, extra)


def error(module, message, extra=None):
    return _write_log("ERROR", module, message, extra)


def audit(actor_type, actor_name, action, target_type="", target_id="", detail=""):
    """记录审计日志到数据库 + 文件"""
    now = int(time.time())
    db = get_db()
    try:
        log = AuditLog(
            actor_type=actor_type,
            actor_name=actor_name,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            created_at=now,
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        close_db(db)

    _write_log("AUDIT", "audit", f"{actor_type}:{actor_name} {action} {target_type}:{target_id}", {"detail": detail})


def get_recent_logs(limit=100):
    """读取最近的日志"""
    logs = []
    if not LOG_FILE.exists():
        return logs
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines[-limit:]:
        try:
            logs.append(json.loads(line.strip()))
        except Exception:
            pass
    return logs
