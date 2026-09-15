# -*- coding: utf-8 -*-
"""业务数据工具层（Function Calling）

用途
----
把「查订单 / 查物流 / 查售后工单进度 / 查实时库存价格」封装成 LLM 可调用的
工具（tool）。由大模型决策要查什么、抽取参数，本模块负责执行查询并返回
结构化数据；客服 AI 再基于这些实时数据组织回答。
这类查询不再走 RAG 知识库 —— 避免知识库里的陈旧数据（尤其是库存）误导用户。

接入真实业务系统时的适配点
--------------------------
本模块是整个客服系统与「业务数据」之间唯一的收口处。接入真实业务库时，
保持 TOOL_SCHEMAS / TOOL_REGISTRY 的结构不变，只需改写各执行函数
（_exec_orders / _exec_logistics / _exec_after_sales / _exec_product）里的查询来源：
  * 改连真实业务库（可复用 database.py 的 SQLAlchemy 连接，指向业务库），或
  * 换成调用业务系统内部 HTTP / RPC API；
  * 把表字段映射成与真实业务表一致。
其余部分（工具描述、LLM 决策、账号绑定、错误兜底、前端）无需改动。

安全约定
--------
* 用户身份由服务端注入：execute_tool(name, username, args) 的 username 永远来自
  登录态，LLM 无法通过参数伪造身份。
* 所有订单 / 售后查询强制绑定当前用户
  （Order.customer_username == username / Ticket.username == username）。
* 所有 SQL 均走 SQLAlchemy 参数绑定，不拼接用户输入。
"""

import json
import re

from sqlalchemy import or_

from database import get_db, close_db, Order, Product, Ticket

# ── 状态中文化 ──────────────────────────────────────────

ORDER_STATUS_ZH = {
    "pending": "待付款",
    "paid": "已付款",
    "shipped": "已发货",
    "delivered": "已签收",
    "refunded": "已退款",
    "cancelled": "已取消",
}

TICKET_TYPE_ZH = {
    "refund": "退款/售后",
    "human_agent": "人工客服",
    "exchange": "换货",
    "after_sale": "售后",
}

TICKET_STATUS_ZH = {
    "pending": "待处理（客服审核中）",
    "approved": "已通过（退款/处理中）",
    "rejected": "已驳回",
    "resolved": "已办结",
    "closed": "已关闭",
}


def _fmt_amount(amount):
    """金额转展示文本。"""
    try:
        v = float(amount)
        if v == int(v):
            return f"{int(v)}元"
        return f"{v:g}元"
    except (TypeError, ValueError):
        return f"{amount}元"


def _fmt_price(price):
    """价格转展示文本（price 字段是 String，可能是 '59.9' 或 '59.9元'）。"""
    p = str(price or "").strip()
    if not p:
        return "未标注"
    if "元" in p:
        return p
    return f"{p}元"


def _stock_text(stock):
    """库存转可读文本（stock 字段是 String，可能是 '100'、'有货'、'--'）。"""
    s = str(stock or "").strip()
    if not s or s in ("--", "-", "未知"):
        return "未知"
    m = re.search(r"\d+", s)
    if m:
        n = int(m.group(0))
        return f"有货（约{n}件）" if n > 0 else "暂时无货"
    if any(w in s for w in ("有货", "现货", "充足", "在售")):
        return "有货"
    if any(w in s for w in ("无货", "缺货", "断货")):
        return "暂时无货"
    return s


# ── 工具执行器 ──────────────────────────────────────────
# 每个执行器返回统一结构：
#   {"ok": True/False, "records": [...], "text": "给用户/模型看的友好文本"}

def _exec_orders(username, args):
    """查询当前用户的订单（全部，或按订单号精确查询）。"""
    order_id = str(args.get("order_id") or "").strip().upper() or None
    db = get_db()
    try:
        if order_id:
            rows = db.query(Order).filter(
                Order.order_id == order_id,
                Order.customer_username == username,
            ).all()
        else:
            rows = db.query(Order).filter(
                Order.customer_username == username,
            ).order_by(Order.created_at.desc()).all()
    except Exception as e:
        return {"ok": False, "records": [], "text": f"订单系统查询失败：{e}"}
    finally:
        close_db(db)

    if order_id and not rows:
        # 订单号存在但不属于当前用户 / 不存在 —— 尝试区分，给用户准确提示
        db = get_db()
        try:
            other = db.query(Order).filter(Order.order_id == order_id).first()
        except Exception:
            other = None
        finally:
            close_db(db)
        if other:
            return {"ok": False, "records": [], "text":
                    f"订单号 {order_id} 存在，但不属于当前登录账号（{username}）。"
                    "请使用该用户的账号登录后查询，或联系人工客服协助处理。"}
        return {"ok": False, "records": [], "text":
                f"未找到订单号为 {order_id} 的订单，请确认订单号是否正确。"}

    if not rows:
        return {"ok": False, "records": [],
                "text": "您名下暂时没有订单记录。如果您有具体订单号，请提供以便精确查询。"}

    records = []
    lines = [f"您共有 {len(rows)} 个订单："]
    for o in rows:
        st = ORDER_STATUS_ZH.get(o.status, o.status)
        rec = {
            "order_id": o.order_id,
            "product_name": o.product_name,
            "quantity": o.quantity,
            "amount": o.amount,
            "status": o.status,
            "status_text": st,
            "tracking_number": o.tracking_number or "",
            "created_at": o.created_at,
        }
        records.append(rec)
        lines.append(f"• {o.order_id} | {o.product_name} ×{o.quantity} | "
                     f"{_fmt_amount(o.amount)} | {st}")
    return {"ok": True, "records": records, "text": "\n".join(lines)}


