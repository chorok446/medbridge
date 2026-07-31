"""document_jobs 테이블

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

job_type = postgresql.ENUM("VALIDATE_FILE", name="job_type", create_type=False)
job_status = postgresql.ENUM(
    "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", name="job_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    job_type.create(bind, checkfirst=True)
    job_status.create(bind, checkfirst=True)
    op.create_table(
        "document_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_type", job_type, nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="QUEUED"),
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
    bind = op.get_bind()
    job_status.drop(bind, checkfirst=True)
    job_type.drop(bind, checkfirst=True)
