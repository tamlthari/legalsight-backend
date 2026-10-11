"""initial schema

Revision ID: 20260920_01
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260920_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = postgresql.ENUM(
        "UPLOADED", "TEXTRACT_SUBMITTED", "TEXTRACT_PROCESSING", "INDEXING", "READY", "FAILED",
        name="documentstatus", create_type=False,
    )
    status.create(op.get_bind(), checkfirst=True)
    op.create_table("users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_table("documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("matter_code", sa.String(120)), sa.Column("title", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False), sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False), sa.Column("s3_key", sa.String(1024), nullable=False),
        sa.Column("textract_job_id", sa.String(128)), sa.Column("status", status, nullable=False),
        sa.Column("page_count", sa.Integer()), sa.Column("chunk_count", sa.Integer()), sa.Column("error_message", sa.Text()),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("s3_key"), sa.UniqueConstraint("textract_job_id"),
    )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])
    op.create_index("ix_documents_matter_code", "documents", ["matter_code"])
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_table("query_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False), sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("matter_code", sa.String(120)), sa.Column("citations", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_query_logs_owner_id", "query_logs", ["owner_id"])
    op.create_table("activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(80), nullable=False), sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_activities_owner_id", "activities", ["owner_id"])


def downgrade() -> None:
    op.drop_table("activities")
    op.drop_table("query_logs")
    op.drop_table("documents")
    op.drop_table("users")
    postgresql.ENUM(name="documentstatus").drop(op.get_bind(), checkfirst=True)
