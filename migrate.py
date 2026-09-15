import json
import csv
import re
import time
from pathlib import Path

from database import init_db, get_db, close_db, User, Product, FAQ, ShippingPolicy, SizeChart

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def migrate_users():
    """迁移用户数据。旧密码为 SHA256 哈希，无法恢复，统一设置为默认密码 123456。"""
    users_file = BASE_DIR / "users.json"
    if not users_file.exists():
        print("[migrate] 无 users.json，跳过用户迁移")
        return 0

    users = json.loads(users_file.read_text(encoding="utf-8"))
    db = get_db()
    count = 0
    try:
        for username, data in users.items():
            existing = db.query(User).filter(User.username == username).first()
            if existing:
                # 已存在用户，重置密码为默认密码
                existing.password_hash = hash_password("123456")
                count += 1
                continue
            user = User(
                username=username,
                password_hash=hash_password("123456"),
                gender=data.get("gender", "male"),
                created_at=int(data.get("created_at", time.time())),
            )
            db.add(user)
            count += 1
        db.commit()
        print(f"[migrate] 用户迁移完成: {count} 条（旧用户密码已重置为 123456）")
    except Exception as e:
        db.rollback()
        print(f"[migrate] 用户迁移失败: {e}")
    finally:
        close_db(db)
    return count


def migrate_products():
    products_file = DATA_DIR / "商品信息.md"
    if not products_file.exists():
        print("[migrate] 无 商品信息.md，跳过商品迁移")
        return 0

    text = products_file.read_text(encoding="utf-8")
    products = []
    for block in re.split(r"(?=^### )", text, flags=re.MULTILINE):
        product = {}
        for line in block.splitlines():
            match = re.match(r"^- ([^：]+)：(.*)$", line.strip())
            if match:
                product[match.group(1).strip()] = match.group(2).strip()
        if product.get("商品编号"):
            products.append(product)

    db = get_db()
    added = 0
    updated = 0
    try:
        for p in products:
            product_id = p.get("商品编号", "")
            if not product_id:
                continue
            exclude_keys = {"商品编号", "商品名称", "品牌", "分类", "价格", "商品描述", "库存"}
            attributes = {k: v for k, v in p.items() if k not in exclude_keys and v}
            existing = db.query(Product).filter(Product.product_id == product_id).first()
            if existing:
                existing.name = p.get("商品名称", "")
                existing.brand = p.get("品牌", "")
                existing.category = p.get("分类", "")
                existing.price = p.get("价格", "")
                existing.description = p.get("商品描述", "")
                existing.stock = p.get("库存", "")
                existing.attributes = json.dumps(attributes, ensure_ascii=False)
                existing.updated_at = int(time.time())
                existing.version = (existing.version or 1) + 1
                updated += 1
                continue

            product = Product(
                product_id=product_id,
                name=p.get("商品名称", ""),
                brand=p.get("品牌", ""),
                category=p.get("分类", ""),
                price=p.get("价格", ""),
                description=p.get("商品描述", ""),
                stock=p.get("库存", ""),
                attributes=json.dumps(attributes, ensure_ascii=False),
                created_at=int(time.time()),
                updated_at=int(time.time()),
            )
            db.add(product)
            added += 1
        db.commit()
        print(f"[migrate] 商品同步完成: 新增 {added} 条，更新 {updated} 条")
    except Exception as e:
        db.rollback()
        print(f"[migrate] 商品迁移失败: {e}")
    finally:
        close_db(db)
    return added + updated


