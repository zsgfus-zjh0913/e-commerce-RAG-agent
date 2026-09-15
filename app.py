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
from business_tools import (
    TOOL_SCHEMAS, TOOL_SOURCES, TOOL_REGISTRY, execute_tool,
    TICKET_STATUS_ZH, TICKET_TYPE_ZH,
)
from auth_manager import AuthManager
from database import init_db, get_db, close_db, Ticket, Order, Product, ChatMessage, IS_SQLITE, DATABASE_URL
from security import (
    apply_security_headers, sanitize_input, is_malicious_input,
    rate_limit, validate_file_upload,
)
from agent_routes import agent_bp
from multimodal_engine import ProductMultimodalEngine
import logger
import redis_manager
import qa_cache

init_db()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", os.environ.get("JWT_SECRET", "ecommerce-cs-secret-2026"))
app.register_blueprint(agent_bp)


@app.after_request
def after_request(response):
    apply_security_headers(response)
    return response


@app.route("/api/rating", methods=["POST"])
def submit_rating():
    """接收用户对客服回答的星级评价。"""
    from functools import wraps
    token = request.cookies.get("token")
    user_info = auth.verify_token(token) if token else None
    if not user_info:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    rating = data.get("rating")
    conversation_id = data.get("conversation_id", "")
    question = data.get("question", "")
    if not rating or not (1 <= int(rating) <= 5):
        return jsonify({"error": "评分需为1-5"}), 400
    logger.audit(
        user_info["username"], user_info["username"], "rating",
        "chat", conversation_id,
        f"评分:{rating}星 问题:{question[:80]}"
    )
    return jsonify({"success": True, "message": "感谢您的评价！"})


@app.route("/health")
def health_check():
    """健康检查端点 — 用于负载均衡器/Nginx 探活"""
    storage = redis_manager.health_check()
    stats = rag.get_stats()
    return jsonify({
        "status": "ok",
        "database": {
            "type": "sqlite" if IS_SQLITE else "external",
            "url": DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else "local",
        },
        "storage": storage,
        "rag": {
            "chunks": stats.get("total_chunks", 0),
            "documents": stats.get("total_documents", 0),
        },
        "llm_enabled": bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("API_KEY")),
        "multimodal": multimodal.status(),
        "timestamp": int(time.time()),
    })

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRODUCT_DOCUMENT_NAME = "商品信息.md"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
INDEX_DIR = BASE_DIR / "index"
INDEX_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_DIR = BASE_DIR / "histories"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

rag = RAGEngine(str(DATA_DIR), str(INDEX_DIR))
_ = rag.encoder
_ = rag.reranker
multimodal = ProductMultimodalEngine(str(DATA_DIR), str(INDEX_DIR))
multimodal.ensure_catalog_index()

auth = AuthManager()
qa_cache = qa_cache.init_cache(lambda: rag.encoder)

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
        "你是谁", "你叫什么", "你叫啥", "你是什么", "暖小助是谁",
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
            "我是电商客服助手「暖小助」🌸，专门为您提供商品咨询和售后服务。\n\n"
            "我可以帮助您：\n"
            "1. **商品信息查询** — 查询商品详情、价格、尺码、库存等\n"
            "2. **商品更换建议** — 换货政策、换码流程\n"
            "3. **退换货处理** — 退货政策、退款进度\n"
            "4. **物流咨询** — 发货时间、运费、配送时效\n"
            "5. **尺码推荐** — 根据您的身材推荐合适尺码\n\n"
            "请问有什么可以帮您的？"
        ),
        "capability": (
            "您好，我是客服助手「暖小助」，可以为您做以下事情：\n\n"
            "1. **商品信息查询** — 查询商品详情、价格、尺码、库存、参数等\n"
            "2. **商品更换建议** — 换货政策、换码流程咨询\n"
            "3. **退换货处理** — 退货政策、退款进度查询\n"
            "4. **物流咨询** — 发货时间、运费、配送时效\n"
            "5. **尺码推荐** — 根据您的身材推荐合适尺码\n\n"
            "请问您需要哪方面的帮助？"
        ),
        "greeting": "您好，我是客服助手「暖小助」，请问有什么可以帮您？",
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


