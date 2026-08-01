"""추출 페이지 교체용 document_words FK 인덱스 추가

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_document_words_block_id", "document_words", ["block_id"], unique=False
    )
    op.create_index(
        "ix_document_words_line_id", "document_words", ["line_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_document_words_line_id", table_name="document_words")
    op.drop_index("ix_document_words_block_id", table_name="document_words")
