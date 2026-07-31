"""documents 테이블

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

document_type = postgresql.ENUM(
    "LECTURE",
    "TEXTBOOK",
    "REVIEW",
    "SYSTEMATIC_REVIEW",
    "META_ANALYSIS",
    "RCT",
    "OBSERVATIONAL",
    "CASE_REPORT",
    "GUIDELINE",
    "EXAM",
    "PATIENT_CASE",
    "OTHER",
    "UNKNOWN",
    name="document_type",
    create_type=False,
)
processing_status = postgresql.ENUM(
    "CREATED",
    "UPLOADING",
    "UPLOADED",
    "QUEUED",
    "VALIDATING",
    "READY",
    "FAILED",
    "DELETING",
    "DELETED",
    name="processing_status",
    create_type=False,
)
processing_stage = postgresql.ENUM(
    "UPLOAD", "FILE_VALIDATION", name="processing_stage", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    document_type.create(bind, checkfirst=True)
    processing_status.create(bind, checkfirst=True)
    processing_stage.create(bind, checkfirst=True)
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=True),
        sa.Column("redacted_storage_key", sa.String(500), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False, server_default="application/pdf"),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("document_type", document_type, nullable=False, server_default="UNKNOWN"),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("processing_status", processing_status, nullable=False, server_default="CREATED"),
        sa.Column("processing_stage", processing_stage, nullable=False, server_default="UPLOAD"),
        sa.Column("processing_progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "external_evidence_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_user_created", "documents", ["user_id", "created_at"])
    op.create_index("ix_documents_user_sha256", "documents", ["user_id", "sha256"])
    op.create_index("ix_documents_status", "documents", ["processing_status"])


def downgrade() -> None:
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_user_sha256", table_name="documents")
    op.drop_index("ix_documents_user_created", table_name="documents")
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")
    bind = op.get_bind()
    processing_stage.drop(bind, checkfirst=True)
    processing_status.drop(bind, checkfirst=True)
    document_type.drop(bind, checkfirst=True)