def _product_catalog_response():
    """直接根据商品表中的实时目录生成自然、稳定的商品列表回答。"""
    db = get_db()
    try:
        products = db.query(Product).order_by(Product.category, Product.id).all()
    finally:
        close_db(db)

    if not products:
        return "目前暂时没有可展示的在售商品，您可以稍后再来看看。"

    lines = ["目前在售卖的产品有："]
    for index, product in enumerate(products, 1):
        price = (product.price or "").strip()
        price_text = f"，{price}元" if price else ""
        category = f"（{product.category}）" if product.category else ""
        lines.append(f"{index}. {product.name}{category}{price_text}")
    lines.append("")
    lines.append("如果您想了解某款产品的价格、规格或库存，直接告诉我商品名称就可以。")
    return "\n".join(lines)


def _product_context(product):
    attributes = {}
    if product.attributes:
        try:
            attributes = json.loads(product.attributes)
        except Exception:
            attributes = {}
    lines = [
        f"商品编号：{product.product_id}",
        f"商品名称：{product.name}",
    ]
    if product.brand:
        lines.append(f"品牌：{product.brand}")
    if product.category:
        lines.append(f"分类：{product.category}")
    if product.price:
        lines.append(f"价格：{product.price}元")
    if product.stock:
        lines.append(f"库存：{product.stock}")
    if product.description:
        lines.append(f"商品描述：{product.description}")
    for key, value in attributes.items():
        if value:
            lines.append(f"{key}：{value}")
    return "\n".join(lines)


def _multimodal_fallback_answer(question, matches, ocr_text):
    if not matches:
        if ocr_text:
            return (
                "我识别到图片中的文字是：“"
                f"{ocr_text}”。暂时没有匹配到确定的商品，"
                "请补充吊牌、尺码标或商品正面照片，我可以继续帮您判断。"
            )
        return (
            "这张图片暂时不够清晰，我无法准确匹配商品。"
            "请拍清楚商品正面、吊牌、型号或尺码标后再发给我。"
        )

    top = matches[0]
    if top.get("exact_match"):
        lines = [
            f"根据图片中的型号或文字，已识别到商品：{top['name']}"
            f"（{top['product_id']}）。"
        ]
    else:
        lines = [f"根据图片，最接近的商品是 {top['name']}（{top['product_id']}）。"]
    attributes = top.get("attributes") or {}
    for key in ("型号", "规格", "尺码", "颜色", "材质", "面料", "容量", "功率"):
        value = attributes.get(key)
        if value:
            lines.append(f"{key}：{value}")
    if top.get("price"):
        lines.append(f"当前价格：{top['price']}元。")
    if top.get("stock"):
        lines.append(f"库存：{top['stock']}。")
    if top.get("description"):
        lines.append(f"商品说明：{top['description']}")
    if len(matches) > 1:
        alternatives = "、".join(
            f"{item['name']}（{item['product_id']}）"
            for item in matches[1:3]
        )
        lines.append(f"另外两个相似候选是：{alternatives}。")
    return "\n\n".join(lines)


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
    # 格式1: DD20260828001 (2+大写字母 + 8+位数字)
    m = re.search(r'[A-Z]{2}\d{8,}', question.upper())
    if m:
        return m.group(0)
    # 格式2: 订单号DD20260828001 / 单号:DD20260828001
    m = re.search(r'(?:订单|单号|编号)[号:：\s]*([A-Za-z0-9\-]{6,})', question)
    if m:
        return m.group(1).upper()
    # 格式3: DD-2026-0828-001 (带分隔符)
    m = re.search(r'([A-Z]{2}[-]?\d{4}[-]?\d{4}[-]?\d{3})', question.upper())
    if m:
        return m.group(1).replace('-', '')
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
            if order_id:
                other = db.query(Order).filter(Order.order_id == order_id).first()
                if other:
                    return None, (
                        f"订单号 {order_id} 存在，但不属于当前登录账号（{username}）。\n"
                        f"该订单属于用户：{other.customer_username}。\n"
                        f"请使用该用户的账号登录后查询，或联系客服协助处理。"
                    )
                return None, f"未找到订单号为 {order_id} 的订单，请确认订单号是否正确。"
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


