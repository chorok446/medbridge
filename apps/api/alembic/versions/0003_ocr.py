"""OCR 실행 기록 + 단어 출처·페이지 OCR 상태

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("document_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("engine", sa.String(30), nullable=False, server_default="tesseract"),
        sa.Column("engine_version", sa.String(30), nullable=False, server_default=""),
        sa.Column("language", sa.String(30), nullable=False, server_default="kor+eng"),
        sa.Column("render_dpi", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("preprocessing_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="RUNNING"),
        sa.Column("mean_confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("median_confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("low_confidence_ratio", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ocr_runs_document_id", "ocr_runs", ["document_id"])
    op.create_index("ix_ocr_runs_page_id", "ocr_runs", ["page_id"])

    op.add_column(
        "document_words",
        sa.Column("source_method", sa.String(10), nullable=False, server_default="digital"),
    )
    op.add_column("document_pages", sa.Column("ocr_status", sa.String(30), nullable=True))
    op.add_column(
        "document_pages", sa.Column("ocr_mean_confidence", sa.Float(), nullable=True)
    )
    # extraction_method 의미 체계 정리: pymupdf → digital
    op.execute(
        "UPDATE document_pages SET extraction_method = 'digital' "
        "WHERE extraction_method = 'pymupdf'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE document_pages SET extraction_method = 'pymupdf' "
        "WHERE extraction_method = 'digital'"
    )
    op.drop_column("document_pages", "ocr_mean_confidence")
    op.drop_column("document_pages", "ocr_status")
    op.drop_column("document_words", "source_method")
    op.drop_index("ix_ocr_runs_page_id", table_name="ocr_runs")
    op.drop_index("ix_ocr_runs_document_id", table_name="ocr_runs")
    op.drop_table("ocr_runs")
