"""활성 잡 부분 유니크 인덱스의 status 리터럴 대소문자 불일치 수정

Revision ID: 0006
Revises: 0005

0005에서 만든 uq_active_job_per_type는 `WHERE status IN ('queued','running')`(소문자
값)로 필터했다. 그러나 document_jobs.status는 SQLAlchemy Enum(native_enum=False,
values_callable 미지정)이라 멤버 **이름**(대문자, 'QUEUED'/'RUNNING')으로 저장된다.
따라서 이 부분 인덱스가 ORM으로 만든 활성 잡을 전혀 커버하지 못해, 문서·유형별 동시
잡 1개를 DB 레벨에서 보장하려던 목적이 무력화돼 있었다(Python 체크-후-삽입만 남아
동시 요청 경쟁을 완전히 막지 못함).

수정: 인덱스를 다시 만들되 저장되는 실제 값(대문자)을 포함하고, 미래에 저장 규약이
값(소문자)으로 바뀌어도 견디도록 두 형태를 모두 WHERE에 넣는다. 데이터 변환은 하지
않는다(기존 행은 그대로).
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_ACTIVE = "'QUEUED','RUNNING','queued','running'"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_active_job_per_type")
    # 0005 인덱스가 무력화돼 있었으므로 중복 활성 잡이 남아있을 수 있다. 새 인덱스 생성
    # 전에 (document_id, job_type)별 최신 1개만 남기고 나머지를 실패로 확정한다(대문자·
    # 소문자 저장 형태 모두 대상). 그러지 않으면 유니크 인덱스 생성이 실패할 수 있다.
    op.execute(
        f"""
        UPDATE document_jobs
        SET status = 'FAILED', failure_code = 'SUPERSEDED'
        WHERE status IN ({_ACTIVE})
          AND id NOT IN (
            SELECT id FROM (
              SELECT id,
                     ROW_NUMBER() OVER (
                       PARTITION BY document_id, job_type ORDER BY created_at DESC, id DESC
                     ) AS rn
              FROM document_jobs
              WHERE status IN ({_ACTIVE})
            ) ranked
            WHERE rn = 1
          )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_active_job_per_type ON document_jobs(document_id, job_type) "
        f"WHERE status IN ({_ACTIVE})"
    )


def downgrade() -> None:
    # 0005의 (무력화됐던) 소문자 전용 정의로 되돌린다
    op.execute("DROP INDEX IF EXISTS uq_active_job_per_type")
    op.execute(
        "CREATE UNIQUE INDEX uq_active_job_per_type ON document_jobs(document_id, job_type) "
        "WHERE status IN ('queued','running')"
    )