# ── 业务实时数据（Function Calling）支持 ─────────────────
# 接入真实业务系统后，工具的查询来源统一在 business_tools.py 中适配，
# 本段（门控/决策/兜底）无需改动。

PROGRESS_PATTERNS = [
    "进度", "到哪", "到哪一步", "处理到", "审核", "办得", "进行到",
    "什么情况", "好了吗", "通过了吗", "怎么样", "查询",
]
AFTERSALE_WORDS = ("售后", "退款", "退换", "退货", "换货", "工单", "返修")
GATE_PRODUCT_REALTIME = ("库存", "有货", "现货", "还有货", "剩多少", "补货", "断货", "缺货")
# 这些是知识型问题（政策/规则类），不触发业务工具，避免浪费一次 LLM 决策调用
GATE_SKIP_WORDS = ("政策", "规则", "流程", "能退吗", "可以退吗", "怎么退", "如何退",
                   "支持退", "运费险", "几天到", "多久到", "时效")


def _is_progress_query(question):
    """是否为查询处理进度的问法（如「退款到哪一步了」）。"""
    q = question.lower()
    return any(p in q for p in PROGRESS_PATTERNS)


def _is_after_sales_progress_query(question):
    """售后/退款/工单 + 进度问法 → 查工单进度（区别于「申请退款」的卡片流程）。"""
    q = question.lower()
    return _is_progress_query(q) and any(w in q for w in AFTERSALE_WORDS)


def _is_business_query(question):
    """门控：问题是否疑似需要实时业务数据（订单/物流/售后进度/实时库存）。

    仅为减少无效 LLM 调用而设；真正的决策由 function calling 的模型完成。
    """
    q = question.lower()
    if _is_order_query(question):
        return True
    if any(w in q for w in GATE_SKIP_WORDS):
        return False
    if any(w in q for w in AFTERSALE_WORDS):
        return True
    if any(w in q for w in GATE_PRODUCT_REALTIME):
        return True
    if any(w in q for w in ("物流", "快递", "发货", "签收", "收货")):
        return True
    return False


def _query_after_sales_progress(username):
    """确定性兜底：列出用户的售后/退款工单及处理进度。返回 (text, count)。"""
    db = get_db()
    try:
        rows = db.query(Ticket).filter(
            Ticket.username == username
        ).order_by(Ticket.created_at.desc()).limit(10).all()
    except Exception:
        rows = []
    finally:
        close_db(db)

    if not rows:
        return ("您名下暂时没有售后/退款工单记录。如需退款，请告知我对应的订单号，"
                "我可以协助您发起申请。", 0)
    lines = [f"您有 {len(rows)} 条售后/服务工单："]
    for t in rows:
        st = TICKET_STATUS_ZH.get(t.status, t.status)
        ty = TICKET_TYPE_ZH.get(t.ticket_type, t.ticket_type)
        line = f"• 工单 {t.ticket_id}（{ty}）｜{t.subject}｜{st}"
        if t.agent_reply:
            line += f"｜客服回复：{t.agent_reply}"
        lines.append(line)
    return "\n".join(lines), len(rows)


