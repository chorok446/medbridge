"""추출 결과 테이블 5종 + documents 추출 캐시 컬럼

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _bbox_columns() -> list[sa.Column]:
    return [
        sa.Column("x0", sa.Float(), nullable=False),
        sa.Column("y0", sa.Float(), nullable=False),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("documents", sa.Column("extraction_engine", sa.String(30), nullable=True))
    op.add_column(
        "documents", sa.Column("extraction_engine_version", sa.String(30), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("extraction_schema_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column("extraction_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "document_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "extraction_method", sa.String(30), nullable=False, server_default="pymupdf"
        ),
        sa.Column(
            "extraction_status", sa.String(30), nullable=False, server_default="PENDING"
        ),
        sa.Column(
            "extraction_confidence", sa.Float(), nullable=False, server_default="1.0"
        ),
        sa.Column(
            "reading_order_confidence", sa.Float(), nullable=False, server_default="1.0"
        ),
        sa.Column("scan_verdict", sa.String(30), nullable=False, server_default="UNKNOWN"),
        sa.Column("text_character_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("image_area_ratio", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("requires_ocr", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("error_message_internal", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])
    op.create_index(
        "ix_document_pages_doc_page",
        "document_pages",
        ["document_id", "page_number"],
        unique=True,
    )

    op.create_table(
        "document_blocks",
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
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("block_type", sa.String(30), nullable=False, server_default="TEXT"),
        *_bbox_columns(),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("reading_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_header", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_footer", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_table", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_caption", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_document_blocks_document_id", "document_blocks", ["document_id"])
    op.create_index("ix_document_blocks_page_id", "document_blocks", ["page_id"])
    op.create_index(
        "ix_document_blocks_page_order", "document_blocks", ["page_id", "reading_order"]
    )

    op.create_table(
        "document_lines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("document_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "block_id",
            sa.Uuid(),
            sa.ForeignKey("document_blocks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_index", sa.Integer(), nullable=False),
        *_bbox_columns(),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("reading_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_document_lines_page_id", "document_lines", ["page_id"])
    op.create_index("ix_document_lines_block_id", "document_lines", ["block_id"])

    op.create_table(
        "document_words",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("document_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "block_id",
            sa.Uuid(),
            sa.ForeignKey("document_blocks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "line_id",
            sa.Uuid(),
            sa.ForeignKey("document_lines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("word_index", sa.Integer(), nullable=False),
        *_bbox_columns(),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_document_words_page_id", "document_words", ["page_id"])

    op.create_table(
        "document_tables",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("document_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("table_index", sa.Integer(), nullable=False),
        *_bbox_columns(),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("column_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cells_json", sa.JSON(), nullable=False),
        sa.Column("markdown_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "extraction_status", sa.String(30), nullable=False, server_default="extracted"
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_document_tables_page_id", "document_tables", ["page_id"])


def downgrade() -> None:
    for table in (
        "document_tables",
        "document_words",
        "document_lines",
        "document_blocks",
        "document_pages",
    ):
        op.drop_table(table)
    for column in (
        "extraction_completed_at",
        "extraction_schema_version",
        "extraction_engine_version",
        "extraction_engine",
    ):
        op.drop_column("documents", column)
