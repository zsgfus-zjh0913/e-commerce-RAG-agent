import os
import json
import time
import uuid
import hashlib
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request, jsonify, send_from_directory,
    Response, stream_with_context, redirect, url_for, make_response,
)
from functools import wraps

from rag_engine import RAGEngine, SUPPORTED_EXTENSIONS
from llm_client import LLMClient
from auth_manager import AuthManager
from database import init_db, get_db, close_db, Ticket, Order, Product
from security import (
    apply_security_headers, sanitize_input, is_malicious_input,
    rate_limit, validate_file_upload,
)
from agent_routes import agent_bp
import logger

init_db()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", os.environ.get("JWT_SECRET", "ecommerce-cs-secret-2026"))
app.register_blueprint(agent_bp)


@app.after_request
def after_request(response):
    apply_security_headers(response)
    return response

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DIR = BASE_DIR / "index"
INDEX_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_DIR = BASE_DIR / "histories"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

rag = RAGEngine(str(DATA_DIR), str(INDEX_DIR))
_ = rag.encoder
_ = rag.reranker

auth = AuthManager()

# ── Session / History 管理 ──────────────────────────────

MAX_HISTORY_TURNS = 6
_sessions = {}
_sessions_lock = threading.Lock()


def _get_session(session_id):
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, s in _sessions.items()
                   if now - s["last_active"] > 1800]
        for sid in expired:
            del _sessions[sid]
        if session_id not in _sessions:
            _sessions[session_id] = {"messages": [], "last_active": now}
        else:
            _sessions[session_id]["last_active"] = now
        return _sessions[session_id]


def _add_to_history(session_id, role, content):
    session = _get_session(session_id)
    session["messages"].append({"role": role, "content": content})
    if len(session["messages"]) > MAX_HISTORY_TURNS:
        session["messages"] = session["messages"][-MAX_HISTORY_TURNS:]


# ── 对话性问题检测 ─────────────────────────────────────

def _is_conversational(question):
    """检测是否为对话性问题（不需要RAG检索，直接回答）。"""
    q = question.lower().strip()
    identity_patterns = [
        "你是谁", "你叫什么", "你叫啥", "你是什么", "牛马是谁",
        "你是机器人", "你是人还是机器", "你是ai", "你是人工智能",
    ]
    capability_patterns = [
        "你能做什么", "你能为我做什么", "你会什么", "你能帮我什么",
        "你的功能", "你能干什么", "你会做什么", "你能帮我",
        "你有什么用", "你能提供什么",
    ]
    greeting_patterns = [
        "你好", "您好", "嗨", "hello", "hi", "在吗", "有人吗",
        "有人", "客服在吗",
    ]
    thanks_patterns = ["谢谢", "感谢", "thanks", "多谢", "谢了"]
    goodbye_patterns = ["再见", "拜拜", "bye", "88", "晚安"]

    for p in identity_patterns:
        if p in q:
            return "identity"
    for p in capability_patterns:
        if p in q:
            return "capability"
    for p in greeting_patterns:
        if p in q:
            return "greeting"
    for p in thanks_patterns:
        if p in q:
            return "thanks"
    for p in goodbye_patterns:
        if p in q:
            return "goodbye"
    return None


def _conversational_response(kind):
    """返回对话性问题的内置回复。"""
    responses = {
        "identity": (
            "我是电商客服助手「牛马」🐂，专门为您提供商品咨询和售后服务。\n\n"
            "我可以帮助您：\n"
            "1. **商品信息查询** — 查询商品详情、价格、尺码、库存等\n"
            "2. **商品更换建议** — 换货政策、换码流程\n"
            "3. **退换货处理** — 退货政策、退款进度\n"
            "4. **物流咨询** — 发货时间、运费、配送时效\n"
            "5. **尺码推荐** — 根据您的身材推荐合适尺码\n\n"
            "请问有什么可以帮您的？"
        ),
        "capability": (
            "您好，我是客服助手「牛马」，可以为您做以下事情：\n\n"
            "1. **商品信息查询** — 查询商品详情、价格、尺码、库存、参数等\n"
            "2. **商品更换建议** — 换货政策、换码流程咨询\n"
            "3. **退换货处理** — 退货政策、退款进度查询\n"
            "4. **物流咨询** — 发货时间、运费、配送时效\n"
            "5. **尺码推荐** — 根据您的身材推荐合适尺码\n\n"
            "请问您需要哪方面的帮助？"
        ),
        "greeting": "您好，我是客服助手「牛马」，请问有什么可以帮您？",
        "thanks": "不客气！如果还有其他问题，随时可以问我。😊",
        "goodbye": "感谢您的咨询，祝您购物愉快！再见！👋",
    }
    return responses.get(kind, None)


