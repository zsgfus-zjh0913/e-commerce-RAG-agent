---
name: ecommerce-product-multimodal-rag
description: Implement or operate image-based product matching for the ecommerce assistant, including product-photo upload, PaddleOCR text extraction, CLIP or Qwen-VL image understanding, image-vector search, and image-plus-text RAG answering. Use for multimodal product retrieval or image-grounded product questions; do not use for generic image generation or text-only RAG changes.
---

# Ecommerce Product Multimodal RAG

Use this skill when a customer uploads a product photo and asks about its material, size, model, price, stock, or likely product identity. The outcome is a grounded answer backed by matched catalog products and existing text knowledge, not a free-form visual guess.

## Non-Negotiable Rules

- Never identify a product from pixels alone when the catalog evidence is weak. Return the closest candidates and ask for a clearer image or identifying detail.
- Treat OCR text and image similarity as retrieval signals, not final answers. Generate the answer only from matched product records and retrieved text context.
- Preserve product IDs in retrieval metadata and answers so every match is traceable.
- Keep the existing text RAG path available when OCR is empty, the image is unclear, or no product vector index exists.
- Do not upload customer images to external model APIs or install large model dependencies without explicit approval.

## Pipeline

1. Validate the upload.
   - Accept only image MIME types and enforce the existing upload-size limit.
   - Normalize orientation, strip metadata, and create a short-lived working copy. Do not persist raw customer photos in the knowledge base.
2. Extract visible text.
   - Run PaddleOCR in Chinese and English mode.
   - Keep recognized text, confidence, and bounding boxes.
   - Normalize obvious SKU, model-number, size, material, and brand tokens before search.
3. Represent the image.
   - Prefer a Chinese-capable CLIP model for local image embeddings.
   - Use Qwen-VL when richer visual reasoning or product captioning is required. If the selected Qwen endpoint does not expose embeddings, generate a structured visual description and embed that description with the text/image encoder instead of pretending the endpoint returns a vector.
   - Store one or more product-image vectors per catalog product in the configured vector store.
4. Retrieve candidates.
   - Query the image-vector index for visually similar product images.
   - Query product text using OCR tokens, model numbers, OCR-normalized phrases, and the customer question.
   - Keep a candidate union before scoring. Do not discard a strong visual match only because OCR is sparse.
5. Rank and ground.
   - Score candidates with a configurable combination of image similarity, OCR-to-attribute overlap, product-name/model match, and text-RAG relevance.
   - Require a confidence margin before claiming a single match. Otherwise return the top 2-3 likely products.
   - Build the answer from matched catalog fields such as material, size chart, model, price, stock, and description.
6. Generate the response.
   - Lead with the product identification or uncertainty statement.
   - Answer the actual question, then add only relevant attributes from the matched product.
   - State when the image is insufficient, when OCR conflicts with catalog data, or when the requested attribute is absent.

## Project Integration

- Product text knowledge lives in `data/商品信息.md`; keep product IDs and names consistent with it.
- Reuse the existing upload and authentication boundaries in `app.py`.
- Extend `rag_engine.py` for hybrid retrieval and `retriever_store.py` for image-vector persistence. Do not create a parallel RAG service unless the existing components cannot support the required storage.
- Prefer Qdrant when `QDRANT_URL` is configured. Otherwise store local vectors in an index under `index/` and persist only derived vectors and metadata.
- Suggested product-image layout:

```text
data/
  product_images/
    P001/
      front.jpg
      detail-01.jpg
  product_images/index.json
```

`index.json` should map `product_id` to image paths, captions, model version, and embedding revision.

## Retrieval Contract

Each image-search result should expose at least:

```json
{
  "product_id": "P001",
  "name": "纯棉圆领短袖T恤",
  "image_score": 0.91,
  "ocr_score": 0.72,
  "text_score": 0.83,
  "final_score": 0.86,
  "matched_evidence": ["OCR: 100% cotton", "image asset: front.jpg"]
}
```

Log the model version, index revision, OCR text, candidate IDs, and final scores for debuggability. Never log raw customer images or sensitive metadata.

## Failure and Fallback Behavior

- If OCR has no usable text, continue with image retrieval and ask a focused follow-up such as "请拍一下吊牌、尺码标或商品正面".
- If image retrieval is unavailable, fall back to existing text RAG and clearly avoid claiming that the photo was matched.
- If two candidates are within the confidence margin, present both with their distinguishing attributes.
- If the customer asks about real-time stock, price, logistics progress, or order status after matching a product ID, use the existing business-tool path instead of the static knowledge document.

## Validation

For every implementation or model change, verify:

- A known product image retrieves its correct `product_id` in the top 3.
- An image with a visible model number prioritizes the exact model.
- An ambiguous image returns uncertainty or multiple candidates instead of an invented match.
- OCR-empty images still exercise image retrieval and the text-RAG fallback.
- Answers contain no attribute that is absent from the matched product record.
- Existing text-only product questions and business queries continue to pass their current flows.

Use representative catalog images and customer-style photos separately. Do not tune thresholds only on clean product renders.
