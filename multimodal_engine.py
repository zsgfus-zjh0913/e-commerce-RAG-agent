"""商品图片理解与多模态检索引擎。

该模块只负责图片/OCR/向量检索，不直接处理 Flask 请求。
运行时会优先使用 PaddleOCR + 中文 CLIP；依赖不可用时自动降级。
"""

import io
import json
import os
import re
import site
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
_rag_libs = os.environ.get("RAG_LIBS_PATH", r"C:\rag_libs")
if os.path.isdir(_rag_libs):
    site.addsitedir(_rag_libs)

from database import Product, close_db, get_db


class ProductMultimodalEngine:
    """为商品知识库提供 OCR、图像向量和混合检索。"""

    DEFAULT_CLIP_MODEL = os.environ.get(
        "MULTIMODAL_CLIP_MODEL",
        "OFA-Sys/chinese-clip-vit-base-patch16",
    )
    INDEX_VERSION = 2
    FALLBACK_DIM = 192

    def __init__(self, data_dir="data", index_dir="index"):
        self.data_dir = Path(data_dir)
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.image_manifest_path = self.data_dir / "product_images" / "index.json"
        self.vector_path = self.index_dir / "product_multimodal_vectors.npz"
        self.meta_path = self.index_dir / "product_multimodal_meta.json"

        self._clip_model = None
        self._clip_processor = None
        self._clip_attempted = False
        self._clip_error = None
        self._ocr_engine = None
        self._ocr_name = None
        self._ocr_attempted = False

        self.product_ids = []
        self.product_names = []
        self.catalog_texts = []
        self.text_vectors = None
        self.image_vectors = None
        self.image_present = None
        self.vector_provider = "fallback"

    # ── OCR ──────────────────────────────────────────────

    def _load_ocr(self):
        if self._ocr_attempted:
            return
        self._ocr_attempted = True
        requested = os.environ.get("MULTIMODAL_OCR_ENGINE", "auto").lower()

        if requested in ("auto", "paddle", "paddleocr"):
            try:
                from paddleocr import PaddleOCR

                try:
                    self._ocr_engine = PaddleOCR(
                        use_angle_cls=True,
                        lang="ch",
                        show_log=False,
                    )
                except TypeError:
                    self._ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch")
                self._ocr_name = "paddleocr"
                return
            except Exception as exc:
                if requested in ("paddle", "paddleocr"):
                    self._ocr_name = f"paddleocr-unavailable: {exc}"

        if requested in ("auto", "rapid", "rapidocr"):
            try:
                from rapidocr_onnxruntime import RapidOCR

                self._ocr_engine = RapidOCR()
                self._ocr_name = "rapidocr"
            except Exception as exc:
                self._ocr_name = f"rapidocr-unavailable: {exc}"

    def ocr_image(self, image):
        """返回 [{"text", "score", "box"}]。"""
        self._load_ocr()
        if self._ocr_engine is None:
            return []

        array = np.asarray(image.convert("RGB"))
        try:
            if self._ocr_name == "paddleocr":
                raw = self._ocr_engine.ocr(array, cls=True)
                return self._parse_paddle_result(raw)
            result, _ = self._ocr_engine(array)
            return self._parse_rapid_result(result)
        except Exception:
            return []

    @staticmethod
    def _parse_paddle_result(raw):
        items = []
        for page in raw or []:
            for line in page or []:
                try:
                    box, value = line
                    text, score = value
                    if text:
                        items.append({
                            "text": str(text).strip(),
                            "score": float(score),
                            "box": box,
                        })
                except (TypeError, ValueError):
                    continue
        return items

    @staticmethod
    def _parse_rapid_result(raw):
        items = []
        for line in raw or []:
            try:
                box, text, score = line
                if text:
                    items.append({
                        "text": str(text).strip(),
                        "score": float(score),
                        "box": box,
                    })
            except (TypeError, ValueError):
                continue
        return items

    # ── CLIP / fallback embedding ────────────────────────

    def _load_clip(self):
        if self._clip_attempted:
            return
        self._clip_attempted = True
        try:
            if "chinese-clip" in self.DEFAULT_CLIP_MODEL.lower():
                from transformers import ChineseCLIPModel, ChineseCLIPProcessor
                model_cls = ChineseCLIPModel
                processor_cls = ChineseCLIPProcessor
            else:
                from transformers import CLIPModel, CLIPProcessor
                model_cls = CLIPModel
                processor_cls = CLIPProcessor

            self._clip_processor = processor_cls.from_pretrained(
                self.DEFAULT_CLIP_MODEL
            )
            self._clip_model = model_cls.from_pretrained(
                self.DEFAULT_CLIP_MODEL
            )
            self._clip_model.eval()
            self.vector_provider = self.DEFAULT_CLIP_MODEL
        except Exception as exc:
            self._clip_error = str(exc)
            self.vector_provider = "fallback"

    def _encode_images_with_clip(self, images):
        self._load_clip()
        if self._clip_model is None:
            return None

        import torch

        inputs = self._clip_processor(images=images, return_tensors="pt")
        with torch.no_grad():
            features = self._clip_model.get_image_features(**inputs)
        features = torch.nn.functional.normalize(features, dim=-1)
        return features.cpu().numpy().astype(np.float32)

    def _encode_texts_with_clip(self, texts):
        self._load_clip()
        if self._clip_model is None:
            return None

        import torch

        inputs = self._clip_processor(
            text=texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        with torch.no_grad():
            features = self._clip_model.get_text_features(**inputs)
        features = torch.nn.functional.normalize(features, dim=-1)
        return features.cpu().numpy().astype(np.float32)

    def encode_image(self, image):
        vector = self._encode_images_with_clip([image])
        if vector is not None:
            return vector[0], self.vector_provider
        return self._fallback_image_vector(image), "fallback"

    def encode_texts(self, texts):
        vector = self._encode_texts_with_clip(texts)
        if vector is not None:
            return vector, self.vector_provider
        return self._fallback_text_vectors(texts), "fallback"

    @classmethod
    def _fallback_image_vector(cls, image):
        import cv2

        rgb = np.asarray(image.convert("RGB"))
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        hists = []
        for channel, bins, limits in (
            (0, 16, (0, 180)),
            (1, 8, (0, 256)),
            (2, 8, (0, 256)),
        ):
            hist = cv2.calcHist(
                [hsv], [channel], None, [bins], limits
            ).flatten()
            hists.append(hist)

        small = cv2.resize(rgb, (12, 12), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 60, 140).astype(np.float32).flatten() / 255.0
        pixels = gray.astype(np.float32).flatten() / 255.0
        vector = np.concatenate(hists + [pixels, edges])
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    @classmethod
    def _fallback_text_vectors(cls, texts):
        vectors = []
        for text in texts:
            vector = np.zeros(cls.FALLBACK_DIM, dtype=np.float32)
            normalized = re.sub(r"\s+", "", str(text).lower())
            chunks = list(normalized)
            chunks += [
                normalized[i:i + 2]
                for i in range(max(0, len(normalized) - 1))
            ]
            for token in chunks:
                index = hash(token) % cls.FALLBACK_DIM
                vector[index] += 1.0
            norm = np.linalg.norm(vector)
            vectors.append(vector / norm if norm else vector)
        return np.asarray(vectors, dtype=np.float32)

    @staticmethod
    def _normalize_matrix(vectors):
        if vectors is None or len(vectors) == 0:
            return vectors
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    # ── Catalog index ────────────────────────────────────

    @staticmethod
    def _product_text(product):
        attributes = {}
        if product.attributes:
            try:
                attributes = json.loads(product.attributes)
            except Exception:
                attributes = {}
        parts = [
            product.product_id,
            product.name,
            product.brand or "",
            product.category or "",
            product.description or "",
        ]
        parts.extend(str(value) for value in attributes.values() if value)
        return "；".join(part for part in parts if part)

    def _load_products(self):
        db = get_db()
        try:
            return db.query(Product).order_by(Product.id.asc()).all()
        finally:
            close_db(db)

    @staticmethod
    def _catalog_signature(products):
        raw = [
            [
                product.id,
                product.product_id,
                product.name,
                product.category,
                product.description,
                product.updated_at,
                product.version,
            ]
            for product in products
        ]
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)

    def _load_manifest(self):
        if not self.image_manifest_path.exists():
            return {}
        try:
            data = json.loads(
                self.image_manifest_path.read_text(encoding="utf-8")
            )
            return data.get("products", data)
        except Exception:
            return {}

    def ensure_catalog_index(self, force=False):
        products = self._load_products()
        signature = self._catalog_signature(products)
        cached_signature = None
        if self.meta_path.exists():
            try:
                cached_signature = json.loads(
                    self.meta_path.read_text(encoding="utf-8")
                ).get("signature")
            except Exception:
                cached_signature = None

        if (
            not force
            and cached_signature == signature
            and self.vector_path.exists()
        ):
            data = np.load(self.vector_path, allow_pickle=False)
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if meta.get("index_version") != self.INDEX_VERSION:
                return self.ensure_catalog_index(force=True)
            self.product_ids = meta.get("product_ids", [])
            self.product_names = meta.get("product_names", self.product_ids)
            self.catalog_texts = meta.get("catalog_texts", [])
            self.text_vectors = data["text_vectors"]
            self.image_vectors = data["image_vectors"]
            self.image_present = data["image_present"].astype(bool)
            self.vector_provider = meta.get("provider", "cached")
            return

        self.product_ids = [product.product_id for product in products]
        self.product_names = [product.name for product in products]
        self.catalog_texts = [self._product_text(product) for product in products]
        if not products:
            self.text_vectors = np.zeros((0, self.FALLBACK_DIM), dtype=np.float32)
            self.image_vectors = np.zeros((0, self.FALLBACK_DIM), dtype=np.float32)
            self.image_present = np.zeros(0, dtype=bool)
            return

        text_vectors, provider = self.encode_texts(self.catalog_texts)
        text_vectors = self._normalize_matrix(text_vectors)
        dimension = text_vectors.shape[1]
        image_vectors = np.zeros((len(products), dimension), dtype=np.float32)
        image_present = np.zeros(len(products), dtype=bool)
        manifest = self._load_manifest()

        image_root = self.data_dir / "product_images"
        for index, product_id in enumerate(self.product_ids):
            paths = manifest.get(product_id, [])
            if isinstance(paths, str):
                paths = [paths]
            vectors = []
            for relative in paths:
                path = image_root / relative
                if not path.exists():
                    continue
                try:
                    image = Image.open(path).convert("RGB")
                    vector, _ = self.encode_image(image)
                    vectors.append(vector)
                except Exception:
                    continue
            if vectors:
                matrix = self._normalize_matrix(
                    np.asarray(vectors, dtype=np.float32)
                )
                mean = matrix.mean(axis=0)
                norm = np.linalg.norm(mean)
                image_vectors[index] = mean / norm if norm else mean
                image_present[index] = True

        self.text_vectors = text_vectors.astype(np.float32)
        self.image_vectors = image_vectors
        self.image_present = image_present
        self.vector_provider = provider
        np.savez(
            self.vector_path,
            text_vectors=self.text_vectors,
            image_vectors=self.image_vectors,
            image_present=self.image_present,
        )
        self.meta_path.write_text(
            json.dumps({
                "index_version": self.INDEX_VERSION,
                "signature": signature,
                "provider": provider,
                "updated_at": int(time.time()),
                "products": len(products),
                "product_ids": self.product_ids,
                "product_names": self.product_names,
                "catalog_texts": self.catalog_texts,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Retrieval ────────────────────────────────────────

    @staticmethod
    def _token_set(text):
        try:
            import jieba
            tokens = jieba.lcut(str(text).lower())
        except Exception:
            tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", str(text).lower())
        return {
            token.strip()
            for token in tokens
            if len(token.strip()) >= 2
        }

    def search(self, image, question="", top_k=3, min_score=0.20):
        self.ensure_catalog_index()
        if not self.product_ids:
            return {
                "ocr": [],
                "ocr_text": "",
                "results": [],
                "provider": self.vector_provider,
            }

        ocr_items = self.ocr_image(image)
        ocr_text = " ".join(item["text"] for item in ocr_items)
        query_text = f"{question} {ocr_text}".strip()

        has_catalog_images = bool(np.any(self.image_present))
        compatible = False
        query_image_vector = None
        query_text_vector = None

        # ── 尝试获取 CLIP 向量并检查维度兼容性 ──────────
        try:
            query_image_vector, _ = self.encode_image(image)
            query_text_vectors, _ = self.encode_texts([query_text or ""])
            compatible = (
                query_image_vector.shape[0] == self.text_vectors.shape[1]
                and len(self.text_vectors) > 0
            )
            if compatible:
                query_text_vector = query_text_vectors[0]
        except Exception:
            compatible = False

        if not compatible:
            # CLIP 不可用或维度不匹配 → 跳过向量检索，仅用 OCR 重合度
            image_scores = np.zeros(len(self.product_ids), dtype=np.float32)
            text_scores = np.zeros(len(self.product_ids), dtype=np.float32)
        else:
            image_to_text = self.text_vectors @ query_image_vector
            image_to_catalog = np.zeros(len(self.product_ids), dtype=np.float32)

            if has_catalog_images:
                image_to_catalog[self.image_present] = (
                    self.image_vectors[self.image_present] @ query_image_vector
                )
                image_scores = 0.7 * image_to_catalog + 0.3 * image_to_text
            else:
                # 图库无商品图片：跨模态「图→文」匹配极不可靠，降权到仅做辅助
                image_scores = 0.15 * image_to_text

            text_scores = self.text_vectors @ query_text_vector

        # ── OCR / 关键词重合 ────────────────────────────
        query_tokens = self._token_set(query_text)
        ocr_scores = np.zeros(len(self.product_ids), dtype=np.float32)
        exact_bonus = np.zeros(len(self.product_ids), dtype=np.float32)

        for index, catalog_text in enumerate(self.catalog_texts):
            catalog_tokens = self._token_set(catalog_text)
            if query_tokens and catalog_tokens:
                overlap = len(query_tokens & catalog_tokens)
                ocr_scores[index] = overlap / max(1, len(query_tokens))

            product_id = self.product_ids[index]
            if product_id.lower() in query_text.lower():
                exact_bonus[index] = 0.35
            model_tokens = re.findall(
                r"[a-z]{1,10}[-/]?\d[a-z0-9./-]*",
                f"{query_text} {question}".lower(),
            )
            for token in model_tokens:
                if token and token in catalog_text.lower():
                    exact_bonus[index] = max(exact_bonus[index], 0.55)

        # ── 综合打分（图库无图时加大文本和 OCR 权重） ──────
        if has_catalog_images:
            scores = (
                0.55 * image_scores
                + 0.20 * text_scores
                + 0.15 * ocr_scores
                + exact_bonus
            )
        else:
            scores = (
                0.05 * image_scores      # 跨模态仅辅助
                + 0.40 * text_scores     # 文本语义为主
                + 0.35 * ocr_scores      # OCR 重合
                + exact_bonus
            )

        # ── 按最低置信度过滤 ─────────────────────────────
        order = np.argsort(scores)[::-1][:max(1, top_k * 2)]
        results = []
        for index in order:
            score = float(scores[index])
            if score < min_score:
                continue
            evidence = []
            if float(image_scores[index]) > 0.01:
                evidence.append(f"图像相似度 {float(image_scores[index]):.3f}")
            if float(ocr_scores[index]) > 0.01:
                evidence.append(f"OCR 重合度 {float(ocr_scores[index]):.3f}")
            if exact_bonus[index] > 0:
                evidence.append("命中商品编号或型号")
            results.append({
                "product_id": self.product_ids[index],
                "name": self.product_names[index],
                "image_score": round(float(image_scores[index]), 4),
                "text_score": round(float(text_scores[index]), 4),
                "ocr_score": round(float(ocr_scores[index]), 4),
                "final_score": round(score, 4),
                "matched_evidence": evidence,
            })
            if len(results) >= top_k:
                break

        return {
            "ocr": ocr_items,
            "ocr_text": ocr_text,
            "results": results,
            "provider": self.vector_provider,
        }

    @staticmethod
    def load_image(content):
        try:
            image = Image.open(io.BytesIO(content))
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
        except Exception as exc:
            raise ValueError(f"图片无法解析: {exc}") from exc

        if image.width * image.height > 16_000_000:
            raise ValueError("图片像素过大，请上传较小的商品图片")
        return image

    def status(self):
        return {
            "clip_model": self.DEFAULT_CLIP_MODEL,
            "clip_loaded": self._clip_model is not None,
            "clip_error": self._clip_error,
            "ocr_engine": self._ocr_name,
            "vector_provider": self.vector_provider,
            "catalog_products": len(self.product_ids),
        }
