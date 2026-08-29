"""
人工客服坐席系统 API 路由
"""

import time
import uuid
import json
import bcrypt
import jwt as pyjwt
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, render_template, make_response, g
from database import get_db, close_db, Agent, Ticket, Order, AuditLog
import logger

agent_bp = Blueprint("agent", __name__)

JWT_SECRET = "ecommerce-agent-secret-2026"
JWT_ALGO = "HS256"
JWT_EXPIRY_HOURS = 12
_blacklist = set()


def create_agent_token(agent_id, username, name):
    payload = {
        "sub": username,
        "name": name,
        "aid": agent_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "type": "agent",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def agent_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            token = request.cookies.get("agent_token")
        if not token:
            return jsonify({"error": "未登录"}), 401
        try:
            if token in _blacklist:
                return jsonify({"error": "token已失效"}), 401
            payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
            if payload.get("type") != "agent":
                return jsonify({"error": "非坐席token"}), 403
            request.agent = {
                "id": payload.get("aid"),
                "username": payload.get("sub"),
                "name": payload.get("name"),
            }
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "登录已过期"}), 401
        except Exception:
            return jsonify({"error": "token无效"}), 401
        return f(*args, **kwargs)
    return decorated


# ── 坐席登录页面 ──
@agent_bp.route("/agent/login", methods=["GET"])
def agent_login_page():
    return render_template("agent_login.html")


@agent_bp.route("/agent/dashboard", methods=["GET"])
def agent_dashboard_page():
    token = request.cookies.get("agent_token")
    if not token:
        return render_template("agent_login.html")
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        if payload.get("type") != "agent":
            return render_template("agent_login.html")
    except Exception:
        return render_template("agent_login.html")
    return render_template("agent_dashboard.html")


# ── 坐席登录 API ──
@agent_bp.route("/api/agent/login", methods=["POST"])
def agent_login_api():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.username == username).first()
        if not agent or not bcrypt.checkpw(password.encode(), agent.password_hash.encode()):
            return jsonify({"error": "用户名或密码错误"}), 401

        agent.status = "online"
        db.commit()

        token = create_agent_token(agent.id, agent.username, agent.name)
        logger.audit("agent", agent.username, "login", "agent", str(agent.id), "坐席登录")

        resp = make_response(jsonify({
            "success": True,
            "agent": {
                "username": agent.username,
                "name": agent.name,
            }
        }))
        resp.set_cookie("agent_token", token, max_age=JWT_EXPIRY_HOURS * 3600,
                        httponly=True, samesite="Strict", path="/")
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


@agent_bp.route("/api/agent/logout", methods=["POST"])
@agent_required
def agent_logout():
    token = request.headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    if not token:
        token = request.cookies.get("agent_token")
    if token:
        _blacklist.add(token)

    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.username == request.agent["username"]).first()
        if agent:
            agent.status = "offline"
            db.commit()
    except Exception:
        pass
    finally:
        close_db(db)

    logger.audit("agent", request.agent["username"], "logout", "agent", "", "坐席登出")
    resp = make_response(jsonify({"success": True}))
    resp.delete_cookie("agent_token", path="/")
    return resp


# ── 坐席信息 ──
@agent_bp.route("/api/agent/me", methods=["GET"])
@agent_required
def agent_me():
    return jsonify({
        "username": request.agent["username"],
        "name": request.agent["name"],
    })


