"""문서 기반 Q&A — 스레드·메시지·주장

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_threads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_qa_threads_document_id", "qa_threads", ["document_id"])
    op.create_index("ix_qa_threads_user_id", "qa_threads", ["user_id"])
    op.create_index("ix_qa_threads_doc_updated", "qa_threads", ["document_id", "updated_at"])

    op.create_table(
        "qa_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("qa_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default="completed"),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("document_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_revision", sa.Integer(), nullable=True),
        sa.Column("provider_name", sa.String(50), nullable=True),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("retrieval_mode", sa.String(20), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_qa_messages_thread_id", "qa_messages", ["thread_id"])
    op.create_index(
        "uq_qa_messages_thread_seq",
        "qa_messages",
        ["thread_id", "sequence_number"],
        unique=True,
    )
    # 스레드당 진행 중(pending) assistant 답변은 1개만 — 동시 답변 생성 차단(DB 레벨)
    op.execute(
        "CREATE UNIQUE INDEX uq_qa_active_answer ON qa_messages(thread_id) "
        "WHERE role = 'assistant' AND status = 'pending'"
    )

    op.create_table(
        "qa_claims",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("qa_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim_index", sa.Integer(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("verification_status", sa.String(30), nullable=False),
        sa.Column("source_chunk_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_qa_claims_message_id", "qa_claims", ["message_id"])
    op.create_index(
        "uq_qa_claims_message_index", "qa_claims", ["message_id", "claim_index"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_qa_claims_message_index", table_name="qa_claims")
    op.drop_index("ix_qa_claims_message_id", table_name="qa_claims")
    op.drop_table("qa_claims")
    op.execute("DROP INDEX IF EXISTS uq_qa_active_answer")
    op.drop_index("uq_qa_messages_thread_seq", table_name="qa_messages")
    op.drop_index("ix_qa_messages_thread_id", table_name="qa_messages")
    op.drop_table("qa_messages")
    op.drop_index("ix_qa_threads_doc_updated", table_name="qa_threads")
    op.drop_index("ix_qa_threads_user_id", table_name="qa_threads")
    op.drop_index("ix_qa_threads_document_id", table_name="qa_threads")
    op.drop_table("qa_threads")
