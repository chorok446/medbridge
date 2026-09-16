"""대형 문서 청크의 shadow generation과 O(1) 활성 포인터

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_chunk_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("shadow_document_id", sa.String(50), nullable=False, unique=True),
        sa.Column("owner_job_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="building"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_document_chunk_generations_document_id",
        "document_chunk_generations",
        ["document_id"],
    )
    op.create_index(
        "ix_chunk_generations_document_created",
        "document_chunk_generations",
        ["document_id", "created_at"],
    )
    op.create_index(
        "uq_chunk_generations_building_document",
        "document_chunk_generations",
        ["document_id"],
        unique=True,
        sqlite_where=sa.text("status = 'building'"),
    )

    # 두 nullable UUID 컬럼 추가는 기존 청크를 복사하지 않는 SQLite fast path다.
    # NULL은 마이그레이션 전 legacy 활성 세트를 뜻하므로 별도 backfill도 필요 없다.
    op.add_column("documents", sa.Column("active_chunk_generation_id", sa.Uuid(), nullable=True))
    # Alembic의 SQLite add_column(ForeignKey)은 별도 ALTER CONSTRAINT까지 시도해
    # 실패한다. SQLite 자체는 nullable REFERENCES column의 inline ADD를 지원하며 기존
    # 행을 복사하지 않는다. 대형 document_chunks를 batch recreate하지 않기 위해 raw
    # ALTER를 사용한다.
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN generation_id CHAR(32) "
        "REFERENCES document_chunk_generations(id) ON DELETE CASCADE"
    )
    # generation-aware 인덱스가 두 legacy 인덱스의 prefix/정렬 용도를 대체한다. 먼저
    # 내려 migration 중 중복 인덱스만큼의 디스크와 이후 write 증폭을 만들지 않는다.
    op.drop_index("ix_document_chunks_doc_hash", table_name="document_chunks")
    op.drop_index("ix_document_chunks_doc_order", table_name="document_chunks")
    op.create_index(
        "ix_document_chunks_doc_generation_order",
        "document_chunks",
        ["document_id", "generation_id", "chunk_index"],
    )
    op.create_index(
        "ix_document_chunks_doc_generation_hash",
        "document_chunks",
        ["document_id", "generation_id", "content_hash"],
    )

    # 문서 삭제 cascade가 generation metadata를 없앨 때 독립 FTS5 shadow도 정리한다.
    op.execute(
        """
        CREATE TRIGGER trg_chunk_generation_delete_shadow_fts
        AFTER DELETE ON document_chunk_generations
        BEGIN
          DELETE FROM document_chunks_fts
           WHERE document_id = OLD.shadow_document_id;
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_chunk_generation_delete_shadow_fts")
    # 0013에는 generation 개념이 없다. 각 문서의 활성 generation만 legacy(NULL)로
    # 승격하고 나머지는 제거해야 rollback 뒤 중복 청크가 모두 활성처럼 보이지 않는다.
    # 전환 직후 cleanup 전에 crash한 DB에는 legacy(NULL) 구세트도 함께 남으므로 먼저
    # 제거한다. active pointer가 NULL인 문서는 아직 legacy가 활성이라 그대로 보존한다.
    op.execute(
        """
        DELETE FROM document_chunks
         WHERE generation_id IS NULL
           AND document_id IN (
                 SELECT id FROM documents
                  WHERE active_chunk_generation_id IS NOT NULL
               )
        """
    )
    op.execute(
        """
        DELETE FROM document_chunks_fts
         WHERE document_id IN (
                 SELECT substr(id, 1, 8) || '-' || substr(id, 9, 4) || '-' ||
                        substr(id, 13, 4) || '-' || substr(id, 17, 4) || '-' ||
                        substr(id, 21, 12)
                   FROM documents
                  WHERE active_chunk_generation_id IS NOT NULL
               )
        """
    )
    op.execute(
        """
        DELETE FROM document_chunks
         WHERE generation_id IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM documents d
                  WHERE d.id = document_chunks.document_id
                    AND d.active_chunk_generation_id = document_chunks.generation_id
               )
        """
    )
    op.execute(
        """
        DELETE FROM document_chunks_fts
         WHERE document_id IN (
                 SELECT g.shadow_document_id
                   FROM document_chunk_generations g
              LEFT JOIN documents d ON d.active_chunk_generation_id = g.id
                  WHERE d.id IS NULL
               )
        """
    )
    # FTS5 UPDATE의 내부 비용은 여기서는 허용한다. downgrade는 오프라인 작업이고,
    # 본문을 재토큰화하더라도 0013이 요구하는 실제 document UUID key로 돌려야 한다.
    op.execute(
        """
        UPDATE document_chunks_fts
           SET document_id = (
                 SELECT substr(g.document_id, 1, 8) || '-' ||
                        substr(g.document_id, 9, 4) || '-' ||
                        substr(g.document_id, 13, 4) || '-' ||
                        substr(g.document_id, 17, 4) || '-' ||
                        substr(g.document_id, 21, 12)
                   FROM document_chunk_generations g
                   JOIN documents d ON d.active_chunk_generation_id = g.id
                  WHERE g.shadow_document_id = document_chunks_fts.document_id
               )
         WHERE document_id IN (
                 SELECT g.shadow_document_id
                   FROM document_chunk_generations g
                   JOIN documents d ON d.active_chunk_generation_id = g.id
               )
        """
    )
    op.execute(
        """
        UPDATE document_chunks
           SET generation_id = NULL
         WHERE generation_id IN (
                 SELECT active_chunk_generation_id FROM documents
                  WHERE active_chunk_generation_id IS NOT NULL
               )
        """
    )
    op.drop_index("ix_document_chunks_doc_generation_hash", table_name="document_chunks")
    op.drop_index("ix_document_chunks_doc_generation_order", table_name="document_chunks")
    op.create_index(
        "ix_document_chunks_doc_order",
        "document_chunks",
        ["document_id", "chunk_index"],
    )
    op.create_index(
        "ix_document_chunks_doc_hash",
        "document_chunks",
        ["document_id", "content_hash"],
    )
    op.drop_column("document_chunks", "generation_id")
    op.drop_column("documents", "active_chunk_generation_id")
    op.drop_index("ix_chunk_generations_document_created", table_name="document_chunk_generations")
    op.drop_index(
        "uq_chunk_generations_building_document",
        table_name="document_chunk_generations",
    )
    op.drop_index(
        "ix_document_chunk_generations_document_id", table_name="document_chunk_generations"
    )
    op.drop_table("document_chunk_generations")
