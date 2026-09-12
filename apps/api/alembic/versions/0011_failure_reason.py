"""실패 원인 분류값(failure_reason) — 실패 코드 옆에 '무엇이 깨졌는지'를 남긴다

`SUMMARY_INVALID_RESPONSE` 하나에 JSON 파싱 실패·응답 봉투 형태 위반·출력 절단·
필드 타입 위반 등 7가지 원인이 뭉쳐 있다. 지금까지 이 구분은 로그에만 있었고, 오류
보고서가 담는 것은 실패 코드와 sidecar.log 마지막 200줄뿐이다. 실기기 보고서에서는
그 200줄이 전부 OCR 폴링이라 원인을 하나도 좁히지 못했다.

값은 코드가 정하는 분류값만 들어간다(모델 응답 원문·문서 본문은 넣지 않는다).

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_jobs", sa.Column("failure_reason", sa.String(50), nullable=True)
    )
    op.add_column(
        "summary_runs", sa.Column("failure_reason", sa.String(50), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("summary_runs", "failure_reason")
    op.drop_column("document_jobs", "failure_reason")
