"""Q&A 스트리밍 — 스트림 필드 + 활성 assistant 인덱스 확장

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("qa_messages", sa.Column("draft_content", sa.Text(), nullable=True))
    op.add_column("qa_messages", sa.Column("stream_request_id", sa.String(64), nullable=True))
    op.add_column(
        "qa_messages", sa.Column("stream_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "qa_messages", sa.Column("stream_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "qa_messages", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "qa_messages", sa.Column("interrupted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "qa_messages",
        sa.Column("last_stream_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    # SQLite ALTER는 FK 추가를 지원하지 않으므로 plain Uuid(앱 레벨 참조)로 둔다
    op.add_column("qa_messages", sa.Column("retry_of_message_id", sa.Uuid(), nullable=True))
    op.create_index(
        "uq_qa_stream_request_id",
        "qa_messages",
        ["stream_request_id"],
        unique=True,
        sqlite_where=sa.text("stream_request_id IS NOT NULL"),
    )

    # 활성 assistant 부분 유니크 인덱스를 pending + streaming + finalizing로 확장한다.
    # 스레드당 진행 중 답변 1개(비스트림·스트림 공통)를 DB 레벨에서 보장.
    op.execute("DROP INDEX IF EXISTS uq_qa_active_answer")
    op.execute(
        "CREATE UNIQUE INDEX uq_qa_active_answer ON qa_messages(thread_id) "
        "WHERE role = 'assistant' AND status IN ('pending','streaming','finalizing')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_qa_active_answer")
    op.execute(
        "CREATE UNIQUE INDEX uq_qa_active_answer ON qa_messages(thread_id) "
        "WHERE role = 'assistant' AND status = 'pending'"
    )
    op.drop_index("uq_qa_stream_request_id", table_name="qa_messages")
    op.drop_column("qa_messages", "retry_of_message_id")
    op.drop_column("qa_messages", "last_stream_seq")
    op.drop_column("qa_messages", "interrupted_at")
    op.drop_column("qa_messages", "cancel_requested_at")
    op.drop_column("qa_messages", "stream_updated_at")
    op.drop_column("qa_messages", "stream_started_at")
    op.drop_column("qa_messages", "stream_request_id")
    op.drop_column("qa_messages", "draft_content")
