"""후속 질문 저장 — 모델이 만든 제안을 버리지 않는다

verify()가 followUpSuggestions를 받아 _finalize()까지 넘겼지만 저장할 컬럼이 없어
그대로 버려졌다. 화면에 후속 질문 칩을 띄우려면 저장 경로가 필요하다.

기존 메시지는 NULL로 남기고 백필하지 않는다 — 지난 답변의 후속 질문은 이미 사라졌고,
없는 값을 지어내는 것보다 없다고 말하는 편이 정직하다.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("qa_messages", sa.Column("followups_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("qa_messages", "followups_json")
