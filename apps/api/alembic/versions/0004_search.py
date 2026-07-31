"""검색 청크 + FTS5 키워드 인덱스

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_title", sa.String(300), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("embedding_blob", sa.LargeBinary(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index(
        "ix_document_chunks_doc_order", "document_chunks", ["document_id", "chunk_index"]
    )
    op.create_index(
        "ix_document_chunks_doc_hash", "document_chunks", ["document_id", "content_hash"]
    )

    # FTS5는 alembic의 테이블 추상화로 표현할 수 없어 원시 SQL을 쓴다. content= 없는
    # 독립(standalone) 가상 테이블: UUID PK와 SQLite rowid 동기화의 취약성을 피하고,
    # 서비스 코드가 document_chunks 쓰기와 같은 트랜잭션에서 명시적으로 동기화한다.
    #
    # tokenize='trigram': 한국어는 공백이 어절(조사가 붙은 단위) 기준이라 unicode61의
    # 단어 토큰화로는 "심장" 검색이 "심장은"(본문에 실제 저장된 형태)에 매치되지 않는다.
    # trigram은 3자 이상 부분 문자열로 인덱싱해 형태소 분석 없이도 이 문제를 피한다
    # (실측 확인: unicode61은 실패, trigram은 3자 이상 질의에서 정상 매치).
    op.execute(
        "CREATE VIRTUAL TABLE document_chunks_fts USING fts5("
        "normalized_text, section_title, "
        "chunk_id UNINDEXED, document_id UNINDEXED, "
        "tokenize='trigram case_sensitive 0'"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_chunks_fts")
    op.drop_index("ix_document_chunks_doc_hash", table_name="document_chunks")
    op.drop_index("ix_document_chunks_doc_order", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