def migrate_faq():
    faq_file = DATA_DIR / "faq.txt"
    if not faq_file.exists():
        print("[migrate] 无 faq.txt，跳过 FAQ 迁移")
        return 0

    text = faq_file.read_text(encoding="utf-8")
    blocks = re.split(r'\n(?=[一二三四五六七八九十]+、)', text)

    db = get_db()
    count = 0
    try:
        current_category = "通用"
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            cat_match = re.match(r'^([一二三四五六七八九十]+、\S+)', block)
            if cat_match:
                current_category = re.sub(r'^[一二三四五六七八九十]+、', '', cat_match.group(1))

            pairs = re.findall(r'问：(.*?)\n答：(.*?)(?=\n问：|\n\n[一二三四五六七八九十]+、|\Z)', block, re.DOTALL)
            for q, a in pairs:
                q = q.strip()
                a = a.strip()
                if not q or not a:
                    continue
                existing = db.query(FAQ).filter(FAQ.question == q).first()
                if existing:
                    continue
                faq = FAQ(
                    category=current_category,
                    question=q,
                    answer=a,
                    created_at=int(time.time()),
                )
                db.add(faq)
                count += 1
        db.commit()
        print(f"[migrate] FAQ 迁移完成: {count} 条")
    except Exception as e:
        db.rollback()
        print(f"[migrate] FAQ 迁移失败: {e}")
    finally:
        close_db(db)
    return count


def migrate_shipping_policy():
    shipping_file = DATA_DIR / "shipping_policy.md"
    if not shipping_file.exists():
        print("[migrate] 无 shipping_policy.md，跳过物流政策迁移")
        return 0

    content = shipping_file.read_text(encoding="utf-8")
    sections = re.split(r'\n(?=##\s)', content)

    db = get_db()
    count = 0
    try:
        for section in sections:
            section = section.strip()
            if not section:
                continue
            title_match = re.match(r'^#*#?\s*(.+)', section.split('\n')[0])
            title = title_match.group(1).strip() if title_match else "物流政策"
            existing = db.query(ShippingPolicy).filter(ShippingPolicy.title == title).first()
            if existing:
                continue
            sp = ShippingPolicy(
                title=title,
                content=section,
                created_at=int(time.time()),
            )
            db.add(sp)
            count += 1
        db.commit()
        print(f"[migrate] 物流政策迁移完成: {count} 条")
    except Exception as e:
        db.rollback()
        print(f"[migrate] 物流政策迁移失败: {e}")
    finally:
        close_db(db)
    return count


def migrate_size_chart():
    size_file = DATA_DIR / "size_chart.csv"
    if not size_file.exists():
        print("[migrate] 无 size_chart.csv，跳过尺码表迁移")
        return 0

    db = get_db()
    count = 0
    try:
        with open(size_file, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pt = row.get("品类", "")
                sz = row.get("尺码", "")
                existing = db.query(SizeChart).filter(
                    SizeChart.product_type == pt,
                    SizeChart.size == sz,
                ).first()
                if existing:
                    continue
                sc = SizeChart(
                    product_type=pt,
                    size=sz,
                    bust=row.get("胸围cm", ""),
                    waist=row.get("腰围cm", ""),
                    hip=row.get("臀围cm", ""),
                    shoulder=row.get("肩宽cm", ""),
                    length=row.get("衣长cm", ""),
                    height_range=row.get("适合身高cm", ""),
                    weight_range=row.get("适合体重kg", ""),
                    created_at=int(time.time()),
                )
                db.add(sc)
                count += 1
        db.commit()
        print(f"[migrate] 尺码表迁移完成: {count} 条")
    except Exception as e:
        db.rollback()
        print(f"[migrate] 尺码表迁移失败: {e}")
    finally:
        close_db(db)
    return count


def main():
    print("=" * 50)
    print("  数据迁移工具：JSON/文本文件 → SQLite 数据库")
    print("=" * 50)

    init_db()
    print("\n[1/5] 迁移用户数据...")
    migrate_users()
    print("\n[2/5] 迁移商品数据...")
    migrate_products()
    print("\n[3/5] 迁移 FAQ 数据...")
    migrate_faq()
    print("\n[4/5] 迁移物流政策...")
    migrate_shipping_policy()
    print("\n[5/5] 迁移尺码表...")
    migrate_size_chart()

    print("\n" + "=" * 50)
    print("  迁移完成！数据库文件: ecommerce.db")
    print("  请删除 index/ 目录以触发 RAG 重建")
    print("=" * 50)


if __name__ == "__main__":
    main()
