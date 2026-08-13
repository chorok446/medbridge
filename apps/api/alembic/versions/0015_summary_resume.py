"""요약 자동 재개에 필요한 실행 옵션과 공급자 지문

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite는 ADD COLUMN의 별도 FK constraint 추가를 지원하지 않는다. nullable inline
    # REFERENCES는 테이블 복사 없이 안전하게 추가할 수 있다. 기존 행은 어떤 job과
    # 대응하는지 증명할 수 없으므로 NULL로 두고 자동 재개 대상에서 제외한다.
    op.execute(
        "ALTER TABLE summary_runs ADD COLUMN job_id CHAR(32) "
        "REFERENCES document_jobs(id) ON DELETE SET NULL"
    )
    op.add_column(
        "summary_runs",
        sa.Column("include_sections", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "summary_runs",
        sa.Column("include_prerequisites", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "summary_runs", sa.Column("provider_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "summary_runs",
        sa.Column(
            "provider_identity_resumable",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "summary_runs",
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "uq_summary_runs_job_id",
        "summary_runs",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text("job_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_summary_runs_job_id", table_name="summary_runs")
    op.drop_column("summary_runs", "resume_count")
    op.drop_column("summary_runs", "provider_identity_resumable")
    op.drop_column("summary_runs", "provider_fingerprint")
    op.drop_column("summary_runs", "include_prerequisites")
    op.drop_column("summary_runs", "include_sections")
    op.drop_column("summary_runs", "job_id")