# ── 仪表盘数据 ──
@agent_bp.route("/api/agent/dashboard", methods=["GET"])
@agent_required
def agent_dashboard():
    db = get_db()
    try:
        pending_tickets = db.query(Ticket).filter(Ticket.status == "pending").order_by(Ticket.created_at.desc()).all()
        all_tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).limit(50).all()
        pending_count = db.query(Ticket).filter(Ticket.status == "pending").count()
        approved_count = db.query(Ticket).filter(Ticket.status == "approved").count()
        rejected_count = db.query(Ticket).filter(Ticket.status == "rejected").count()
        resolved_count = db.query(Ticket).filter(Ticket.status == "resolved").count()
        total_orders = db.query(Order).count()

        return jsonify({
            "pending_tickets": [t.to_dict() for t in pending_tickets],
            "recent_tickets": [t.to_dict() for t in all_tickets],
            "stats": {
                "pending": pending_count,
                "approved": approved_count,
                "rejected": rejected_count,
                "resolved": resolved_count,
                "total_orders": total_orders,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 工单处理 ──
@agent_bp.route("/api/agent/ticket/<ticket_id>/process", methods=["POST"])
@agent_required
def agent_process_ticket(ticket_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    reply = data.get("reply", "").strip()

    if action not in ("approve", "reject", "resolve"):
        return jsonify({"error": "操作类型无效"}), 400

    db = get_db()
    try:
        ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        if not ticket:
            return jsonify({"error": "工单不存在"}), 404

        status_map = {
            "approve": "approved",
            "reject": "rejected",
            "resolve": "resolved",
        }
        old_status = ticket.status
        ticket.status = status_map[action]
        ticket.agent_reply = reply
        ticket.handled_by = request.agent["username"]
        ticket.updated_at = int(time.time())
        db.commit()

        logger.audit(
            "agent", request.agent["username"],
            f"ticket_{action}", "ticket", ticket_id,
            f"工单 {ticket_id} 状态: {old_status} -> {status_map[action]}, 回复: {reply[:100]}"
        )

        return jsonify({"success": True, "ticket": ticket.to_dict()})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 工单列表 ──
@agent_bp.route("/api/agent/tickets", methods=["GET"])
@agent_required
def agent_ticket_list():
    status = request.args.get("status", "")
    db = get_db()
    try:
        q = db.query(Ticket)
        if status:
            q = q.filter(Ticket.status == status)
        tickets = q.order_by(Ticket.created_at.desc()).limit(100).all()
        return jsonify({"tickets": [t.to_dict() for t in tickets]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 订单查询 ──
@agent_bp.route("/api/agent/orders", methods=["GET"])
@agent_required
def agent_order_list():
    customer = request.args.get("customer", "").strip()
    status = request.args.get("status", "").strip()
    db = get_db()
    try:
        q = db.query(Order)
        if customer:
            q = q.filter(Order.customer_username == customer)
        if status:
            q = q.filter(Order.status == status)
        orders = q.order_by(Order.created_at.desc()).limit(100).all()
        return jsonify({"orders": [o.to_dict() for o in orders]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


@agent_bp.route("/api/agent/order/<order_id>", methods=["GET"])
@agent_required
def agent_order_detail(order_id):
    db = get_db()
    try:
        order = db.query(Order).filter(Order.order_id == order_id).first()
        if not order:
            return jsonify({"error": "订单不存在"}), 404
        return jsonify(order.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


@agent_bp.route("/api/agent/order/<order_id>/status", methods=["POST"])
@agent_required
def agent_update_order_status(order_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "").strip()

    valid_statuses = ["pending", "paid", "shipped", "delivered", "refunded", "cancelled"]
    if new_status not in valid_statuses:
        return jsonify({"error": f"状态无效，可选: {', '.join(valid_statuses)}"}), 400

    db = get_db()
    try:
        order = db.query(Order).filter(Order.order_id == order_id).first()
        if not order:
            return jsonify({"error": "订单不存在"}), 404

        old_status = order.status
        order.status = new_status
        order.updated_at = int(time.time())
        db.commit()

        logger.audit(
            "agent", request.agent["username"],
            "order_status_update", "order", order_id,
            f"订单 {order_id} 状态: {old_status} -> {new_status}"
        )

        return jsonify({"success": True, "order": order.to_dict()})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 审计日志 ──
@agent_bp.route("/api/agent/audit-logs", methods=["GET"])
@agent_required
def agent_audit_logs():
    db = get_db()
    try:
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(200).all()
        return jsonify({"logs": [l.to_dict() for l in logs]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 系统日志 ──
@agent_bp.route("/api/agent/system-logs", methods=["GET"])
@agent_required
def agent_system_logs():
    logs = logger.get_recent_logs(200)
    return jsonify({"logs": logs})
