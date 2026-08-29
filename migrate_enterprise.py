"""
企业级数据迁移脚本：
1. 创建人工客服坐席账号
2. 创建示例订单数据
"""

import time
import bcrypt
from database import init_db, get_db, close_db, Agent, Order, Product, AuditLog

AGENTS = [
    {"username": "agent001", "password": "agent123", "name": "客服小王"},
    {"username": "agent002", "password": "agent456", "name": "客服小李"},
    {"username": "admin", "password": "admin123", "name": "管理员"},
]

SAMPLE_ORDERS = [
    {
        "order_id": "DD20260828001",
        "customer_username": "testuser",
        "product_id": "P001",
        "product_name": "纯棉简约圆领T恤",
        "quantity": 2,
        "amount": 99.80,
        "status": "shipped",
        "shipping_address": "北京市朝阳区xx路xx号",
        "tracking_number": "SF1234567890",
    },
    {
        "order_id": "DD20260828002",
        "customer_username": "testuser",
        "product_id": "P003",
        "product_name": "智能体脂秤",
        "quantity": 1,
        "amount": 299.00,
        "status": "delivered",
        "shipping_address": "北京市朝阳区xx路xx号",
        "tracking_number": "YT0987654321",
    },
    {
        "order_id": "DD20260828003",
        "customer_username": "demo",
        "product_id": "P002",
        "product_name": "无线蓝牙降噪耳机",
        "quantity": 1,
        "amount": 599.00,
        "status": "pending",
        "shipping_address": "上海市浦东新区xx路xx号",
        "tracking_number": "",
    },
    {
        "order_id": "DD20260827004",
        "customer_username": "testuser",
        "product_id": "P005",
        "product_name": "经典直筒牛仔裤",
        "quantity": 1,
        "amount": 159.00,
        "status": "paid",
        "shipping_address": "北京市朝阳区xx路xx号",
        "tracking_number": "",
    },
    {
        "order_id": "DD20260826005",
        "customer_username": "demo",
        "product_id": "P006",
        "product_name": "多功能升降电脑书桌",
        "quantity": 1,
        "amount": 1299.00,
        "status": "shipped",
        "shipping_address": "上海市浦东新区xx路xx号",
        "tracking_number": "JD5566778899",
    },
]


def run_migration():
    init_db()
    db = get_db()
    now = int(time.time())

    # 坐席账号
    existing_agents = db.query(Agent).count()
    if existing_agents == 0:
        for a in AGENTS:
            hashed = bcrypt.hashpw(a["password"].encode(), bcrypt.gensalt(rounds=12)).decode()
            agent = Agent(
                username=a["username"],
                password_hash=hashed,
                name=a["name"],
                status="offline",
                created_at=now,
            )
            db.add(agent)
        db.commit()
        print(f"[迁移] 创建 {len(AGENTS)} 个坐席账号")
    else:
        print(f"[迁移] 坐席账号已存在（{existing_agents}个），跳过")

    # 订单数据
    existing_orders = db.query(Order).count()
    if existing_orders == 0:
        for o in SAMPLE_ORDERS:
            order = Order(
                order_id=o["order_id"],
                customer_username=o["customer_username"],
                product_id=o["product_id"],
                product_name=o["product_name"],
                quantity=o["quantity"],
                amount=o["amount"],
                status=o["status"],
                shipping_address=o["shipping_address"],
                tracking_number=o["tracking_number"],
                created_at=now - 86400,
                updated_at=now,
            )
            db.add(order)
        db.commit()
        print(f"[迁移] 创建 {len(SAMPLE_ORDERS)} 个示例订单")
    else:
        print(f"[迁移] 订单数据已存在（{existing_orders}个），跳过")

    # 审计日志
    log = AuditLog(
        actor_type="system",
        actor_name="migration",
        action="enterprise_migration",
        target_type="database",
        target_id="all",
        detail="企业级数据迁移：坐席账号 + 订单数据",
        created_at=now,
    )
    db.add(log)
    db.commit()
    print("[迁移] 审计日志已记录")

    close_db(db)
    print("[迁移] 完成！")
    print()
    print("坐席账号：")
    for a in AGENTS:
        print(f"  用户名: {a['username']:<12} 密码: {a['password']:<12} 姓名: {a['name']}")


if __name__ == "__main__":
    run_migration()
