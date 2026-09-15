import json
import time
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, Column, Integer, Text, String, Float
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool

BASE_DIR = Path(__file__).parent

# ── 数据库配置：支持 SQLite / MySQL / PostgreSQL ──
# 开发模式（默认）：SQLite
# 生产模式：设置环境变量 DATABASE_URL
#   MySQL:      mysql+pymysql://user:password@host:3306/dbname?charset=utf8mb4
#   PostgreSQL: postgresql://user:password@host:5432/dbname

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    DB_PATH = BASE_DIR / "ecommerce.db"
    DATABASE_URL = f"sqlite:///{DB_PATH}"

IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=3600,
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    gender = Column(String(10), nullable=False)
    created_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "gender": self.gender,
            "created_at": self.created_at,
        }


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=False, index=True)
    gender = Column(String(10), nullable=False)
    login_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "username": self.username,
            "gender": self.gender,
            "login_at": self.login_at,
        }


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    brand = Column(String(100))
    category = Column(String(50), index=True)
    price = Column(String(20))
    description = Column(Text)
    attributes = Column(Text)
    stock = Column(String(50))
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)

    def to_text(self):
        parts = [f"商品编号：{self.product_id}"]
        parts.append(f"商品名称：{self.name}")
        if self.brand:
            parts.append(f"品牌：{self.brand}")
        if self.category:
            parts.append(f"分类：{self.category}")
        if self.price:
            parts.append(f"价格：{self.price}元")
        if self.stock:
            parts.append(f"库存：{self.stock}")
        if self.description:
            parts.append(f"商品描述：{self.description}")
        if self.attributes:
            try:
                attrs = json.loads(self.attributes)
                for k, v in attrs.items():
                    if v:
                        parts.append(f"{k}：{v}")
            except Exception:
                pass
        return "；".join(parts)

    def to_dict(self):
        return {
            "product_id": self.product_id,
            "name": self.name,
            "brand": self.brand,
            "category": self.category,
            "price": self.price,
            "description": self.description,
            "attributes": json.loads(self.attributes) if self.attributes else {},
            "stock": self.stock,
        }


class FAQ(Base):
    __tablename__ = "faq"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(50), index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)

    def to_text(self):
        return f"问：{self.question}\n答：{self.answer}"


class ShippingPolicy(Base):
    __tablename__ = "shipping_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)


class SizeChart(Base):
    __tablename__ = "size_charts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_type = Column(String(50), nullable=False, index=True)
    size = Column(String(20), nullable=False)
    bust = Column(String(20))
    waist = Column(String(20))
    hip = Column(String(20))
    shoulder = Column(String(20))
    length = Column(String(20))
    height_range = Column(String(50))
    weight_range = Column(String(50))
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)

    def to_text(self):
        parts = [f"品类：{self.product_type}", f"尺码：{self.size}"]
        if self.bust and self.bust != "--":
            parts.append(f"胸围cm：{self.bust}")
        if self.waist and self.waist != "--":
            parts.append(f"腰围cm：{self.waist}")
        if self.hip and self.hip != "--":
            parts.append(f"臀围cm：{self.hip}")
        if self.shoulder and self.shoulder != "--":
            parts.append(f"肩宽cm：{self.shoulder}")
        if self.length and self.length != "--":
            parts.append(f"衣长cm：{self.length}")
        if self.height_range and self.height_range != "--":
            parts.append(f"适合身高cm：{self.height_range}")
        if self.weight_range and self.weight_range != "--":
            parts.append(f"适合体重kg：{self.weight_range}")
        return "；".join(parts)


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(20), unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=False, index=True)
    ticket_type = Column(String(20), nullable=False)
    subject = Column(String(200), nullable=False)
    description = Column(Text)
    order_id = Column(String(50))
    status = Column(String(20), nullable=False, default="pending")
    agent_reply = Column(Text)
    handled_by = Column(String(100))
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "ticket_id": self.ticket_id,
            "username": self.username,
            "ticket_type": self.ticket_type,
            "subject": self.subject,
            "description": self.description,
            "order_id": self.order_id,
            "status": self.status,
            "agent_reply": self.agent_reply,
            "handled_by": self.handled_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default="offline")
    created_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
        }


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(30), unique=True, nullable=False, index=True)
    customer_username = Column(String(100), nullable=False, index=True)
    product_id = Column(String(20), nullable=False)
    product_name = Column(String(200), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    amount = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    shipping_address = Column(Text)
    tracking_number = Column(String(50))
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "customer_username": self.customer_username,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "quantity": self.quantity,
            "amount": self.amount,
            "status": self.status,
            "shipping_address": self.shipping_address,
            "tracking_number": self.tracking_number,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_text(self):
        parts = [
            f"订单号：{self.order_id}",
            f"客户：{self.customer_username}",
            f"商品：{self.product_name}",
            f"数量：{self.quantity}",
            f"金额：{self.amount}元",
            f"订单状态：{self.status}",
        ]
        if self.tracking_number:
            parts.append(f"快递单号：{self.tracking_number}")
        if self.shipping_address:
            parts.append(f"收货地址：{self.shipping_address}")
        return "；".join(parts)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(20), nullable=False, index=True)
    sender = Column(String(20), nullable=False)  # 'user' or 'agent'
    message = Column(Text, nullable=False)
    created_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "sender": self.sender,
            "message": self.message,
            "created_at": self.created_at,
        }


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_type = Column(String(20), nullable=False)
    actor_name = Column(String(100), nullable=False)
    action = Column(String(50), nullable=False)
    target_type = Column(String(30))
    target_id = Column(String(50))
    detail = Column(Text)
    created_at = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "actor_type": self.actor_type,
            "actor_name": self.actor_name,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
            "created_at": self.created_at,
        }


class LearnedQA(Base):
    __tablename__ = "learned_qa"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    source = Column(String(20))
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(Integer, nullable=False)
    last_hit_at = Column(Integer)

    def to_dict(self):
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "source": self.source,
            "hit_count": self.hit_count,
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
        }


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise


def close_db(db):
    try:
        db.close()
    except Exception:
        pass
