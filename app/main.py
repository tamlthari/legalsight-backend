import asyncio
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.aws_services import aws
from app.config import settings
from app.database import engine, get_db
from app.indexing import answer_with_visuals, embeddings
from app.models import Activity, Document, DocumentStatus, QueryLog, User
from app.schemas import AskRequest, AskResponse, DocumentOut, LoginRequest, RegisterRequest, TokenOut, UserOut
from app.security import create_token, current_user, hash_password, verify_password
from app.vector_store import vector_store

ALLOWED_TYPES = {
    "application/pdf": ".pdf", "image/png": ".png",
    "image/jpeg": ".jpg", "image/tiff": ".tiff",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="LegalSight Textract VAG API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    await db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/api/v1/auth/register", response_model=TokenOut, status_code=201)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> TokenOut:
    email = payload.email.lower()
    if await db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email đã tồn tại")
    user = User(email=email, full_name=payload.full_name.strip(), password_hash=hash_password(payload.password))
    db.add(user)
    await db.flush()
    db.add(Activity(owner_id=user.id, action="user_registered", detail={}))
    await db.commit()
    await db.refresh(user)
    return TokenOut(access_token=create_token(user.id), user=UserOut.model_validate(user))


@app.post("/api/v1/auth/login", response_model=TokenOut)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenOut:
    user = await db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email hoặc mật khẩu không đúng")
    return TokenOut(access_token=create_token(user.id), user=UserOut.model_validate(user))


@app.get("/api/v1/auth/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> User:
    return user


@app.post("/api/v1/documents", response_model=DocumentOut, status_code=202)
async def upload_document(
    file: UploadFile = File(...), title: str = Form(..., min_length=2, max_length=255),
    matter_code: str | None = Form(default=None, max_length=120),
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> Document:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Chỉ hỗ trợ PDF, PNG, JPEG và TIFF")
    filename = Path(file.filename or f"document{ALLOWED_TYPES[content_type]}").name
    document_id = uuid.uuid4()
    prefix = f"{settings.s3_prefix}/users/{user.id}/documents/{document_id}"
    s3_key = f"{prefix}/raw/{filename}"
    limit, size, temp_path = settings.max_upload_mb * 1024 * 1024, 0, ""
    try:
        with tempfile.NamedTemporaryFile(suffix=ALLOWED_TYPES[content_type], delete=False) as temp:
            temp_path = temp.name
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Tệp vượt quá {settings.max_upload_mb} MB")
                temp.write(chunk)
        if size == 0:
            raise HTTPException(400, "Tệp rỗng")
        with open(temp_path, "rb") as source:
            await asyncio.to_thread(aws.put_file, source, s3_key, content_type)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
        await file.close()

    document = Document(
        id=document_id, owner_id=user.id, matter_code=matter_code.strip() if matter_code else None,
        title=title.strip(), filename=filename, content_type=content_type,
        size_bytes=size, s3_key=s3_key, status=DocumentStatus.UPLOADED,
    )
    db.add(document)
    await db.commit()
    try:
        document.textract_job_id = await asyncio.to_thread(
            aws.start_analysis, s3_key, document_id.hex, document_id.hex,
        )
        document.status = DocumentStatus.TEXTRACT_SUBMITTED
        db.add(Activity(owner_id=user.id, action="document_uploaded", detail={"document_id": str(document_id), "filename": filename}))
        await db.commit()
        await db.refresh(document)
        return document
    except Exception as exc:
        document.status = DocumentStatus.FAILED
        document.error_message = str(exc)[:4000]
        await db.commit()
        raise HTTPException(502, f"Không thể khởi chạy Textract: {exc}")


@app.get("/api/v1/documents", response_model=list[DocumentOut])
async def list_documents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[Document]:
    return list((await db.scalars(
        select(Document).where(Document.owner_id == user.id).order_by(Document.created_at.desc())
    )).all())


@app.get("/api/v1/documents/{document_id}/download")
async def download_url(document_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    document = await db.scalar(select(Document).where(Document.id == document_id, Document.owner_id == user.id))
    if not document:
        raise HTTPException(404, "Không tìm thấy tài liệu")
    url = await asyncio.to_thread(
        aws.s3.generate_presigned_url, "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": document.s3_key}, ExpiresIn=300,
    )
    return {"url": url}


@app.delete("/api/v1/documents/{document_id}", status_code=204)
async def delete_document(document_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> None:
    document = await db.scalar(select(Document).where(Document.id == document_id, Document.owner_id == user.id))
    if not document:
        raise HTTPException(404, "Không tìm thấy tài liệu")
    prefix = document.s3_key.rsplit("/raw/", 1)[0]
    await asyncio.to_thread(vector_store.delete_document, str(user.id), str(document.id))
    await asyncio.to_thread(aws.delete_prefix, prefix)
    filename = document.filename
    await db.delete(document)
    db.add(Activity(owner_id=user.id, action="document_deleted", detail={"document_id": str(document_id), "filename": filename}))
    await db.commit()


@app.post("/api/v1/query", response_model=AskResponse)
async def ask(payload: AskRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> AskResponse:
    clauses: list[dict] = []
    if payload.matter_code:
        clauses.append({"matter_code": {"$eq": payload.matter_code}})
    if payload.document_ids:
        allowed = set((await db.scalars(select(Document.id).where(
            Document.owner_id == user.id, Document.status == DocumentStatus.READY,
            Document.id.in_(payload.document_ids),
        ))).all())
        if len(allowed) != len(set(payload.document_ids)):
            raise HTTPException(400, "Có tài liệu không tồn tại, chưa sẵn sàng hoặc không thuộc tài khoản")
        clauses.append({"document_id": {"$in": [str(item) for item in allowed]}})
    filter_ = clauses[0] if len(clauses) == 1 else {"$and": clauses} if clauses else None
    vector = (await asyncio.to_thread(embeddings, [payload.question]))[0]
    matches = await asyncio.to_thread(vector_store.query, str(user.id), vector, payload.top_k, filter_)
    answer, citations = await asyncio.to_thread(answer_with_visuals, payload.question, matches)
    db.add(QueryLog(owner_id=user.id, question=payload.question, answer=answer, matter_code=payload.matter_code, citations=citations))
    db.add(Activity(owner_id=user.id, action="query_created", detail={"sources": len(citations)}))
    await db.commit()
    return AskResponse(answer=answer, citations=citations)
