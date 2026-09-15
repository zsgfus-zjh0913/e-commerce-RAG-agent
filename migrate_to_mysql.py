"""
SQLite → MySQL/PostgreSQL 数据迁移脚本

使用方法：
1. 安装 MySQL 驱动：pip install PyMySQL
2. 创建数据库：CREATE DATABASE ecommerce CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
3. 设置环境变量：
   set DATABASE_URL=mysql+pymysql://root:password@localhost:3306/ecommerce?charset=utf8mb4
4. 运行：python migrate_to_mysql.py
"""

import os
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

# SQLite 源数据库
from pathlib import Path
BASE_DIR = Path(__file__).parent
SQLITE_PATH = BASE_DIR / "ecommerce.db"

# 目标数据库
TARGET_URL = os.environ.get("DATABASE_URL", "")

if not TARGET_URL or TARGET_URL.startswith("sqlite"):
    print("[错误] 请先设置 DATABASE_URL 环境变量指向 MySQL/PostgreSQL")
    print("  MySQL 示例:")
    print("  set DATABASE_URL=mysql+pymysql://root:password@localhost:3306/ecommerce?charset=utf8mb4")
    print("  PostgreSQL 示例:")
    print("  set DATABASE_URL=postgresql://user:password@localhost:5432/ecommerce")
    sys.exit(1)

if not SQLITE_PATH.exists():
    print(f"[错误] SQLite 数据库不存在: {SQLITE_PATH}")
    print("请先运行 python migrate.py 和 python migrate_enterprise.py 创建本地数据")
    sys.exit(1)

from database import Base, User, Session, Product, FAQ, ShippingPolicy, SizeChart, Ticket, Agent, Order, AuditLog, LearnedQA


def run_migration():
    print("=" * 60)
    print("  SQLite → MySQL/PostgreSQL 数据迁移")
    print("=" * 60)
    print(f"  源数据库: {SQLITE_PATH}")
    print(f"  目标数据库: {TARGET_URL.split('@')[-1] if '@' in TARGET_URL else TARGET_URL}")
    print()

    # 连接源数据库
    print("[1/5] 连接源数据库 (SQLite)...")
    sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
    sqlite_session = sessionmaker(bind=sqlite_engine)()
    print("  ✓ 源数据库连接成功")

    # 连接目标数据库
    print("[2/5] 连接目标数据库...")
    target_engine = create_engine(TARGET_URL, echo=False)
    target_session = sessionmaker(bind=target_engine)()
    print("  ✓ 目标数据库连接成功")

    # 创建表结构
    print("[3/5] 创建表结构...")
    Base.metadata.create_all(target_engine)
    print("  ✓ 表结构创建成功")

    # 迁移数据
    models = [
        ("User", User),
        ("Session", Session),
        ("Product", Product),
        ("FAQ", FAQ),
        ("ShippingPolicy", ShippingPolicy),
        ("SizeChart", SizeChart),
        ("Ticket", Ticket),
        ("Agent", Agent),
        ("Order", Order),
        ("AuditLog", AuditLog),
        ("LearnedQA", LearnedQA),
    ]

    print("[4/5] 迁移数据...")
    total_count = 0
    for name, model in models:
        try:
            records = sqlite_session.query(model).all()
            if not records:
                print(f"  - {name}: 0 条 (跳过)")
                continue

            for record in records:
                target_session.add(record)
            target_session.commit()
            print(f"  ✓ {name}: {len(records)} 条")
            total_count += len(records)
        except Exception as e:
            print(f"  ✗ {name}: 失败 - {e}")
            target_session.rollback()

    print(f"  总计迁移: {total_count} 条记录")

    # 验证
    print("[5/5] 验证数据...")
    for name, model in models:
        count = target_session.query(model).count()
        print(f"  - {name}: {count} 条")

    sqlite_session.close()
    target_session.close()
    print()
    print("=" * 60)
    print("  迁移完成！")
    print(f"  现在可以通过设置 DATABASE_URL 启动应用:")
    print(f"  DATABASE_URL={TARGET_URL} python app.py")
    print("=" * 60)


if __name__ == "__main__":
    run_migration()
