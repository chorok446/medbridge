"""계층 요약 체크포인트(summary_nodes) + run 진행률 카운터

대형 문서는 map/reduce 호출이 수백 회에 이른다. 중간 요약을 노드로 저장해 앱 종료·실패
후에도 성공한 노드를 재사용하고, 진행률을 실제 노드 수 기준으로 표시한다.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "summary_runs",
        sa.Column("planned_nodes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "summary_runs",
        sa.Column("completed_nodes", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "summary_nodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "summary_run_id",
            sa.Uuid(),
            sa.ForeignKey("summary_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_chunk_ids_json", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_summary_nodes_document_id", "summary_nodes", ["document_id"])
    op.create_index("ix_summary_nodes_summary_run_id", "summary_nodes", ["summary_run_id"])
    op.create_index(
        "uq_summary_nodes_run_level_pos",
        "summary_nodes",
        ["summary_run_id", "level", "position"],
        unique=True,
    )
    op.create_index(
        "ix_summary_nodes_doc_hash", "summary_nodes", ["document_id", "input_hash"]
    )


def downgrade() -> None:
    op.drop_index("ix_summary_nodes_doc_hash", table_name="summary_nodes")
    op.drop_index("uq_summary_nodes_run_level_pos", table_name="summary_nodes")
    op.drop_index("ix_summary_nodes_summary_run_id", table_name="summary_nodes")
    op.drop_index("ix_summary_nodes_document_id", table_name="summary_nodes")
    op.drop_table("summary_nodes")
    op.drop_column("summary_runs", "completed_nodes")
    op.drop_column("summary_runs", "planned_nodes")
