"""초기 스키마 (SQLite) — app_profile, documents, document_jobs

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "display_name", sa.String(100), nullable=False, server_default="MedBridge 사용자"
        ),
        sa.Column("study_level", sa.String(20), nullable=False, server_default="BASIC"),
        sa.Column("preferred_language", sa.String(10), nullable=False, server_default="ko"),
        sa.Column(
            "external_ai_allowed", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_profile.id", ondelete="CASCADE"),
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
        sa.Column("document_type", sa.String(30), nullable=False, server_default="UNKNOWN"),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("processing_status", sa.String(30), nullable=False, server_default="CREATED"),
        sa.Column("processing_stage", sa.String(30), nullable=False, server_default="UPLOAD"),
        sa.Column("processing_progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "external_evidence_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
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

    op.create_table(
        "document_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="QUEUED"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_document_jobs_document_id", "document_jobs", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_document_jobs_document_id", table_name="document_jobs")
    op.drop_table("document_jobs")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_user_sha256", table_name="documents")
    op.drop_index("ix_documents_user_created", table_name="documents")
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")
    op.drop_table("app_profile")
