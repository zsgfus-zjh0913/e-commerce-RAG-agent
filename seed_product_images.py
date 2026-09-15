#!/usr/bin/env python3
"""为现有商品目录生成占位产品图（用于多模态检索测试）。

只在 data/product_images/ 下生成缩略占位图，
不会覆盖用户上传的真实商品照片。
运行: python seed_product_images.py
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from database import Product, close_db, get_db, init_db

IMAGE_DIR = Path("data") / "product_images"
INDEX_FILE = IMAGE_DIR / "index.json"


def make_placeholder(product_id, name, size=(400, 400)):
    """生成带商品编号和名称首字的彩色占位图。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[seed] Pillow 未安装，跳过图片生成: pip install Pillow")
        return None

    img = Image.new("RGB", size, color=(240, 240, 245))
    draw = ImageDraw.Draw(img)

    # 背景色块（按 product_id 哈希选色）
    hues = [
        (220, 230, 255), (255, 230, 230), (230, 255, 230),
        (255, 245, 220), (240, 230, 255), (255, 240, 245),
        (230, 245, 255), (255, 250, 230), (245, 235, 255),
    ]
    bg = hues[hash(product_id) % len(hues)]
    draw.rectangle([40, 40, 360, 360], fill=bg, outline=(200, 200, 210), width=2)

    # 文字
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
        font_small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    except Exception:
        font = ImageFont.load_default()
        font_small = font

    # 商品编号
    draw.text((120, 160), product_id, fill=(80, 80, 100), font=font)
    # 名称取前两个字
    short = name[:2] if name else "??"
    bbox = draw.textbbox((0, 0), short, font=font)
    w = bbox[2] - bbox[0]
    draw.text((200 - w // 2, 210), short, fill=(50, 50, 80), font=font)

    return img


def main():
    init_db()
    db = get_db()
    try:
        products = db.query(Product).order_by(Product.id.asc()).all()
    finally:
        close_db(db)

    if not products:
        print("[seed] 数据库中没有商品记录，请先运行 migrate.py 导入数据。")
        return

    print(f"[seed] 共找到 {len(products)} 个商品")

    # 加载现有 manifest
    manifest = {}
    if INDEX_FILE.exists():
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            manifest = data.get("products", data)
            if isinstance(manifest, dict) and "version" in manifest:
                manifest = manifest.get("products", {})
        except Exception:
            manifest = {}

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    generated = 0
    for p in products:
        pid = p.product_id
        # 跳过已有图片的商品
        if pid in manifest and manifest[pid]:
            existing = manifest[pid]
            paths = existing if isinstance(existing, list) else [existing]
            if any((IMAGE_DIR / path).exists() for path in paths):
                print(f"  [{pid}] 已有图片，跳过")
                continue

        # 生成正面占位图
        img = make_placeholder(pid, p.name)
        if img is None:
            break  # Pillow 不可用

        rel_path = f"{pid}/front.png"
        abs_path = IMAGE_DIR / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(abs_path), "PNG")
        manifest[pid] = [rel_path]
        generated += 1
        print(f"  [{pid}] {p.name} → {rel_path}")

    if generated:
        INDEX_FILE.write_text(
            json.dumps({"version": 1, "products": manifest},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[seed] 完成！生成 {generated} 张占位图，已更新 index.json")
    else:
        print("[seed] 无需生成（所有商品已有图片或 Pillow 不可用）。")


if __name__ == "__main__":
    main()