def _looks_like_clarification(content):
    """判断模型直接输出的文本是否属于「向用户澄清信息」而不是正式回答。

    澄清追问（如「请问您要查询哪个订单/哪个商品？」）可直接返回给用户；
    其他无工具输出的内容放行到 RAG 链路，让回答带上知识库上下文。
    """
    text = (content or "").strip()
    if not text:
        return False
    ask_markers = ("请问", "请提供", "请告知", "请确认", "麻烦您", "能告诉",
                   "哪个订单", "哪个商品", "订单号是", "是哪一个", "是哪个",
                   "哪个快递", "可以告诉我", "是否方便")
    if text.endswith(("？", "?", "吗", "呢")):
        return True
    return any(m in text for m in ask_markers)


def _business_tool_answer(api_key, username, question, history):
    """LLM function-calling：决策 → 执行工具 → 生成最终回答（一次性返回文本）。

    返回 dict: {"text": ..., "sources": [...]}；
    链路不可用（无 Key / 模型不支持工具 / 异常）时返回 None，由调用方走确定性兜底。
    """
    try:
        client = LLMClient(api_key)
        decision, err = client.decide_tools(question, history, TOOL_SCHEMAS)
        if err or not decision:
            return None

        calls = [c for c in decision.get("tool_calls", [])
                 if c.get("name") in TOOL_REGISTRY]
        if not calls:
            # 模型认为无需工具：若是澄清追问则直接返回给用户；
            # 否则视为知识型问题，放行到 RAG 链路回答（带知识库上下文更准）
            content = (decision.get("content") or "").strip()
            if content and _looks_like_clarification(content):
                return {"text": content, "sources": []}
            return None

        executed = []
        tool_messages = []
        for c in calls:
            name = c["name"]
            try:
                res = execute_tool(name, username, c.get("arguments") or {})
            except Exception as e:
                res = {"ok": False, "records": [], "text": f"工具执行异常：{e}"}
            executed.append(res)
            tool_messages.append({
                "role": "tool",
                "tool_call_id": c.get("id") or f"call_{name}",
                "content": json.dumps(res, ensure_ascii=False),
            })

        text, aerr = client.answer_with_tools(
            question, history,
            assistant_tool_message=decision.get("raw_message"),
            tool_messages=tool_messages,
        )
        if aerr or not text:
            # LLM 最终回答故障：把工具返回的友好文本直接作为兜底答案
            parts = [r.get("text") for r in executed if r.get("text")]
            text = ("以下为业务系统实时查询结果：\n\n" + "\n\n".join(parts)
                    if parts else None)

        sources = []
        for i, c in enumerate(calls):
            res = executed[i] if i < len(executed) else {}
            sources.append({
                "source": TOOL_SOURCES.get(c["name"], "业务系统"),
                "ok": bool(res.get("ok")),
                "count": len(res.get("records") or []),
            })
        return {"text": text, "sources": sources}
    except Exception as e:
        print(f"[Tool] function-calling 链路异常: {e}")
        return None


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
            qa_cache.store(question, answer, "conversational")
            yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        # ── 退款意图（申请类）：查询用户订单，发送退款卡片 ──
        # 注：进度问法（如「退款到哪一步了」）不走此卡片流程，交由业务工具查工单进度
        if _is_refund_intent(question) and not _is_progress_query(question):
            order_id = _extract_order_id(question)
            db = get_db()
            try:
                if order_id:
                    orders_data = [o.to_dict() for o in db.query(Order).filter(
                        Order.order_id == order_id,
                        Order.customer_username == username,
                    ).all()]
                else:
                    orders_data = [o.to_dict() for o in db.query(Order).filter(
                        Order.customer_username == username,
                    ).order_by(Order.created_at.desc()).all()]
            except Exception:
                orders_data = []
            finally:
                close_db(db)

            status_map = {
                "pending": "待付款", "paid": "已付款",
                "shipped": "已发货", "delivered": "已签收",
                "refunded": "已退款", "cancelled": "已取消",
            }
            for o in orders_data:
                o["status_text"] = status_map.get(o["status"], o["status"])

            if orders_data:
                answer = "请选择需要退款的订单，点击「确认退款」后将由人工客服审核处理："
                card_data = {"orders": orders_data}
            elif order_id:
                answer = f"未找到订单号为 {order_id} 的订单，或该订单不属于您的账号。请核实订单号后重试。"
                card_data = None
            else:
                answer = "您名下暂无订单记录。如有订单号，请直接提供以便查询。"
                card_data = None

            yield f"data: {json.dumps({'type': 'sources', 'data': []}, ensure_ascii=False)}\n\n"
            if card_data:
                yield f"data: {json.dumps({'type': 'refund_card', 'data': card_data}, ensure_ascii=False)}\n\n"
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

        # ── 商品目录查询：直接按商品表生成自然列表，避免模型自由发挥 ──
        if _is_product_listing_query(question):
            answer = _product_catalog_response()
            sources = [{"source": PRODUCT_DOCUMENT_NAME}]
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
            yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            return

        # ── 业务实时数据（订单/物流/售后进度/实时库存）──
        # 有 API Key：LLM function calling 决策并查询；无 Key 或 LLM 不可用：确定性兜底
        if _is_business_query(question):
            sources = []
            answer = None
            if api_key:
                res = _business_tool_answer(api_key, username, question, conv_history)
                if res and res.get("text"):
                    answer = res["text"]
                    sources = res.get("sources", [])

            if answer is None:
                # 确定性兜底（离线模式 / LLM 不可用 / 未调用工具）
                if _is_order_query(question):
                    orders, answer = _query_orders(username, question)
                    if orders:
                        sources = [{"source": "订单系统",
                                    "order_id": o.get("order_id", "")} for o in orders]
                elif _is_after_sales_progress_query(question):
                    answer, ticket_count = _query_after_sales_progress(username)
                    sources = [{"source": "售后工单", "count": ticket_count}]

            if answer is not None:
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
                logger.audit("user", username, "business_query", "order",
                             "", f"查询: {question[:100]}")
                qa_cache.store(question, answer, "tool")
                yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return

        # ── 自学习问答缓存：命中则直接复用已存答案 ──
        cached = qa_cache.find_match(question)
        if cached:
            answer = cached["answer"]
            qa_cache.record_hit(cached["id"])
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
            if not llm_error and answer_text:
                qa_cache.store(question, answer_text, "llm")

        # ── 无 API Key：离线 RAG 检索 ──
        else:
            results = rag.query(search_question, top_k=top_k, threshold=0.25)

            # 商品列表查询：只保留统一商品文档，过滤掉 FAQ/物流等
            if _is_product_listing_query(question) and results:
                product_results = [
                    r for r in results
                    if r.get("source") == PRODUCT_DOCUMENT_NAME
                    or r.get("meta", {}).get("category") == "product"
                ]
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
            qa_cache.store(question, answer, "rag")

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