def _is_product_listing_query(question):
    """检测是否为商品列表查询（如「有哪些商品」），需要限定检索来源。"""
    q = question.lower()
    patterns = ["哪些商品", "什么商品", "有什么商品", "有哪些产品",
                "什么产品", "有什么产品", "有哪些东西", "卖什么",
                "都有什么", "商品列表", "产品列表"]
    return any(p in q for p in patterns)


# ── 退款/转人工意图检测 ─────────────────────────────────

REFUND_PATTERNS = [
    "退款", "退钱", "申请退款", "我要退款", "退还货款",
    "退款申请", "帮我退款", "退货退款", "想要退款",
]

HUMAN_AGENT_PATTERNS = [
    "转人工", "人工客服", "找人工", "转接人工", "我要人工",
    "转接客服", "找人工客服", "接通人工", "人工服务",
    "转真人", "真人客服", "找客服", "接人工",
]


def _is_refund_intent(question):
    q = question.lower()
    return any(p in q for p in REFUND_PATTERNS)


def _is_human_agent_intent(question):
    q = question.lower()
    return any(p in q for p in HUMAN_AGENT_PATTERNS)


# ── 订单查询意图检测 ───────────────────────────────────

ORDER_QUERY_PATTERNS = [
    "我的订单", "订单状态", "订单查询", "查订单", "查一下订单",
    "订单号", "物流状态", "快递单号", "发货了吗", "发货了没",
    "到哪了", "什么时候到", "我的快递", "包裹状态",
    "订单进度", "查看订单", "订单情况", "订单怎么样",
]

import re

def _is_order_query(question):
    q = question.lower()
    if any(p in q for p in ORDER_QUERY_PATTERNS):
        return True
    if re.search(r'[A-Z]{2}\d{8,}', question.upper()):
        return True
    if re.search(r'订单.*\d{6,}', q) or re.search(r'\d{6,}.*订单', q):
        return True
    return False


def _extract_order_id(question):
    m = re.search(r'[A-Z]{2}\d{8,}', question.upper())
    if m:
        return m.group(0)
    m = re.search(r'(?:订单|单号|编号)[号:：\s]*([A-Za-z0-9\-]{6,})', question)
    if m:
        return m.group(1)
    return None


def _query_orders(username, question):
    """查询用户订单"""
    db = get_db()
    try:
        order_id = _extract_order_id(question)

        if order_id:
            orders = db.query(Order).filter(
                Order.order_id == order_id,
                Order.customer_username == username,
            ).all()
        else:
            orders = db.query(Order).filter(
                Order.customer_username == username,
            ).order_by(Order.created_at.desc()).all()

        if not orders:
            return None, "未找到您的订单信息。如果您有具体订单号，请提供以便精确查询。"

        status_map = {
            "pending": "待付款",
            "paid": "已付款",
            "shipped": "已发货",
            "delivered": "已签收",
            "refunded": "已退款",
            "cancelled": "已取消",
        }

        if order_id:
            order = orders[0]
            parts = [
                f"订单号：{order.order_id}",
                f"商品：{order.product_name}",
                f"数量：{order.quantity}",
                f"金额：{order.amount}元",
                f"状态：{status_map.get(order.status, order.status)}",
            ]
            if order.tracking_number:
                parts.append(f"快递单号：{order.tracking_number}")
            if order.shipping_address:
                parts.append(f"收货地址：{order.shipping_address}")
            return [order.to_dict()], "；".join(parts)
        else:
            lines = [f"您共有 {len(orders)} 个订单："]
            for o in orders:
                s = status_map.get(o.status, o.status)
                lines.append(f"• {o.order_id} | {o.product_name} ×{o.quantity} | {o.amount}元 | {s}")
            return [o.to_dict() for o in orders], "\n".join(lines)
    except Exception as e:
        return None, f"订单查询失败: {e}"
    finally:
        close_db(db)


