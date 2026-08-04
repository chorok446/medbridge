"""naive UTC datetime이 오프셋 없는 ISO 문자열로 새어 나가지 않는지 고정한다.

SQLite(aiosqlite)는 DateTime(timezone=True) 컬럼도 naive datetime으로 돌려주고,
오프셋 없는 문자열은 JS의 new Date()가 로컬 시각으로 해석해 표시 시각이
UTC 오프셋만큼 밀린다(KST 기준 9시간).
"""

import json
from datetime import UTC, datetime

from app.schemas.common import CamelModel, utc_isoformat

NAIVE = datetime(2026, 8, 4, 15, 24, 38, 32987)
AWARE = datetime(2026, 8, 4, 15, 24, 38, 32987, tzinfo=UTC)


class _Sample(CamelModel):
    created_at: datetime
    completed_at: datetime | None = None
    name: str = "sample"


def test_camel_model_stamps_utc_on_naive_datetime():
    sample = _Sample(created_at=NAIVE, completed_at=NAIVE)
    dumped = json.loads(sample.model_dump_json(by_alias=True))
    assert dumped["createdAt"].endswith(("Z", "+00:00"))
    assert dumped["completedAt"].endswith(("Z", "+00:00"))


def test_camel_model_keeps_aware_datetime_and_other_fields():
    dumped = json.loads(_Sample(created_at=AWARE).model_dump_json(by_alias=True))
    assert dumped["createdAt"].endswith(("Z", "+00:00"))
    assert dumped["completedAt"] is None
    assert dumped["name"] == "sample"


def test_utc_isoformat_stamps_naive_and_passes_none():
    assert utc_isoformat(None) is None
    assert utc_isoformat(NAIVE) == "2026-08-04T15:24:38.032987+00:00"
    assert utc_isoformat(AWARE) == "2026-08-04T15:24:38.032987+00:00"
