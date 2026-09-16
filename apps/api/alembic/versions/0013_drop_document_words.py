"""단어 단위 테이블 제거 — 쓰기만 하고 읽지 않던 1.27GB를 없앤다

document_words는 353만 행에 테이블 0.85GB + 인덱스 0.41GB로 실기기 DB(2.51GB)의
라이브 데이터 66%를 차지했다. 그런데 이 행을 읽는 곳은 한 군데, 그것도 COUNT(*)
하나뿐이었다(ocr/service.py) — 페이지의 디지털 단어 수를 다시 세기 위해서다.
텍스트·좌표·신뢰도는 아무도 읽지 않는다. 인용 하이라이트도 word가 아니라 block
기준이고(source_refs의 blockId), 검색은 청크를 쓴다. metadata_json은 353만 행 전부
`{}`였다.

그 COUNT(*)를 대신할 값만 페이지에 남긴다. digital_word_count는 추출 시점의 디지털
단어 수이고 OCR이 덮어쓰지 않는다 — word_count는 재실행 때 digital+ocr로 덮이므로
기준선이 따로 필요했고, 그게 지금까지 테이블을 살려 둔 유일한 이유였다.

되돌릴 수 없는 변경이다. 단어 단위 좌표가 필요한 기능(단어 하이라이트 등)을 나중에
만들려면 재추출해야 한다.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_pages",
        sa.Column(
            "digital_word_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    # 기존 DB의 값을 보존한다. 백필하지 않으면 이미 OCR을 돌린 페이지의 재실행이
    # 디지털 단어를 0으로 보고 word_count를 낮춰 잡는다.
    op.execute(
        """
        UPDATE document_pages
           SET digital_word_count = COALESCE((
                 SELECT COUNT(*) FROM document_words w
                  WHERE w.page_id = document_pages.id
                    AND w.source_method = 'digital'
               ), 0)
        """
    )
    op.drop_table("document_words")


def downgrade() -> None:
    # 구조만 되돌린다 — 지운 단어 행은 복원할 수 없다(재추출해야 한다).
    op.create_table(
        "document_words",
        sa.Column("id", sa.CHAR(32), primary_key=True),
        sa.Column("page_id", sa.CHAR(32), nullable=False),
        sa.Column("block_id", sa.CHAR(32), nullable=True),
        sa.Column("line_id", sa.CHAR(32), nullable=True),
        sa.Column("word_index", sa.Integer(), nullable=False),
        sa.Column("x0", sa.Float(), nullable=False),
        sa.Column("y0", sa.Float(), nullable=False),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "source_method", sa.String(10), nullable=False, server_default="digital"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["document_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["block_id"], ["document_blocks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["line_id"], ["document_lines.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_document_words_page_id", "document_words", ["page_id"])
    op.create_index("ix_document_words_block_id", "document_words", ["block_id"])
    op.create_index("ix_document_words_line_id", "document_words", ["line_id"])
    op.drop_column("document_pages", "digital_word_count")