def _create_ticket(username, ticket_type, subject, description="", order_id=""):
    ticket_id = f"TK-{int(time.time())}{uuid.uuid4().hex[:4].upper()}"
    now = int(time.time())
    db = get_db()
    try:
        ticket = Ticket(
            ticket_id=ticket_id,
            username=username,
            ticket_type=ticket_type,
            subject=subject,
            description=description,
            order_id=order_id,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(ticket)
        db.commit()
        return ticket.to_dict()
    except Exception as e:
        db.rollback()
        return None
    finally:
        close_db(db)


# ── API Key 持久化 ──────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(config):
    try:
        CONFIG_FILE.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"[Config] 保存失败: {e}")


_config = load_config()
api_key = os.environ.get("DEEPSEEK_API_KEY") or _config.get("api_key")


def get_user_dir(username):
    user_dir = HISTORY_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_conversation_filepath(username, conv_id):
    return get_user_dir(username) / f"{conv_id}.json"


def list_conversations(username):
    user_dir = get_user_dir(username)
    convs = []
    for f in sorted(user_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            convs.append({
                "id": data["id"],
                "title": data["title"],
                "created_at": data.get("created_at", 0),
                "updated_at": data.get("updated_at", 0),
                "message_count": len(data.get("messages", [])),
            })
        except Exception:
            continue
    return convs


def load_conversation(username, conv_id):
    filepath = get_conversation_filepath(username, conv_id)
    if filepath.exists():
        return json.loads(filepath.read_text(encoding="utf-8"))
    return None


def save_conversation(username, conv_id, data):
    filepath = get_conversation_filepath(username, conv_id)
    filepath.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def delete_conversation(username, conv_id):
    filepath = get_conversation_filepath(username, conv_id)
    if filepath.exists():
        filepath.unlink()
        return True
    return False


# ── 认证装饰器 ──────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.cookies.get("auth_token", "")
        session = auth.validate(token)
        if not session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录或登录已过期"}), 401
            return redirect("/login")
        request.user = session
        request.token = token
        return f(*args, **kwargs)
    return decorated


# ── 页面路由 ────────────────────────────────────────────

@app.route("/")
def index():
    token = request.cookies.get("auth_token", "")
    session = auth.validate(token)
    if not session:
        return redirect("/login")
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


# ── 认证 API ────────────────────────────────────────────

@app.route("/api/register", methods=["POST"])
@rate_limit(max_requests=5, window=60, is_auth=True)
def api_register():
    data = request.get_json(silent=True) or {}
    username = sanitize_input(data.get("username", ""), max_length=50)
    password = data.get("password", "")
    gender = data.get("gender", "")
    result = auth.register(username, password, gender)
    if result["success"]:
        login_result = auth.login(username, password)
        if login_result["success"]:
            resp = jsonify(login_result)
            resp.set_cookie("auth_token", login_result["token"], httponly=True, max_age=86400, samesite="Strict")
            return resp
    return jsonify(result)


@app.route("/api/login", methods=["POST"])
@rate_limit(max_requests=5, window=60, is_auth=True)
def api_login():
    data = request.get_json(silent=True) or {}
    username = sanitize_input(data.get("username", ""), max_length=50)
    password = data.get("password", "")
    result = auth.login(username, password)
    if result["success"]:
        resp = jsonify(result)
        resp.set_cookie("auth_token", result["token"], httponly=True, max_age=86400, samesite="Strict")
        return resp
    return jsonify(result)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("auth_token", "")
    result = auth.logout(token)
    resp = jsonify(result)
    resp.delete_cookie("auth_token")
    return resp


@app.route("/api/user")
@login_required
def api_user():
    return jsonify({
        "username": request.user["username"],
        "gender": request.user["gender"],
    })


@app.route("/api/session", methods=["GET"])
@login_required
def api_session():
    session_id = str(uuid.uuid4())
    _get_session(session_id)
    return jsonify({"session_id": session_id})


# ── 设置 API ────────────────────────────────────────────

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    global api_key
    if request.method == "GET":
        return jsonify({
            "llm_enabled": api_key is not None,
            "model": LLMClient.MODEL,
        })

    data = request.get_json(silent=True) or {}
    key = (data.get("api_key") or "").strip()
    if key:
        api_key = key
        save_config({"api_key": key})
        return jsonify({
            "success": True,
            "message": "API 密钥已设置，AI 回答已启用",
            "llm_enabled": True,
        })
    else:
        api_key = None
        save_config({})
        return jsonify({
            "success": True,
            "message": "API 密钥已清除，已切换为离线 RAG 检索模式",
            "llm_enabled": False,
        })


# ── 聊天 API (含 Query 改写) ────────────────────────────

@app.route("/api/query", methods=["POST"])
@login_required
@rate_limit(max_requests=30, window=60)
def api_query():
    global api_key
    data = request.get_json(silent=True) or {}
    question = sanitize_input(data.get("question", ""), max_length=2000)
    top_k = data.get("top_k", 5)
    session_id = data.get("session_id", "")
    conv_id = data.get("conversation_id", "")
    username = request.user["username"]

    if not question:
        return jsonify({"error": "问题不能为空"}), 400

    if is_malicious_input(question):
        return jsonify({"error": "输入包含不安全内容"}), 400

    history = []
    if session_id:
        session = _get_session(session_id)
        history = list(session["messages"])

    # 加载对话历史
    conv = None
    if conv_id:
        conv = load_conversation(username, conv_id)
    if conv is None:
        conv_id = str(uuid.uuid4())[:8]
        conv = {
            "id": conv_id,
            "title": question[:30],
            "messages": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    conv_history = [{"role": m["role"], "content": m["content"]} for m in conv["messages"][-6:]]

    def generate():
        global api_key
        search_question = question

        # ── 对话性问题：跳过RAG，直接处理 ──
        conv_kind = _is_conversational(question)
        if conv_kind:
            answer = _conversational_response(conv_kind)
            yield f"data: {json.dumps({'type': 'sources', 'data': []}, ensure_ascii=False)}\n\n"
            for i in range(0, len(answer), 20):
                chunk = answer[i:i+20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)
            conv["messages"].append({"role": "user", "content": question})
            conv["messages"].append({"role": "assistant", "content": answer})
            conv["updated_at"] = time.time()
            save_conversation(username, conv_id, conv)
            _add_to_history(session_id, "user", question)
            _add_to_history(session_id, "assistant", answer)
            yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        # ── 退款意图：创建工单，等待人工确认 ──
        if _is_refund_intent(question):
            ticket = _create_ticket(username, "refund", question[:200], question)
            if ticket:
                answer = (
                    f"已为您创建退款申请工单（工单号：{ticket['ticket_id']}）。\n\n"
                    f"退款流程说明：\n"
                    f"1. 您的退款申请已提交，等待人工客服确认\n"
                    f"2. 人工客服确认后，将自动为您处理退款\n"
                    f"3. 退款将在 1-3 个工作日内原路退回\n\n"
                    f"如需加快处理，请提供您的订单号。您也可以在侧边栏「我的工单」中查看进度。"
                )
            else:
                answer = "退款申请提交失败，请稍后重试或联系人工客服。"
            yield f"data: {json.dumps({'type': 'ticket', 'data': ticket}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'data': []}, ensure_ascii=False)}\n\n"
            for i in range(0, len(answer), 20):
                chunk = answer[i:i+20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)
            conv["messages"].append({"role": "user", "content": question})
            conv["messages"].append({"role": "assistant", "content": answer})
            conv["updated_at"] = time.time()
            save_conversation(username, conv_id, conv)
            _add_to_history(session_id, "user", question)
            _add_to_history(session_id, "assistant", answer)
            yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        # ── 转人工意图：创建工单，等待人工接入 ──
        if _is_human_agent_intent(question):
            ticket = _create_ticket(username, "human_agent", question[:200], question)
            if ticket:
                answer = (
                    f"正在为您转接人工客服（工单号：{ticket['ticket_id']}）。\n\n"
                    f"人工客服将尽快接入，请稍候...\n"
                    f"在等待期间，您仍然可以继续向我提问，我会尽力为您解答。"
                )
            else:
                answer = "转接人工客服失败，请稍后重试。"
            yield f"data: {json.dumps({'type': 'ticket', 'data': ticket}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'data': []}, ensure_ascii=False)}\n\n"
            for i in range(0, len(answer), 20):
                chunk = answer[i:i+20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)
            conv["messages"].append({"role": "user", "content": question})
            conv["messages"].append({"role": "assistant", "content": answer})
            conv["updated_at"] = time.time()
            save_conversation(username, conv_id, conv)
            _add_to_history(session_id, "user", question)
            _add_to_history(session_id, "assistant", answer)
            yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        # ── 订单查询：查数据库，直接返回 ──
        if _is_order_query(question):
            orders, answer = _query_orders(username, question)
            if orders:
                sources = [{"source": "订单系统", "order_id": o.get("order_id", "")} for o in orders]
            else:
                sources = []
            yield f"data: {json.dumps({'type': 'sources', 'data': sources}, ensure_ascii=False)}\n\n"
            for i in range(0, len(answer), 20):
                chunk = answer[i:i+20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)
            conv["messages"].append({"role": "user", "content": question})
            conv["messages"].append({"role": "assistant", "content": answer})
            conv["updated_at"] = time.time()
            save_conversation(username, conv_id, conv)
            _add_to_history(session_id, "user", question)
            _add_to_history(session_id, "assistant", answer)
            logger.audit("user", username, "order_query", "order", "", f"查询: {question[:100]}")
            yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        # ── 有 API Key：LLM 改写 + 流式回答 ──
        if api_key:
            client = LLMClient(api_key)

            # 1) Query 改写（内部使用，不展示给用户）
            rewritten, rewrite_error = client.rewrite_query(
                question, conv_history
            )
            if rewritten:
                search_question = rewritten
            elif rewrite_error:
                yield f"data: {json.dumps({'type': 'rewrite_error', 'data': rewrite_error}, ensure_ascii=False)}\n\n"

            # 2) RAG 检索（使用改写后的查询，提高阈值过滤低质量结果）
            results = rag.query(search_question, top_k=top_k, threshold=0.15)
            sources = [{"source": r["source"], "score": r["score"]} for r in results]
            yield f"data: {json.dumps({'type': 'sources', 'data': sources}, ensure_ascii=False)}\n\n"

            # 即使无检索结果，仍交给 LLM 回答（处理打招呼等场景）
            # 3) LLM 流式回答
            full_response = []
            has_output = False
            llm_error = None

            try:
                for chunk in client.chat_stream(search_question, results, history=conv_history):
                    if chunk.startswith("__ERROR__:"):
                        llm_error = chunk[len("__ERROR__:"):]
                        break
                    has_output = True
                    full_response.append(chunk)
                    yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
            except Exception as e:
                llm_error = str(e)

            if llm_error:
                if has_output:
                    note = f"\n\n⚠️ AI 回答中断（{llm_error}），以下为知识库补充检索结果：\n\n"
                else:
                    note = f"⚠️ AI 回答暂不可用（{llm_error}），以下为知识库检索结果：\n\n"
                note += "\n\n".join(r["text"] for r in results)
                full_response.append(note)
                yield f"data: {json.dumps({'type': 'delta', 'data': note}, ensure_ascii=False)}\n\n"

            answer_text = "".join(full_response)
            conv["messages"].append({"role": "user", "content": question})
            conv["messages"].append({"role": "assistant", "content": answer_text})
            conv["updated_at"] = time.time()
            save_conversation(username, conv_id, conv)
            _add_to_history(session_id, "user", question)
            _add_to_history(session_id, "assistant", answer_text)

        # ── 无 API Key：离线 RAG 检索 ──
        else:
            results = rag.query(search_question, top_k=top_k, threshold=0.25)

            # 商品列表查询：只保留 products 来源的结果，过滤掉 FAQ/物流等
            if _is_product_listing_query(question) and results:
                product_results = [r for r in results if "products" in r.get("source", "")]
                if product_results:
                    results = product_results

            # 检查最高相关度，低相关度视为未检索到
            if not results or (results and results[0]["score"] < 0.25):
                answer = "抱歉，我在知识库中未检索到与您问题相关的信息。请尝试换一种提问方式，或上传更多商品文档到知识库中。"
                yield f"data: {json.dumps({'type': 'sources', 'data': []}, ensure_ascii=False)}\n\n"
                for i in range(0, len(answer), 20):
                    chunk = answer[i:i+20]
                    yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                    time.sleep(0.03)
                conv["messages"].append({"role": "user", "content": question})
                conv["messages"].append({"role": "assistant", "content": answer})
                conv["updated_at"] = time.time()
                save_conversation(username, conv_id, conv)
                _add_to_history(session_id, "user", question)
                _add_to_history(session_id, "assistant", answer)
                yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return

            sources = [{"source": r["source"], "score": r["score"]} for r in results]
            yield f"data: {json.dumps({'type': 'sources', 'data': sources}, ensure_ascii=False)}\n\n"

            answer = "\n\n---\n\n".join(r["text"] for r in results)
            for i in range(0, len(answer), 20):
                chunk = answer[i:i+20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)

            conv["messages"].append({"role": "user", "content": question})
            conv["messages"].append({"role": "assistant", "content": answer})
            conv["updated_at"] = time.time()
            save_conversation(username, conv_id, conv)
            _add_to_history(session_id, "user", question)
            _add_to_history(session_id, "assistant", answer)

        yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── 历史记录 API ────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
@login_required
def api_history_list():
    username = request.user["username"]
    convs = list_conversations(username)
    return jsonify({"conversations": convs})


@app.route("/api/history/<conv_id>", methods=["GET"])
@login_required
def api_history_get(conv_id):
    username = request.user["username"]
    conv = load_conversation(username, conv_id)
    if conv:
        return jsonify(conv)
    return jsonify({"error": "对话不存在"}), 404


@app.route("/api/history/<conv_id>", methods=["DELETE"])
@login_required
def api_history_delete(conv_id):
    username = request.user["username"]
    if delete_conversation(username, conv_id):
        return jsonify({"success": True, "message": "对话已删除"})
    return jsonify({"error": "对话不存在"}), 404


@app.route("/api/history", methods=["POST"])
@login_required
def api_history_create():
    username = request.user["username"]
    data = request.get_json(silent=True) or {}
    title = data.get("title", "新对话")
    conv_id = str(uuid.uuid4())[:8]
    conv = {
        "id": conv_id,
        "title": title,
        "messages": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    save_conversation(username, conv_id, conv)
    return jsonify({"success": True, "conversation_id": conv_id})


# ── 文档管理 API ────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window=60)
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "未检测到上传文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    ok, msg = validate_file_upload(file.filename, len(file.read()), SUPPORTED_EXTENSIONS)
    if not ok:
        return jsonify({"error": msg}), 400

    file.seek(0)
    content = file.read()
    name = rag.add_document(file.filename, content)

    return jsonify({
        "success": True,
        "message": f"文件 {name} 上传成功，索引已更新",
        "stats": rag.get_stats(),
    })


@app.route("/api/documents", methods=["GET"])
@login_required
def api_documents():
    return jsonify({"documents": rag.list_documents()})


@app.route("/api/delete", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window=60)
def api_delete():
    data = request.get_json(silent=True) or {}
    filename = sanitize_input(data.get("filename", ""), max_length=200)

    if not filename:
        return jsonify({"error": "文件名不能为空"}), 400
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "非法文件名"}), 400

    if rag.delete_document(filename):
        return jsonify({
            "success": True,
            "message": f"文件 {filename} 已删除，索引已更新",
            "stats": rag.get_stats(),
        })
    else:
        return jsonify({"error": f"文件 {filename} 不存在"}), 404


@app.route("/api/stats", methods=["GET"])
@login_required
def api_stats():
    return jsonify(rag.get_stats())


# ── 工单 API ────────────────────────────────────────────

@app.route("/api/ticket/list", methods=["GET"])
@login_required
def api_ticket_list():
    username = request.user["username"]
    db = get_db()
    try:
        tickets = db.query(Ticket).filter(
            Ticket.username == username
        ).order_by(Ticket.created_at.desc()).all()
        return jsonify({"tickets": [t.to_dict() for t in tickets]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


@app.route("/api/ticket/create", methods=["POST"])
@login_required
@rate_limit(max_requests=5, window=60)
def api_ticket_create():
    username = request.user["username"]
    data = request.get_json(silent=True) or {}
    ticket_type = data.get("ticket_type", "")
    subject = sanitize_input(data.get("subject", ""), max_length=200)
    description = sanitize_input(data.get("description", ""), max_length=2000)
    order_id = sanitize_input(data.get("order_id", ""), max_length=50)

    if ticket_type not in ("refund", "human_agent"):
        return jsonify({"error": "工单类型无效"}), 400
    if not subject:
        return jsonify({"error": "标题不能为空"}), 400

    ticket = _create_ticket(username, ticket_type, subject, description, order_id)
    if ticket:
        return jsonify({"success": True, "ticket": ticket})
    return jsonify({"error": "创建工单失败"}), 500


@app.route("/api/ticket/<ticket_id>", methods=["GET"])
@login_required
def api_ticket_get(ticket_id):
    username = request.user["username"]
    db = get_db()
    try:
        ticket = db.query(Ticket).filter(
            Ticket.ticket_id == ticket_id,
            Ticket.username == username,
        ).first()
        if ticket:
            return jsonify(ticket.to_dict())
        return jsonify({"error": "工单不存在"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


@app.route("/api/ticket/<ticket_id>/process", methods=["POST"])
@login_required
def api_ticket_process(ticket_id):
    username = request.user["username"]
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    reply = sanitize_input(data.get("reply", ""), max_length=1000)

    if action not in ("approve", "reject", "resolve"):
        return jsonify({"error": "操作类型无效"}), 400

    db = get_db()
    try:
        ticket = db.query(Ticket).filter(
            Ticket.ticket_id == ticket_id,
            Ticket.username == username,
        ).first()
        if not ticket:
            return jsonify({"error": "工单不存在"}), 404

        status_map = {
            "approve": "approved",
            "reject": "rejected",
            "resolve": "resolved",
        }
        ticket.status = status_map[action]
        ticket.agent_reply = reply
        ticket.updated_at = int(time.time())
        db.commit()
        return jsonify({"success": True, "ticket": ticket.to_dict()})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


@app.route("/api/download/<filename>", methods=["GET"])
@login_required
def api_download(filename):
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "非法文件名"}), 400
    filepath = DATA_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_from_directory(str(DATA_DIR), filename, as_attachment=True)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "stats": rag.get_stats()})


if __name__ == "__main__":
    print("=" * 55)
    print("  电商智能客服系统 (生产加固版)")
    print(f"  数据目录: {DATA_DIR}")
    print(f"  索引目录: {INDEX_DIR}")
    print(f"  数据库: ecommerce.db (SQLite + bcrypt + JWT)")
    print(f"  安全: 安全头/限流/输入消毒/恶意检测/文件验证")
    print(f"  引擎状态: {rag.get_stats()}")
    print(f"  LLM 模型: {LLMClient.MODEL}")
    print(f"  LLM 状态: {'已启用' if api_key else '未启用（离线 RAG 检索模式）'}")
    print("  访问地址: http://127.0.0.1:5000")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
