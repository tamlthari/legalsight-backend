import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import DocumentStatus


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    full_name: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    matter_code: str | None
    title: str
    filename: str
    size_bytes: int
    status: DocumentStatus
    page_count: int | None
    chunk_count: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    matter_code: str | None = Field(default=None, max_length=120)
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=6, ge=1, le=12)


class Citation(BaseModel):
    document_id: uuid.UUID
    title: str
    page: int
    score: float


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]

