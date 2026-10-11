import asyncio
import base64
import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import fitz
from openai import OpenAI
from PIL import Image, ImageSequence
from sqlalchemy import select

from app.aws_services import aws
from app.config import settings
from app.database import SessionLocal
from app.models import Activity, Document, DocumentStatus
from app.vector_store import vector_store

openai_client = OpenAI(api_key=settings.openai_api_key)


def extract_pages(result: dict[str, Any]) -> dict[int, str]:
    lines: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
    for block in result.get("Blocks", []):
        if block.get("BlockType") != "LINE" or not block.get("Text"):
            continue
        box = block.get("Geometry", {}).get("BoundingBox", {})
        lines[int(block.get("Page", 1))].append((float(box.get("Top", 0)), float(box.get("Left", 0)), block["Text"]))
    return {
        page: "\n".join(text for _, _, text in sorted(items))
        for page, items in sorted(lines.items())
    }


def chunk_text(text: str, size: int = 2200, overlap: int = 300) -> list[str]:
    if not text.strip():
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            split = text.rfind("\n", start, end)
            if split > start + size // 2:
                end = split
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def render_pages(source: bytes, prefix: str, content_type: str) -> dict[int, str]:
    keys: dict[int, str] = {}
    if content_type == "application/pdf":
        document = fitz.open(stream=source, filetype="pdf")
        try:
            rendered = [page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).tobytes("png") for page in document]
        finally:
            document.close()
    else:
        rendered = []
        with Image.open(BytesIO(source)) as image:
            for frame in ImageSequence.Iterator(image):
                output = BytesIO()
                frame.convert("RGB").save(output, format="PNG")
                rendered.append(output.getvalue())
    for index, image in enumerate(rendered):
        key = f"{prefix}/pages/{index + 1}.png"
        aws.put_bytes(image, key, "image/png")
        keys[index + 1] = key
    return keys


def embeddings(texts: list[str]) -> list[list[float]]:
    output: list[list[float]] = []
    for offset in range(0, len(texts), 64):
        response = openai_client.embeddings.create(
            model=settings.openai_embedding_model,
            input=texts[offset:offset + 64],
            dimensions=settings.embedding_dimension,
        )
        output.extend(item.embedding for item in response.data)
    return output


async def mark_failed(document_id: uuid.UUID, message: str) -> None:
    async with SessionLocal() as db:
        document = await db.get(Document, document_id)
        if document:
            document.status = DocumentStatus.FAILED
            document.error_message = message[:4000]
            db.add(Activity(owner_id=document.owner_id, action="document_failed", detail={"document_id": str(document.id), "error": message[:500]}))
            await db.commit()


async def process_document(document_id: uuid.UUID) -> None:
    try:
        async with SessionLocal() as db:
            document = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
            if not document or document.status == DocumentStatus.READY:
                return
            if (
                document.status in {DocumentStatus.TEXTRACT_PROCESSING, DocumentStatus.INDEXING}
                and document.processing_started_at
                and document.processing_started_at > datetime.now(UTC) - timedelta(minutes=14)
            ):
                return
            if not document.textract_job_id:
                raise RuntimeError("Document has no Textract job ID")
            document.status = DocumentStatus.TEXTRACT_PROCESSING
            document.processing_started_at = datetime.now(UTC)
            await db.commit()
            job_id, s3_key, owner_id = document.textract_job_id, document.s3_key, document.owner_id
            title, matter_code, content_type = document.title, document.matter_code, document.content_type

        result = await asyncio.to_thread(aws.analysis_result, job_id)
        page_text = extract_pages(result)
        prefix = s3_key.rsplit("/raw/", 1)[0]
        await asyncio.to_thread(
            aws.put_bytes, json.dumps(result, ensure_ascii=False, default=str).encode(),
            f"{prefix}/textract/result.json", "application/json",
        )
        pdf_bytes = await asyncio.to_thread(aws.get_bytes, s3_key)
        page_keys = await asyncio.to_thread(render_pages, pdf_bytes, prefix, content_type)

        records: list[tuple[int, int, str]] = []
        for page, text in page_text.items():
            records.extend((page, index, chunk) for index, chunk in enumerate(chunk_text(text)))
        if not records:
            raise RuntimeError("Textract không trích xuất được văn bản từ tài liệu")

        async with SessionLocal() as db:
            document = await db.get(Document, document_id)
            if not document:
                return
            document.status = DocumentStatus.INDEXING
            await db.commit()

        vectors = await asyncio.to_thread(embeddings, [record[2] for record in records])
        payload = []
        for (page, index, text), vector in zip(records, vectors, strict=True):
            payload.append({
                "id": f"{document_id}:{page}:{index}",
                "values": vector,
                "metadata": {
                    "document_id": str(document_id), "title": title, "matter_code": matter_code or "",
                    "page": page, "chunk_index": index, "text": text, "page_image_key": page_keys.get(page, ""),
                },
            })
        await asyncio.to_thread(vector_store.upsert, str(owner_id), payload)

        async with SessionLocal() as db:
            document = await db.get(Document, document_id)
            if document:
                document.status = DocumentStatus.READY
                document.page_count = int(result.get("DocumentMetadata", {}).get("Pages") or len(page_text))
                document.chunk_count = len(records)
                document.error_message = None
                document.processing_started_at = None
                db.add(Activity(owner_id=owner_id, action="document_ready", detail={"document_id": str(document_id), "pages": document.page_count}))
                await db.commit()
    except Exception as exc:
        await mark_failed(document_id, str(exc))
        raise


def answer_with_visuals(question: str, matches: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    if not matches:
        return "Không tìm thấy nguồn phù hợp trong kho tri thức đã chọn.", []
    context: list[str] = []
    citations: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    seen_pages: set[tuple[str, int]] = set()
    for match in matches:
        meta = dict(match.metadata or {})
        page = int(meta.get("page", 0))
        doc_id = str(meta.get("document_id"))
        context.append(f"[Tài liệu: {meta.get('title')} | trang {page}]\n{meta.get('text', '')}")
        key = (doc_id, page)
        if key not in seen_pages:
            seen_pages.add(key)
            citations.append({"document_id": doc_id, "title": meta.get("title", ""), "page": page, "score": round(float(match.score or 0), 4)})
            image_key = meta.get("page_image_key")
            if image_key and len(images) < 4:
                image = aws.get_bytes(image_key)
                images.append({"type": "input_image", "image_url": f"data:image/png;base64,{base64.b64encode(image).decode()}"})

    user_content: list[dict[str, Any]] = [{
        "type": "input_text",
        "text": f"CÂU HỎI:\n{question}\n\nNGỮ CẢNH OCR:\n" + "\n\n".join(context),
    }, *images]
    response = openai_client.responses.create(
        model=settings.openai_vision_model,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": (
                "Bạn là trợ lý nghiên cứu pháp lý Việt Nam. Chỉ trả lời từ ngữ cảnh OCR và ảnh trang được cung cấp. "
                "Mỗi nhận định phải dẫn nguồn dạng [Tên tài liệu, trang N]. Nếu nguồn không đủ, nói rõ không đủ căn cứ. "
                "Không tự nhận đây là tư vấn pháp lý và không suy đoán điều khoản bị thiếu."
            )}]},
            {"role": "user", "content": user_content},
        ],
    )
    return response.output_text, citations
