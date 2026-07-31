"""요약 실행·산출물 + 문서 revision + 활성 잡 유니크 인덱스

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 문서 콘텐츠 revision — 청크/요약 stale 판정 기준
    op.add_column(
        "documents",
        sa.Column("content_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("documents", sa.Column("chunk_revision", sa.Integer(), nullable=True))

    op.create_table(
        "summary_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("provider_name", sa.String(50), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("source_chunk_hash", sa.String(64), nullable=False),
        sa.Column("learner_level", sa.String(30), nullable=False),
        sa.Column("language", sa.String(10), nullable=False, server_default="ko"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_summary_runs_document_id", "summary_runs", ["document_id"])
    op.create_index(
        "ix_summary_runs_doc_created", "summary_runs", ["document_id", "created_at"]
    )

    op.create_table(
        "summary_artifacts",
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
        sa.Column("artifact_type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("source_chunk_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_summary_artifacts_document_id", "summary_artifacts", ["document_id"])
    op.create_index("ix_summary_artifacts_doc", "summary_artifacts", ["document_id"])
    op.create_index(
        "ix_summary_artifacts_run_position",
        "summary_artifacts",
        ["summary_run_id", "position"],
    )

    # 요약 모델 설정 — 단일 행(1 user). API 키는 여기 저장하지 않고 OS keyring에 둔다.
    op.create_table(
        "summary_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider_type", sa.String(30), nullable=False, server_default="disabled"),
        sa.Column("endpoint", sa.String(500), nullable=True),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # 인덱스 생성 전에, 기존(체크-후-삽입) 코드가 허용했을 수 있는 중복 활성 잡을 정리한다.
    # (document_id, job_type)별로 가장 최근 것만 남기고 나머지 활성 잡을 FAILED로 확정한다 —
    # 그러지 않으면 유니크 인덱스 생성이 실패해 앱이 기동되지 않는다.
    op.execute(
        """
        UPDATE document_jobs
        SET status = 'failed', failure_code = 'SUPERSEDED'
        WHERE status IN ('queued','running')
          AND id NOT IN (
            SELECT id FROM (
              SELECT id,
                     ROW_NUMBER() OVER (
                       PARTITION BY document_id, job_type ORDER BY created_at DESC, id DESC
                     ) AS rn
              FROM document_jobs
              WHERE status IN ('queued','running')
            ) ranked
            WHERE rn = 1
          )
        """
    )

    # 문서별·유형별 활성 잡(queued/running)을 DB 레벨에서 1개로 제한한다.
    # 체크-후-삽입 경쟁을 제거 — 중복 요약/OCR/청크/추출 잡 삽입은 IntegrityError로 막힌다.
    op.execute(
        "CREATE UNIQUE INDEX uq_active_job_per_type ON document_jobs(document_id, job_type) "
        "WHERE status IN ('queued','running')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_active_job_per_type")
    op.drop_table("summary_settings")
    op.drop_index("ix_summary_artifacts_run_position", table_name="summary_artifacts")
    op.drop_index("ix_summary_artifacts_doc", table_name="summary_artifacts")
    op.drop_index("ix_summary_artifacts_document_id", table_name="summary_artifacts")
    op.drop_table("summary_artifacts")
    op.drop_index("ix_summary_runs_doc_created", table_name="summary_runs")
    op.drop_index("ix_summary_runs_document_id", table_name="summary_runs")
    op.drop_table("summary_runs")
    op.drop_column("documents", "chunk_revision")
    op.drop_column("documents", "content_revision")