def _exec_logistics(username, args):
    """查询用户订单的发货 / 物流信息（快递单号）。"""
    order_id = str(args.get("order_id") or "").strip().upper() or None
    db = get_db()
    try:
        query = db.query(Order).filter(Order.customer_username == username)
        if order_id:
            query = query.filter(Order.order_id == order_id)
        rows = query.order_by(Order.created_at.desc()).all()
    except Exception as e:
        return {"ok": False, "records": [], "text": f"物流信息查询失败：{e}"}
    finally:
        close_db(db)

    if order_id and not rows:
        return {"ok": False, "records": [], "text":
                f"未找到订单号 {order_id} 的发货记录，请确认订单号是否正确。"}
    if not rows:
        return {"ok": False, "records": [], "text":
                "您名下暂时没有可查询物流的订单。"}

    records = []
    lines = []
    for o in rows:
        st = ORDER_STATUS_ZH.get(o.status, o.status)
        rec = {
            "order_id": o.order_id,
            "product_name": o.product_name,
            "status": o.status,
            "status_text": st,
            "tracking_number": o.tracking_number or "",
        }
        records.append(rec)
        if o.tracking_number:
            lines.append(f"• 订单 {o.order_id}（{o.product_name}）：{st}，"
                         f"快递单号 {o.tracking_number}")
        else:
            lines.append(f"• 订单 {o.order_id}（{o.product_name}）：{st}"
                         "（暂无快递单号）")
    if not lines:
        return {"ok": False, "records": [], "text":
                "您的订单均未发货或暂无快递单号。"}
    return {"ok": True, "records": records, "text": "\n".join(lines)}


def _exec_after_sales(username, args):
    """查询当前用户的售后 / 退款工单处理进度。"""
    ttype = str(args.get("ticket_type") or "").strip().lower() or None
    if ttype and ttype not in TICKET_TYPE_ZH:
        ttype = None  # 未知类型不过滤，避免漏查
    db = get_db()
    try:
        query = db.query(Ticket).filter(Ticket.username == username)
        if ttype:
            query = query.filter(Ticket.ticket_type == ttype)
        rows = query.order_by(Ticket.created_at.desc()).limit(10).all()
    except Exception as e:
        return {"ok": False, "records": [], "text": f"售后工单查询失败：{e}"}
    finally:
        close_db(db)

    if not rows:
        return {"ok": False, "records": [], "text":
                "您名下暂时没有售后/退款工单记录。如需退款，请告知我对应的订单号，"
                "我可以协助您发起申请。"}

    records = []
    lines = [f"您有 {len(rows)} 条售后/服务工单："]
    for t in rows:
        st = TICKET_STATUS_ZH.get(t.status, t.status)
        ty = TICKET_TYPE_ZH.get(t.ticket_type, t.ticket_type)
        rec = {
            "ticket_id": t.ticket_id,
            "ticket_type": t.ticket_type,
            "ticket_type_zh": ty,
            "order_id": t.order_id or "",
            "subject": t.subject,
            "status": t.status,
            "status_text": st,
            "agent_reply": t.agent_reply or "",
            "created_at": t.created_at,
        }
        records.append(rec)
        line = f"• 工单 {t.ticket_id}（{ty}）｜{t.subject}｜{st}"
        if t.agent_reply:
            line += f"｜客服回复：{t.agent_reply}"
        lines.append(line)
    return {"ok": True, "records": records, "text": "\n".join(lines)}


