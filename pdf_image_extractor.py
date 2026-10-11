"""PDF 图片抽取器：按页遍历 PDF，提取所有嵌入图片并分类。

分类用途 + 分类规则：
  - text_image   含文字图片（规格表、说明截图）：面积占比足够 且 OCR 抽出实际文本
  - visual_image 纯视觉图片（外观图、示意图）：面积占比足够 但 OCR 基本无文本
  - decorative   装饰图片（logo、图标、分隔线、背景）：面积过小 / 宽高比极端 / 无文本

使用 PyMuPDF (fitz) 抽取，OCR 引擎自动选择（PaddleOCR -> RapidOCR）。
输出 JS 可读的 manifest JSON + 将图片保存到 <out>/<pdf 文件名>/pg_<page>_<i>.<ext>。
"""

import io
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# ── OCR 引擎懒加载 ────────────────────────────────────

_ocr = None
_ocr_name = None


def _load_ocr():
    global _ocr, _ocr_name
    if _ocr_name is not None:
        return _ocr

    requested = os.environ.get("MULTIMODAL_OCR_ENGINE", "auto").lower()
    if requested in ("auto", "paddle", "paddleocr"):
        try:
            from paddleocr import PaddleOCR

            try:
                engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
            except TypeError:
                engine = PaddleOCR(use_angle_cls=True, lang="ch")
            _ocr, _ocr_name = engine, "paddleocr"
            return _ocr
        except Exception:
            pass

    if requested in ("auto", "rapid", "rapidocr"):
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ocr, _ocr_name = RapidOCR(), "rapidocr"
            return _ocr
        except Exception:
            pass

    _ocr_name = "none"
    return None


def _ocr_text_of_pil(image):
    """返回图片中的有效文本字符串（用于分类），OCR 不可用时返回空字符串。"""
    engine = _load_ocr()
    if engine is None:
        return ""

    import numpy as np

    array = np.asarray(image.convert("RGB"))
    try:
        if _ocr_name == "paddleocr":
            raw = engine.ocr(array, cls=True)
            parts = []
            for page in raw or []:
                for line in page or []:
                    try:
                        box, value = line
                        text, score = value
                        if text and float(score) >= 0.5:
                            parts.append(str(text).strip())
                    except (TypeError, ValueError):
                        continue
            return " ".join(parts)
        result, _ = engine(array)
        parts = []
        for line in result or []:
            try:
                box, text, score = line
                if text and float(score) >= 0.5:
                    parts.append(str(text).strip())
            except (TypeError, ValueError):
                continue
        return " ".join(parts)
    except Exception:
        return ""


# ── 分类启发式 ────────────────────────────────────────


def _is_flat_image(pil_image):
    """判断图片是否近乎纯色/无结构（典型装饰背景、色块）。"""
    small = pil_image.convert("RGB").resize((24, 24))
    arr = np.asarray(small, dtype=np.float32)
    ch_mean = arr.mean(axis=(0, 1))
    global_std = float(arr.std())
    channel_std = float(arr.std(axis=(0, 1)).mean())
    # 整体颜色极均匀 → 无结构
    if global_std < 18:
        return True
    # 各通道偏移很小 → 灰/白/黑单色
    if channel_std < 10:
        return True
    return False


def classify_image(pil_image, page_w, page_h):
    """按尺寸 + 内容结构 + OCR 文本量对单张图片分类。返回 (category, ocr_text)。"""
    width, height = pil_image.size
    area_ratio = (width * height) / max(1, page_w * page_h)
    aspect = width / max(1, height)

    # 分隔线 / 超宽横幅 / 极长条
    if aspect > 12 or aspect < 1 / 12:
        return "decorative", ""
    # 像素面积很小 → 边角点缀 / 缩略图标
    if width * height < 120 * 120:
        return "decorative", ""

    ocr_text = ""
    if area_ratio >= 0.02:
        ocr_text = _ocr_text_of_pil(pil_image)
        if len(ocr_text.strip()) >= 12:
            return "text_image", ocr_text

    # 尺寸足够、无有效文本：近纯色/无结构 → 装饰背景，否则 → 纯视觉图
    if _is_flat_image(pil_image):
        return "decorative", ocr_text
    return "visual_image", ocr_text


# ── 核心抽取 ──────────────────────────────────────────


def extract_pdf_images(pdf_path, out_dir, classify=True):
    """遍历 PDF 每页，提取图片并分类。

    返回 manifest dict；每张图会写入 <out_dir>/<doc sanitized>/pg_<page>_<i>.<ext>。
    """
    import fitz  # PyMuPDF（延迟导入便于 import 时缺依赖也能看报错）

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    doc_name = "".join(
        c if c.isalnum() or c in "._-" else "_" for c in pdf_path.stem
    )
    img_root = out_dir / doc_name
    img_root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "source": str(pdf_path),
        "doc_name": doc_name,
        "ocr_engine": "none",
        "pages": [],
    }

    doc = fitz.open(str(pdf_path))
    try:
        for page_no, page in enumerate(doc, start=1):
            page_w, page_h = page.rect.width, page.rect.height
            page_entry = {"page": page_no, "images": []}

            # 去重：同一 xref 在页内多次渲染只记一次
            seen_xref = set()
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen_xref:
                    continue
                seen_xref.add(xref)

                try:
                    base = doc.extract_image(xref)
                except Exception:
                    continue

                # 用实际渲染的 bbox 判断尺寸，而非图字典原始尺寸
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                bbox = rects[0]

                pil_image = Image.open(io.BytesIO(base["image"])).convert("RGB")
                category = None
                ocr_text = ""
                if classify:
                    category, ocr_text = classify_image(
                        pil_image, page_w, page_h
                    )
                else:
                    category = "unknown"

                rel = f"pg_{page_no}_{len(page_entry['images']) + 1}.{base['ext']}"
                out_path = img_root / rel
                pil_image.save(out_path, base["ext"])

                page_entry["images"].append({
                    "xref": xref,
                    "file": rel,
                    "category": category,
                    "ocr_text": ocr_text,
                    "bbox": [round(v, 1) for v in bbox],
                    "pixel_size": list(pil_image.size),
                })

            manifest["pages"].append(page_entry)
    finally:
        doc.close()

    manifest["ocr_engine"] = _ocr_name
    return manifest


# ── 命令行入口 ────────────────────────────────────────


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 1:
        print("用法: python pdf_image_extractor.py <输入.pdf> [输出目录]")
        print("输出目录默认: ./data/pdf_images")
        return 2

    pdf_path = argv[0]
    if not Path(pdf_path).exists():
        print(f"文件不存在: {pdf_path}")
        return 1

    out_dir = argv[1] if len(argv) > 1 else "data/pdf_images"
    manifest = extract_pdf_images(pdf_path, out_dir)
    out_path = Path(out_dir) / f"{manifest['doc_name']}_manifest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    counts = {"text_image": 0, "visual_image": 0, "decorative": 0, "unknown": 0}
    total = 0
    for page in manifest["pages"]:
        for img in page["images"]:
            counts[img["category"]] = counts.get(img["category"], 0) + 1
            total += 1

    print(f"OCR 引擎 : {manifest['ocr_engine']}")
    print(f"抽取图片 : {total} 张")
    print(f"  含字图   : {counts.get('text_image', 0)}")
    print(f"  纯视觉图 : {counts.get('visual_image', 0)}")
    print(f"  装饰图   : {counts.get('decorative', 0)}")
    print(f"Manifest  : {out_path}")
    print(f"图片目录  : {Path(out_dir) / manifest['doc_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())