@app.route("/api/query/image", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window=60)
def api_query_image():
    """商品图片检索：OCR + 图像向量 + 商品文本 RAG。"""
    global api_key
    username = request.user["username"]
    question = sanitize_input(
        request.form.get("question", ""), max_length=2000
    )
    session_id = request.form.get("session_id", "")
    conv_id = request.form.get("conversation_id", "")
    image_file = request.files.get("image")

    if not image_file or not image_file.filename:
        return jsonify({"error": "请上传商品图片"}), 400
    if Path(image_file.filename).suffix.lower() not in IMAGE_EXTENSIONS:
        return jsonify({"error": "仅支持 JPG、PNG、WebP、BMP 图片"}), 400
    if question and is_malicious_input(question):
        return jsonify({"error": "输入包含不安全内容"}), 400

    content = image_file.read()
    if not content:
        return jsonify({"error": "图片内容为空"}), 400
    if len(content) > 10 * 1024 * 1024:
        return jsonify({"error": "图片不能超过 10MB"}), 400

    try:
        image = multimodal.load_image(content)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    analysis = multimodal.search(image, question=question, top_k=3)
    ocr_text = analysis.get("ocr_text", "")
    raw_matches = analysis.get("results", [])
    # 提高匹配门槛至 0.60，低于此分数的候选直接丢弃，宁缺毋滥
    min_score = float(os.environ.get("MULTIMODAL_MIN_SCORE", "0.60"))
    raw_matches = [
        item for item in raw_matches if item.get("final_score", 0) >= min_score
    ]

    product_matches = []
    if raw_matches:
        product_ids = [item["product_id"] for item in raw_matches]
        db = get_db()
        try:
            products = {
                product.product_id: product
                for product in db.query(Product).filter(
                    Product.product_id.in_(product_ids)
                ).all()
            }
        finally:
            close_db(db)
        for item in raw_matches:
            product = products.get(item["product_id"])
            if not product:
                continue
            try:
                attributes = json.loads(product.attributes) if product.attributes else {}
            except Exception:
                attributes = {}
            merged = dict(item)
            merged.update({
                "name": product.name,
                "price": product.price,
                "stock": product.stock,
                "description": product.description,
                "attributes": attributes,
                "exact_match": "命中商品编号或型号" in item.get("matched_evidence", []),
                "context": _product_context(product),
            })
            product_matches.append(merged)

    search_text = " ".join(filter(None, [
        question,
        ocr_text,
        " ".join(item["name"] for item in product_matches),
    ]))
    text_results = []
    if search_text:
        text_results = rag.query(search_text, top_k=4, threshold=0.1)

    context_chunks = []
    for item in product_matches:
        context_chunks.append({
            "source": PRODUCT_DOCUMENT_NAME,
            "text": item["context"],
        })
    for result in text_results:
        if any(
            result["text"] in chunk["text"]
            for chunk in context_chunks
        ):
            continue
        context_chunks.append({
            "source": result["source"],
            "text": result["text"],
        })

    sources = [{
        "source": PRODUCT_DOCUMENT_NAME,
        "product_id": item["product_id"],
        "image_score": item["image_score"],
        "ocr_score": item["ocr_score"],
        "final_score": item["final_score"],
    } for item in product_matches]
    if not sources:
        sources = [
            {"source": result["source"], "score": result["score"]}
            for result in text_results
        ]

    if conv_id:
        conv = load_conversation(username, conv_id)
    else:
        conv = None
    if conv is None:
        conv_id = str(uuid.uuid4())[:8]
        conv = {
            "id": conv_id,
            "title": (question or "图片商品识别")[:30],
            "messages": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in conv["messages"][-6:]
    ]

    def generate():
        generated_answer = None
        high_confidence = False
        if product_matches:
            top = product_matches[0]
            second_score = (
                product_matches[1]["final_score"]
                if len(product_matches) > 1
                else 0
            )
            # 移除 exact_match 捷径，完全依赖分数。
            # 只要分数 >= 0.60 且与第二名拉开差距，才认为是高置信度。
            high_confidence = (
                top.get("final_score", 0) >= 0.60
                and top.get("final_score", 0) - second_score >= 0.15
            )

        if high_confidence:
            generated_answer = _multimodal_fallback_answer(
                question, product_matches, ocr_text
            )
            for index in range(0, len(generated_answer), 20):
                chunk = generated_answer[index:index + 20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)

        if generated_answer is None and api_key and context_chunks:
            client = LLMClient(api_key)
            match_summary = "；".join(
                f"{item['name']}（{item['product_id']}，匹配分 {item['final_score']}）"
                for item in product_matches[:3]
            )

            if product_matches:
                top_score = product_matches[0].get("final_score", 0)
                # 根据分数给大模型明确的指示：分数低时允许它说“认不出来”
                if top_score < 0.7:
                    confidence_hint = "（系统匹配置信度较低。如果图片内容与候选商品明显不符，请直接告知用户无法识别，不要强行介绍。）"
                else:
                    confidence_hint = "（系统匹配置信度较高，请依据候选商品回答。）"

                prompt = (
                    "用户上传了一张商品图片。\n"
                    f"OCR 识别文字：{ocr_text or '无'}\n"
                    f"系统图片检索候选：{match_summary}\n"
                    f"用户问题：{question or '请识别图片中的商品并介绍相关信息。'}\n"
                    f"置信度提示：{confidence_hint}\n"
                    "请优先依据系统图片检索候选回答，不要编造候选列表中不存在的商品信息。"
                )
            else:
                prompt = (
                    "用户上传了一张商品图片，但系统未能从图片中识别出明确的商品匹配。\n"
                    f"OCR 识别文字：{ocr_text or '无（图片中未识别到可读文字）'}\n"
                    f"用户问题：{question or '请识别图片中的商品'}\n"
                    "请根据以上信息诚实回答：如有 OCR 文字则解读，"
                    "如无法确定商品则告知用户并建议拍清楚吊牌/型号/正面，"
                    "不要编造任何商品信息或商品编号。"
                )
            chunks = []
            try:
                for chunk in client.chat_stream(
                    prompt,
                    context_chunks,
                    history=history,
                ):
                    if chunk.startswith("__ERROR__:"):
                        break
                    chunks.append(chunk)
                    yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                if chunks:
                    generated_answer = "".join(chunks)
            except Exception:
                generated_answer = None

        if generated_answer is None:
            generated_answer = _multimodal_fallback_answer(
                question, product_matches, ocr_text
            )
            for index in range(0, len(generated_answer), 20):
                chunk = generated_answer[index:index + 20]
                yield f"data: {json.dumps({'type': 'delta', 'data': chunk}, ensure_ascii=False)}\n\n"
                time.sleep(0.03)

        conv["messages"].append({
            "role": "user",
            "content": f"[图片] {question}".strip(),
        })
        conv["messages"].append({
            "role": "assistant",
            "content": generated_answer,
        })
        conv["updated_at"] = time.time()
        save_conversation(username, conv_id, conv)
        _add_to_history(session_id, "user", f"[图片] {question}".strip())
        _add_to_history(session_id, "assistant", generated_answer)
        yield f"data: {json.dumps({'type': 'conversation_id', 'data': conv_id}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    def event_stream():
        yield f"data: {json.dumps({'type': 'multimodal', 'data': {'ocr_text': ocr_text, 'provider': analysis.get('provider')}}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'sources', 'data': sources}, ensure_ascii=False)}\n\n"
        yield from generate()

    return Response(
        stream_with_context(event_stream()),
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

    if ticket_type not in ("refund", "human_agent", "exchange", "after_sale"):
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


@app.route("/api/ticket/<ticket_id>/history", methods=["GET"])
@login_required
def api_ticket_history(ticket_id):
    """获取当前用户某张工单的完整人工会话记录。"""
    username = request.user["username"]
    db = get_db()
    try:
        ticket = db.query(Ticket).filter(
            Ticket.ticket_id == ticket_id,
            Ticket.username == username,
        ).first()
        if not ticket:
            return jsonify({"error": "工单不存在"}), 404

        messages = db.query(ChatMessage).filter(
            ChatMessage.ticket_id == ticket_id,
        ).order_by(ChatMessage.id.asc()).all()
        return jsonify({
            "ticket": ticket.to_dict(),
            "messages": [m.to_dict() for m in messages],
        })
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


# ── 用户确认退款（从退款卡片触发） ──
@app.route("/api/refund/confirm", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window=60)
def api_refund_confirm():
    username = request.user["username"]
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "").strip()

    if not order_id:
        return jsonify({"error": "请提供订单号"}), 400

    db = get_db()
    try:
        order = db.query(Order).filter(
            Order.order_id == order_id,
            Order.customer_username == username,
        ).first()
        if not order:
            return jsonify({"error": "订单不存在或不属于您的账号"}), 404

        if order.status == "refunded":
            return jsonify({"error": "该订单已退款，无需重复申请"}), 400
        if order.status == "cancelled":
            return jsonify({"error": "该订单已取消，无法退款"}), 400

        ticket = _create_ticket(
            username, "refund",
            f"退款申请 - 订单 {order_id} - {order.product_name}",
            f"用户申请退款，订单号：{order_id}，商品：{order.product_name}，"
            f"数量：{order.quantity}，金额：{order.amount}元",
            order_id=order_id,
        )
        if ticket:
            return jsonify({"success": True, "ticket": ticket})
        return jsonify({"error": "工单创建失败"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 用户轮询工单状态更新 + 坐席消息 ──
@app.route("/api/ticket/<ticket_id>/updates", methods=["GET"])
@login_required
def api_ticket_updates(ticket_id):
    username = request.user["username"]
    after_id = int(request.args.get("after_id", 0))
    db = get_db()
    try:
        ticket = db.query(Ticket).filter(
            Ticket.ticket_id == ticket_id,
            Ticket.username == username,
        ).first()
        if not ticket:
            return jsonify({"error": "工单不存在"}), 404

        messages = db.query(ChatMessage).filter(
            ChatMessage.ticket_id == ticket_id,
            ChatMessage.sender == "agent",
            ChatMessage.id > after_id,
        ).order_by(ChatMessage.id.asc()).all()

        return jsonify({
            "ticket": ticket.to_dict(),
            "messages": [m.to_dict() for m in messages],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(db)


# ── 用户发送消息给坐席（人工会话模式） ──
@app.route("/api/chat/send", methods=["POST"])
@login_required
@rate_limit(max_requests=30, window=60)
def api_chat_send():
    username = request.user["username"]
    data = request.get_json(silent=True) or {}
    ticket_id = data.get("ticket_id", "").strip()
    message = sanitize_input(data.get("message", ""), max_length=2000)

    if not ticket_id or not message:
        return jsonify({"error": "缺少工单号或消息内容"}), 400

    db = get_db()
    try:
        ticket = db.query(Ticket).filter(
            Ticket.ticket_id == ticket_id,
            Ticket.username == username,
        ).first()
        if not ticket:
            return jsonify({"error": "工单不存在"}), 404
        if ticket.status not in ("pending", "approved"):
            return jsonify({"error": "会话已结束"}), 400

        msg = ChatMessage(
            ticket_id=ticket_id,
            sender="user",
            message=message,
            created_at=int(time.time()),
        )
        db.add(msg)
        db.commit()
        return jsonify({"success": True, "message": msg.to_dict()})
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


if __name__ == "__main__":
    db_type = "SQLite (开发模式)" if IS_SQLITE else f"MySQL/PostgreSQL (生产模式)"
    storage_info = redis_manager.health_check()
    storage_type = storage_info["storage_mode"].upper()

    print("=" * 60)
    print("  电商智能客服系统 (企业级)")
    print(f"  数据库: {db_type}")
    if not IS_SQLITE:
        print(f"    连接: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'external'}")
    print(f"  会话存储: {storage_type}{'（自动降级）' if storage_type == 'MEMORY' else ''}")
    print(f"  健康检查: http://127.0.0.1:5000/health")
    print(f"  安全: 安全头/限流/输入消毒/恶意检测/文件验证")
    print(f"  引擎: {rag.get_stats()}")
    print(f"  LLM: {LLMClient.MODEL} ({'已启用' if api_key else '离线模式'})")
    print(f"  坐席工作台: http://127.0.0.1:5000/agent/login")
    print(f"  访问地址: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