def _exec_product(username, args):
    """实时查询商品目录的库存 / 价格（业务商品系统，公开数据，无需绑定用户）。"""
    product_id = str(args.get("product_id") or "").strip().upper() or None
    keyword = str(args.get("keyword") or "").strip() or None
    if not product_id and not keyword:
        return {"ok": False, "records": [], "text":
                "缺少查询条件：请提供商品编号（如 P001）或商品名称关键词。"}

    db = get_db()
    try:
        if product_id:
            rows = db.query(Product).filter(
                Product.product_id == product_id
            ).limit(5).all()
            if not rows:
                # 支持模糊匹配编号片段
                rows = db.query(Product).filter(
                    Product.product_id.contains(product_id)
                ).limit(5).all()
        else:
            kw = f"%{keyword}%"
            rows = db.query(Product).filter(or_(
                Product.name.like(kw),
                Product.brand.like(kw),
                Product.category.like(kw),
                Product.product_id.like(kw),
            )).limit(5).all()
    except Exception as e:
        return {"ok": False, "records": [], "text": f"商品查询失败：{e}"}
    finally:
        close_db(db)

    if not rows:
        hint = f"商品编号 {product_id}" if product_id else f"关键词「{keyword}」"
        return {"ok": False, "records": [], "text":
                f"在商品系统中未找到{hint}对应的商品。请核对编号/名称，"
                "或询问知识库中的商品介绍。"}

    records = []
    lines = []
    for p in rows:
        rec = {
            "product_id": p.product_id,
            "name": p.name,
            "category": p.category or "",
            "price": _fmt_price(p.price),
            "stock_text": _stock_text(p.stock),
            "stock_raw": p.stock or "",
        }
        records.append(rec)
        lines.append(f"• {p.name}（{p.product_id}）｜价格 {rec['price']}｜"
                     f"库存 {rec['stock_text']}" +
                     (f"｜分类：{p.category}" if p.category else ""))
    return {"ok": True, "records": records, "text": "\n".join(lines)}


# ── 工具注册表：schema（喂给 LLM）+ 执行器 ───────────────

def _schema(name, description, properties, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = [
    _schema(
        "query_orders",
        "查询当前登录用户在电商平台的订单：支持返回订单列表，或按订单号精确查询状态。"
        "用于回答「我的订单」「我买了什么」「订单发货了吗」「查一下订单号 XX 的状态」等。"
        "订单号形如 DD20260828001。不要填写用户名参数——系统已自动绑定当前登录用户。",
        {
            "order_id": {
                "type": "string",
                "description": "可选的订单号（形如 DD20260828001）。不提供则返回用户最近的全部订单。",
            },
        },
    ),
    _schema(
        "query_logistics",
        "查询当前用户订单的发货与物流信息（是否已发货、快递单号），"
        "用于回答「我的快递到哪了」「发货了吗」「物流状态怎么样」。"
        "订单号可省略，省略则返回用户最近的订单物流。不要填写用户名参数。",
        {
            "order_id": {
                "type": "string",
                "description": "可选的订单号（形如 DD20260828001）。不提供则返回最近订单的物流。",
            },
        },
    ),
    _schema(
        "query_after_sales",
        "查询当前用户提交的售后/退款/服务工单的处理进度，"
        "用于回答「退款到哪一步了」「售后处理得怎么样了」「我的工单处理进度」。"
        "不要填写用户名参数。",
        {
            "ticket_type": {
                "type": "string",
                "enum": ["refund", "human_agent"],
                "description": "可选的工单类型过滤：refund=退款/售后，human_agent=人工客服。不传则查全部。",
            },
        },
    ),
    _schema(
        "query_product",
        "实时查询业务商品系统中的商品库存与价格（返回商品编号、名称、价格、库存），"
        "用于回答「某商品还有货吗」「库存多少」「现在什么价格」等需要实时数据的问题。"
        "只用于实时库存/价格；商品介绍、尺码等知识型问题不要调用本工具。",
        {
            "product_id": {
                "type": "string",
                "description": "商品编号（形如 P001）。",
            },
            "keyword": {
                "type": "string",
                "description": "商品名称/品牌/分类关键词，用于模糊查找。",
            },
        },
    ),
]

# 工具名 → 前端来源标签
TOOL_SOURCES = {
    "query_orders": "订单系统",
    "query_logistics": "订单系统",
    "query_after_sales": "售后工单",
    "query_product": "商品系统",
}

TOOL_REGISTRY = {
    "query_orders": {"handler": _exec_orders, "schema": TOOL_SCHEMAS[0]},
    "query_logistics": {"handler": _exec_logistics, "schema": TOOL_SCHEMAS[1]},
    "query_after_sales": {"handler": _exec_after_sales, "schema": TOOL_SCHEMAS[2]},
    "query_product": {"handler": _exec_product, "schema": TOOL_SCHEMAS[3]},
}


def execute_tool(name, username, args=None):
    """执行工具（账号由服务端注入，args 来自 LLM 已校验）。"""
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        return {"ok": False, "records": [], "text": f"未知工具：{name}"}
    if not isinstance(args, dict):
        args = {}
    try:
        return entry["handler"](username, args)
    except Exception as e:  # 兜底：任何异常都以可读文本返回，不让链路中断
        return {"ok": False, "records": [], "text": f"工具 {name} 执行异常：{e}"}


# ── 方便调试/独立运行验证 ────────────────────────────────

if __name__ == "__main__":
    import sys
    _u = sys.argv[1] if len(sys.argv) > 1 else "testuser"
    print("工具列表：", ", ".join(TOOL_REGISTRY))
    print(json.dumps(execute_tool("query_orders", _u, {}), ensure_ascii=False, indent=2))
