# 근거 우선 답변 화면 구현 계획

> **에이전트용:** 이 계획은 태스크 단위로 실행한다. 각 단계는 체크박스(`- [ ]`)다.

**목표:** 답변 문장 안에 검증된 근거만 가리키는 인용 번호를 박고, 질문 탭을 검색형 진입으로 바꾼다.

**설계 문서:** `docs/product/evidence-first-qa-design.md`

**접근:** 모델이 `answer` 산문에 `[c<n>]` 마커를 넣고, 서버가 검증되지 않은 마커를 제거한 뒤 최종 인덱스로 다시 쓴다. 프론트는 마커를 클릭 가능한 인용 pill로 렌더하고, 마커가 없으면 현행 렌더로 폴백한다. 후속 질문은 컬럼이 없으므로 마이그레이션 `0012`로 저장 경로를 만든다.

**기술 스택:** FastAPI · SQLAlchemy · Alembic · pytest / Next.js · React · TanStack Query · Tailwind · vitest

## 전역 제약

- 커밋 메시지에 Claude 공동저자 서명을 넣지 않는다.
- 기술 용어(chunkId, bbox, OCR, DPI, 모델명, 오류 코드)를 화면에 노출하지 않는다. — `PRODUCT.md` anti-reference
- 진단·처방·응급 판단 기능을 만들지 않는다. — `PRODUCT.md:17`
- box-shadow·그라디언트·중첩 카드를 UI에 쓰지 않는다. 브랜드 마크만 예외. — `DESIGN.md`
- 읽기용 본문은 17px / 행간 1.7. UI 라벨 최소 12px.
- 백엔드 테스트는 `apps/api`에서 실행한다: `.\.venv\Scripts\python.exe -m pytest ...`
- 프론트 테스트는 `apps/web`에서 실행한다: `npm test`
- `ruff check .` 와 `mypy app` 이 통과해야 한다. `mypy`는 Windows에서 `local_ai/system.py`의 `os.sysconf` 2건이 기존부터 뜨며 이번 작업과 무관하다.

---

## Task 1: 인용 마커 검증 (`verify()`)

**파일**
- 수정: `apps/api/app/services/qa/schema.py`
- 테스트: `apps/api/tests/unit/test_qa_schema.py`

**인터페이스**
- 산출: `verify()`가 돌려주는 `VerifiedAnswer.answer`에는 유효한 `[c<n>]` 마커만 남으며, `<n>`은 `VerifiedClaim.claim_index`와 일치한다.

**핵심 함정:** 모델이 쓰는 `<n>`은 **모델 자신의 `claims` 배열 인덱스**다. `verify()`는 dict가 아니거나 텍스트가 비었거나 중복인 항목을 건너뛰므로, 살아남은 claim의 `claim_index`는 원래 배열 위치와 어긋난다. 마커를 그대로 두면 엉뚱한 근거를 가리킨다. **원본 위치 → 최종 인덱스 매핑을 만들어 다시 써야 한다.**

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`apps/api/tests/unit/test_qa_schema.py`의 `TestVerify` 클래스 끝에 추가한다.

```python
    def test_valid_marker_survives(self):
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {"answer": "심장은 혈액을 보냅니다[c0].", "answerStatus": "answered",
             "claims": [{"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}]},
            lookup, had_results=True,
        )
        assert out.answer == "심장은 혈액을 보냅니다[c0]."

    def test_out_of_range_marker_is_removed(self):
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {"answer": "심장은 혈액을 보냅니다[c7].", "answerStatus": "answered",
             "claims": [{"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}]},
            lookup, had_results=True,
        )
        assert out.answer == "심장은 혈액을 보냅니다."

    def test_marker_to_unsupported_claim_is_removed(self):
        # 두 번째 claim은 출처가 없어 unsupported → 그 마커는 지운다.
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {"answer": "심장은 혈액을 보냅니다[c0]. 폐는 산소를 만듭니다[c1].",
             "answerStatus": "answered",
             "claims": [{"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]},
                        {"text": "폐는 산소를 만든다", "sourceChunkIds": ["없는청크"]}]},
            lookup, had_results=True,
        )
        assert "[c1]" not in out.answer
        assert "[c0]" in out.answer

    def test_marker_is_remapped_when_earlier_claim_is_skipped(self):
        # 0번 raw 항목이 빈 텍스트로 버려지면, 모델의 [c1]은 최종 claim_index 0을 가리켜야 한다.
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {"answer": "심장은 혈액을 보냅니다[c1].", "answerStatus": "answered",
             "claims": [{"text": "", "sourceChunkIds": ["c1"]},
                        {"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}]},
            lookup, had_results=True,
        )
        assert out.claims[0].claim_index == 0
        assert out.answer == "심장은 혈액을 보냅니다[c0]."

    def test_repeated_marker_for_same_claim_is_kept(self):
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {"answer": "심장은 혈액을 보냅니다[c0]. 다시 말해 그렇습니다[c0].",
             "answerStatus": "answered",
             "claims": [{"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}]},
            lookup, had_results=True,
        )
        assert out.answer.count("[c0]") == 2

    def test_answer_without_markers_is_untouched(self):
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {"answer": "심장은 혈액을 보냅니다.", "answerStatus": "answered",
             "claims": [{"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}]},
            lookup, had_results=True,
        )
        assert out.answer == "심장은 혈액을 보냅니다."

    def test_conflicting_claim_marker_survives(self):
        c1 = "초기 연구에서는 이 요법이 사망 위험을 감소시킨다고 보고하였다"
        c2 = "후속 연구에서는 이 요법이 사망 위험에 영향을 주지 않았다고 보고하였다"
        lookup = _lookup(("c1", c1), ("c2", c2))
        out = verify(
            {"answer": f"{c1}[c0] 그러나 {c2}[c1]", "answerStatus": "answered",
             "claims": [{"text": c1, "sourceChunkIds": ["c1"]},
                        {"text": c2, "sourceChunkIds": ["c2"]}]},
            lookup, had_results=True,
        )
        assert out.answer_status == "conflicting_evidence"
        assert "[c0]" in out.answer and "[c1]" in out.answer
```

- [ ] **Step 2: 실패를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_qa_schema.py -k marker -q`
예상: 마커가 그대로 남아 FAIL (`test_out_of_range_marker_is_removed` 등).

- [ ] **Step 3: 구현한다**

`schema.py` 상단 정규식 옆에 추가한다.

```python
# 답변 산문 안의 인용 마커. 문서 본문에 흔한 대괄호(`[1]`, `[표 3]`)와 겹치지 않도록
# `c` 접두사를 요구한다. MAX_CLAIMS=20이라 두 자리면 충분하다.
_CITATION_RE = re.compile(r"\[c(\d{1,2})\]")
```

`verify()` 안에서 원본 위치를 기록한다. `for raw in (...)` 루프를 `enumerate`로 바꾸고 매핑을 채운다.

```python
    verified: list[VerifiedClaim] = []
    seen_text: set[str] = set()
    # 모델이 쓴 마커 번호(원본 배열 위치) → 최종 claim_index. 건너뛴 항목 때문에 둘이
    # 어긋나므로, 이 표 없이 마커를 그대로 두면 엉뚱한 근거를 가리킨다.
    raw_to_index: dict[int, int] = {}
    idx = 0
    for raw_pos, raw in enumerate((model_output.get("claims") or [])[: MAX_CLAIMS * 2]):
        ...
        seen_text.add(text)
        raw_to_index[raw_pos] = idx
        verified.append(...)
        idx += 1
        if len(verified) >= MAX_CLAIMS:
            break
```

`unsupported` 필터링(`status in ("not_found", "insufficient_evidence")`) **뒤에** 마커 정리를 넣는다. 그래야 걸러진 claim의 마커도 함께 사라진다.

```python
def _rewrite_citations(
    answer: str, claims: list[VerifiedClaim], raw_to_index: dict[int, int]
) -> tuple[str, int]:
    """마커를 최종 claim_index로 다시 쓰고, 해석되지 않는 마커는 지운다.

    남은 마커는 전부 "검증을 통과해 화면에 보이는 claim"을 가리킨다. 모델이 없는 근거를
    지어내도 화면에 번호가 뜨지 않는다.
    """
    valid = {
        c.claim_index
        for c in claims
        if c.verification_status != QaClaimVerification.UNSUPPORTED
    }
    dropped = 0

    def _sub(m: re.Match) -> str:
        nonlocal dropped
        target = raw_to_index.get(int(m.group(1)))
        if target is None or target not in valid:
            dropped += 1
            return ""
        return f"[c{target}]"

    return _CITATION_RE.sub(_sub, answer), dropped
```

`return VerifiedAnswer(...)` 직전에 호출한다.

```python
    answer, dropped_citations = _rewrite_citations(answer, verified, raw_to_index)
    if dropped_citations:
        # 조용히 지우지 않는다 — 마커가 통째로 사라지는 회귀를 로그에서 볼 수 있어야 한다.
        # 원문은 남기지 않고 개수만 남긴다.
        logger.info("qa_citations_dropped", dropped=dropped_citations)
```

`schema.py`에 로거가 없으면 상단에 추가한다.

```python
from app.core.logging import get_logger

logger = get_logger(__name__)
```

- [ ] **Step 4: 통과를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_qa_schema.py -q`
예상: 전부 PASS (기존 테스트 포함).

- [ ] **Step 5: 커밋**

```bash
git add apps/api/app/services/qa/schema.py apps/api/tests/unit/test_qa_schema.py
git commit -m "feat: 답변 인용 마커를 검증된 근거로만 해석한다"
```

---

## Task 2: 프롬프트 계약 + 결정론 공급자

**파일**
- 수정: `apps/api/app/services/qa/provider.py`
- 테스트: `apps/api/tests/unit/test_qa_schema.py`

**인터페이스**
- 소비: Task 1의 `_CITATION_RE` 문법 `[c<n>]`
- 산출: `DeterministicQaProvider.answer()`가 마커가 박힌 `answer`를 낸다 (통합 테스트가 마커 경로를 타게 하려면 필요하다)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_qa_schema.py`에 새 클래스로 추가한다.

```python
class TestDeterministicProviderCitations:
    def test_answer_carries_markers_for_each_claim(self):
        req = QaRequest(
            question="심장은?",
            chunks=[
                QaContextChunk(chunk_id="c1", section_title="순환", text="심장은 혈액을 보낸다",
                               page_start=1, page_end=1),
                QaContextChunk(chunk_id="c2", section_title="호흡", text="폐는 산소를 교환한다",
                               page_start=2, page_end=2),
            ],
        )
        out = DeterministicQaProvider().answer(req)
        assert "[c0]" in out["answer"]
        assert "[c1]" in out["answer"]

    def test_not_found_answer_has_no_markers(self):
        out = DeterministicQaProvider().answer(QaRequest(question="x", chunks=[]))
        assert "[c" not in out["answer"]
```

- [ ] **Step 2: 실패를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_qa_schema.py::TestDeterministicProviderCitations -q`
예상: FAIL — `answer`에 마커가 없다.

- [ ] **Step 3: 구현한다**

`_SYSTEM_PROMPT`의 규칙 목록에 두 줄을 넣는다(`- page나 bbox를...` 바로 앞).

```python
    "- answer 산문에서 근거가 있는 문장 끝에 그 claim의 번호를 [c0], [c1] 형태로 붙인다. "
    "번호는 claims 배열의 순서(0부터)다.\n"
    "- 근거가 없는 문장에는 마커를 붙이지 않는다. claims에 없는 번호를 쓰지 않는다.\n"
```

`DeterministicQaProvider.answer()`의 `answer` 조립을 바꾼다.

```python
        claims = [{"text": t, "sourceChunkIds": [cid]} for t, cid in pairs]
        # 마커를 함께 낸다 — 통합 테스트가 실제 인용 경로를 타야 의미가 있다.
        answer = " ".join(f"{t}[c{i}]" for i, (t, _) in enumerate(pairs))[:ANSWER_MAX_CHARS]
```

- [ ] **Step 4: 통과를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_qa_schema.py -q`
예상: PASS.

- [ ] **Step 5: 커밋**

```bash
git add apps/api/app/services/qa/provider.py apps/api/tests/unit/test_qa_schema.py
git commit -m "feat: 모델에게 인용 마커 규칙을 준다"
```

---

## Task 3: 후속 질문 저장 (마이그레이션 0012) + 응답 노출

**파일**
- 생성: `apps/api/alembic/versions/0012_qa_followups.py`
- 수정: `apps/api/app/models/qa.py:88` 부근, `apps/api/app/services/qa/service.py:494-513`, `apps/api/app/api/routes/qa.py:55-98`
- 테스트: `apps/api/tests/integration/test_migrations.py:49`, `apps/api/tests/integration/test_system.py:65`, `apps/api/tests/integration/test_qa_api.py`

**인터페이스**
- 산출: `MessageOut.followups: list[str]` — Task 8의 프론트가 소비한다.

**주의:** `_finalize()`의 `if followups: assistant_msg.error_code = None` 은 후속 질문과 무관한 줄이다. 후속 질문이 있을 때만 오류 코드를 지우는 데엔 근거가 없다. 함께 걷어낸다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`apps/api/tests/integration/test_qa_api.py` 끝에 추가한다.

```python
class TestFollowupsReachTheClient:
    async def test_followups_are_persisted_and_returned(self, client, monkeypatch):
        # 결정론 공급자에 후속 질문을 실어 보낸다.
        from app.services.qa.provider import DeterministicQaProvider

        original = DeterministicQaProvider.answer

        def with_followups(self, request):
            out = original(self, request)
            out["followUpSuggestions"] = ["더 자세히?", "다른 예시는?"]
            return out

        monkeypatch.setattr(DeterministicQaProvider, "answer", with_followups)

        doc_id = await _ready_document(client)
        thread = (await client.post(f"/api/documents/{doc_id}/qa/threads")).json()["data"]["thread"]
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{thread['id']}/messages",
            json={"question": "이 문서의 핵심은?"},
        )
        detail = (
            await client.get(f"/api/documents/{doc_id}/qa/threads/{thread['id']}")
        ).json()["data"]
        assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
        assert assistant["followups"] == ["더 자세히?", "다른 예시는?"]

    async def test_messages_without_followups_return_empty_list(self, client):
        doc_id = await _ready_document(client)
        thread = (await client.post(f"/api/documents/{doc_id}/qa/threads")).json()["data"]["thread"]
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{thread['id']}/messages",
            json={"question": "이 문서의 핵심은?"},
        )
        detail = (
            await client.get(f"/api/documents/{doc_id}/qa/threads/{thread['id']}")
        ).json()["data"]
        assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
        assert assistant["followups"] == []
```

문서 준비 헬퍼(`_ready_document`)는 같은 파일의 기존 테스트가 쓰는 것을 그대로 재사용한다 — 새로 만들지 말고 파일 안에서 이름을 확인해 쓴다.

같은 커밋에서 리비전 기대값도 올린다.

```python
# tests/integration/test_migrations.py:49
        assert revision == "0012"
# tests/integration/test_system.py:65
        assert report["migrationRevision"] == "0012"
```

- [ ] **Step 2: 실패를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_qa_api.py -k Followups tests/integration/test_migrations.py -q`
예상: FAIL — `followups` 키 없음, 리비전 `0011`.

- [ ] **Step 3: 마이그레이션을 만든다**

`apps/api/alembic/versions/0012_qa_followups.py`

```python
"""후속 질문 저장 — 모델이 만든 제안을 버리지 않는다

verify()가 followUpSuggestions를 받아 _finalize()까지 넘겼지만 저장할 컬럼이 없어
그대로 버려졌다. 화면에 후속 질문 칩을 띄우려면 저장 경로가 필요하다.

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
```

- [ ] **Step 4: 모델·서비스·라우트를 고친다**

`app/models/qa.py` — `error_code` 아래에 추가한다.

```python
    # 모델이 제안한 후속 질문. 없으면 NULL — 기존 메시지는 백필하지 않는다.
    followups_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
```

`app/services/qa/service.py` `_finalize()` — 실제로 저장한다.

```python
    assistant_msg.completed_at = datetime.now(UTC)
    assistant_msg.followups_json = list(followups) if followups else None
```

기존의 `if followups: assistant_msg.error_code = None` 두 줄을 삭제한다.

`app/api/routes/qa.py` — `MessageOut`에 필드를 넣고 변환에서 채운다.

```python
class MessageOut(CamelModel):
    ...
    claims: list[ClaimOut]
    followups: list[str] = []
```

```python
def _message_out(m: QaMessage, claims: list[QaClaim]) -> MessageOut:
    return MessageOut(
        ...
        claims=[_claim_out(c) for c in claims],
        followups=list(m.followups_json or []),
    )
```

- [ ] **Step 5: 통과를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_qa_api.py tests/integration/test_migrations.py tests/integration/test_system.py -q`
예상: PASS.

- [ ] **Step 6: 커밋**

```bash
git add apps/api/alembic/versions/0012_qa_followups.py apps/api/app/models/qa.py apps/api/app/services/qa/service.py apps/api/app/api/routes/qa.py apps/api/tests/integration/
git commit -m "feat: 모델이 만든 후속 질문을 저장하고 응답에 싣는다"
```

---

## Task 4: 학습 수준(LearnerLevel)을 QA 경로로 통과시킨다

**파일**
- 수정: `apps/api/app/services/qa/provider.py`, `apps/api/app/services/qa/service.py`, `apps/api/app/services/qa/stream_service.py`, `apps/api/app/api/routes/qa.py`
- 테스트: `apps/api/tests/unit/test_qa_schema.py`

**인터페이스**
- 산출: 요청 바디 `{"question": "...", "learnerLevel": "concise|nursing_student|experienced_nurse"}`. 생략하면 `nursing_student`.

**저장하지 않는다.** 스레드·메시지에 남기면 마이그레이션이 또 생기는데 이 기능이 그 값을 치를 만큼 중요하지 않다. 재시도는 프론트가 현재 선택값을 다시 보낸다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
class TestLearnerLevelReachesThePrompt:
    def test_level_changes_the_user_prompt(self):
        from app.services.qa.provider import _build_user_prompt

        base = QaRequest(question="심장은?", chunks=[], learner_level="nursing_student")
        concise = QaRequest(question="심장은?", chunks=[], learner_level="concise")
        assert _build_user_prompt(base) != _build_user_prompt(concise)
        assert "간단" in _build_user_prompt(concise)

    def test_unknown_level_falls_back_to_default(self):
        from app.services.qa.provider import _build_user_prompt

        weird = QaRequest(question="심장은?", chunks=[], learner_level="wizard")
        default = QaRequest(question="심장은?", chunks=[])
        assert _build_user_prompt(weird) == _build_user_prompt(default)
```

- [ ] **Step 2: 실패를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_qa_schema.py::TestLearnerLevelReachesThePrompt -q`
예상: FAIL — `QaRequest`에 `learner_level`이 없다.

- [ ] **Step 3: 구현한다**

`provider.py`

```python
@dataclass
class QaRequest:
    question: str
    chunks: list[QaContextChunk]
    history: list[QaHistoryTurn] = field(default_factory=list)
    language: str = "ko"
    learner_level: str = "nursing_student"


# 요약 파이프라인과 같은 3단계를 쓴다. 알 수 없는 값은 기본값으로 떨어뜨린다 —
# 사용자 입력이 프롬프트 문구를 바꾸는 경로이므로 화이트리스트로만 받는다.
_LEVEL_HINTS = {
    "concise": "답변은 간단하게, 핵심만 짧게 쓴다.",
    "nursing_student": "간호학생이 이해할 수 있게 용어를 풀어 설명한다.",
    "experienced_nurse": "임상 경험이 있는 간호사 대상으로 배경 설명은 줄이고 핵심을 밀도 있게 쓴다.",
}
_DEFAULT_LEVEL = "nursing_student"
```

`_build_user_prompt()`의 마지막 지시문 앞에 넣는다.

```python
    parts.append(_LEVEL_HINTS.get(request.learner_level, _LEVEL_HINTS[_DEFAULT_LEVEL]))
```

`routes/qa.py`

```python
class QuestionIn(CamelModel):
    question: str = Field(min_length=1)
    learner_level: str = Field(
        default="nursing_student",
        pattern="^(concise|nursing_student|experienced_nurse)$",
    )
```

`post_message` / `stream_message`에서 값을 넘긴다.

```python
    await qa_service.ask(db, doc, user, thread, body.question, learner_level=body.learner_level)
```

```python
    user_msg, assistant_msg, request_id = await qa_stream.prepare_stream(
        db, doc, user, thread, body.question, learner_level=body.learner_level
    )
```

`service.ask()` · `stream_service.prepare_stream()` · `run_stream()`에 `learner_level: str = "nursing_student"` 키워드 인자를 추가하고, `QaRequest(...)`를 만드는 지점까지 그대로 전달한다. `run_stream`은 `prepare_stream`이 받은 값을 인자로 다시 받는다 — 저장하지 않으므로 DB에서 되읽을 수 없다.

**재시도 경로:** `retry_last()`도 같은 키워드 인자를 받게 하고, 라우트에서 `QuestionIn`과 동일한 바디를 선택적으로 받아 넘긴다. 바디가 없으면 기본값을 쓴다.

- [ ] **Step 4: 통과를 확인한다**

실행: `.\.venv\Scripts\python.exe -m pytest tests/unit tests/integration -q -k "qa"`
예상: PASS.

- [ ] **Step 5: 린트·타입 확인**

실행: `.\.venv\Scripts\python.exe -m ruff check . && .\.venv\Scripts\python.exe -m mypy app`
예상: ruff 통과. mypy는 `local_ai/system.py` 2건만(기존 이슈).

- [ ] **Step 6: 커밋**

```bash
git add apps/api/app/services/qa apps/api/app/api/routes/qa.py apps/api/tests/unit/test_qa_schema.py
git commit -m "feat: 질문에도 학습 수준을 적용한다"
```

---

## Task 5: 프론트 인용 토큰화 유틸

**파일**
- 생성: `apps/web/lib/citations.ts`
- 테스트: `apps/web/lib/__tests__/citations.test.ts`

**인터페이스**
- 산출:
  - `type CitationToken = { kind: "text"; text: string } | { kind: "citation"; claimIndex: number }`
  - `tokenizeCitations(content: string, claimCount: number): CitationToken[]`
  - `hasCitations(tokens: CitationToken[]): boolean`
  - `interface CitationSource { pageNumber: number; sectionTitle?: string | null; sourceMethod: "digital" | "ocr"; bbox: [number, number, number, number] }`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`apps/web/lib/__tests__/citations.test.ts`

```ts
import { describe, expect, it } from "vitest";
import { hasCitations, tokenizeCitations } from "@/lib/citations";

describe("tokenizeCitations", () => {
  it("splits text around a marker", () => {
    expect(tokenizeCitations("심장은 혈액을 보냅니다[c0].", 1)).toEqual([
      { kind: "text", text: "심장은 혈액을 보냅니다" },
      { kind: "citation", claimIndex: 0 },
      { kind: "text", text: "." },
    ]);
  });

  it("keeps consecutive markers separate", () => {
    const tokens = tokenizeCitations("근거가 둘입니다[c0][c1]", 2);
    expect(tokens.filter((t) => t.kind === "citation")).toHaveLength(2);
  });

  it("treats an out-of-range marker as plain text", () => {
    // 서버가 걸러주지만 프론트도 스스로를 지킨다 — 없는 근거로 이동하면 안 된다.
    expect(tokenizeCitations("x[c9]", 1)).toEqual([{ kind: "text", text: "x[c9]" }]);
  });

  it("leaves a truncated marker alone", () => {
    expect(tokenizeCitations("잘린 마커[c", 1)).toEqual([
      { kind: "text", text: "잘린 마커[c" },
    ]);
  });

  it("does not treat document brackets as citations", () => {
    expect(tokenizeCitations("표 [1]을 보라", 3)).toEqual([
      { kind: "text", text: "표 [1]을 보라" },
    ]);
  });

  it("reports no citations for plain prose", () => {
    expect(hasCitations(tokenizeCitations("마커 없는 답변", 2))).toBe(false);
  });

  it("reports citations when a valid marker exists", () => {
    expect(hasCitations(tokenizeCitations("답변[c0]", 1))).toBe(true);
  });
});
```

- [ ] **Step 2: 실패를 확인한다**

실행: `npm test -- citations`
예상: FAIL — 모듈 없음.

- [ ] **Step 3: 구현한다**

`apps/web/lib/citations.ts`

```ts
/** 답변 산문 안의 인용 마커를 렌더 가능한 토큰으로 쪼갠다.
 *
 * 서버(`verify()`)가 이미 유효하지 않은 마커를 지우지만, 프론트도 범위를 다시 확인한다.
 * 없는 근거를 가리키는 번호를 눌러 엉뚱한 페이지로 이동하는 것이 가장 나쁜 실패다.
 */
export type CitationToken =
  | { kind: "text"; text: string }
  | { kind: "citation"; claimIndex: number };

export interface CitationSource {
  pageNumber: number;
  sectionTitle?: string | null;
  sourceMethod: "digital" | "ocr";
  bbox: [number, number, number, number];
}

const CITATION_RE = /\[c(\d{1,2})\]/g;

export function tokenizeCitations(content: string, claimCount: number): CitationToken[] {
  const tokens: CitationToken[] = [];
  let cursor = 0;
  for (const match of content.matchAll(CITATION_RE)) {
    const index = Number(match[1]);
    if (index >= claimCount) continue; // 범위 밖 → 본문 글자로 남긴다
    const start = match.index ?? 0;
    if (start > cursor) tokens.push({ kind: "text", text: content.slice(cursor, start) });
    tokens.push({ kind: "citation", claimIndex: index });
    cursor = start + match[0].length;
  }
  if (cursor < content.length) tokens.push({ kind: "text", text: content.slice(cursor) });
  return tokens;
}

export function hasCitations(tokens: CitationToken[]): boolean {
  return tokens.some((t) => t.kind === "citation");
}
```

- [ ] **Step 4: 통과를 확인한다**

실행: `npm test -- citations`
예상: PASS.

- [ ] **Step 5: 커밋**

```bash
git add apps/web/lib/citations.ts apps/web/lib/__tests__/citations.test.ts
git commit -m "feat: 답변 인용 마커 토큰화 유틸"
```

---

## Task 6: 인용 pill · 출처 목록 공유 컴포넌트

**파일**
- 생성: `apps/web/components/citations.tsx`
- 테스트: `apps/web/components/__tests__/citations.test.tsx`

**인터페이스**
- 소비: Task 5의 `tokenizeCitations`, `CitationSource`
- 산출:
  - `<CitedText content={string} sources={CitationSource[]} onNavigate={(s: CitationSource) => void} />`
  - `<SourceList sources={CitationSource[]} onNavigate={(s: CitationSource) => void} />`

`sources[i]`가 `[ci]`에 대응한다. 호출자가 claim 또는 요약 아티팩트에서 이 배열을 만든다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CitedText, SourceList } from "@/components/citations";
import type { CitationSource } from "@/lib/citations";

const SOURCES: CitationSource[] = [
  { pageNumber: 3, sectionTitle: "순환계", sourceMethod: "digital", bbox: [0, 0, 1, 1] },
  { pageNumber: 7, sectionTitle: null, sourceMethod: "ocr", bbox: [1, 1, 2, 2] },
];

describe("CitedText", () => {
  it("renders a clickable number for each marker", () => {
    const onNavigate = vi.fn();
    render(<CitedText content="심장은 혈액을 보냅니다[c0]." sources={SOURCES} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: /3쪽/ }));
    expect(onNavigate).toHaveBeenCalledWith(SOURCES[0]);
  });

  it("renders prose unchanged when there is no marker", () => {
    render(<CitedText content="마커 없는 답변" sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByText("마커 없는 답변")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("SourceList", () => {
  it("labels a scanned source in plain Korean", () => {
    render(<SourceList sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByText(/스캔 인식/)).toBeInTheDocument();
    // 기술 용어는 화면에 내지 않는다
    expect(screen.queryByText(/OCR/i)).toBeNull();
  });

  it("shows the section title when present", () => {
    render(<SourceList sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByText(/순환계/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 실패를 확인한다**

실행: `npm test -- citations.test.tsx`
예상: FAIL — 모듈 없음.

- [ ] **Step 3: 구현한다**

`apps/web/components/citations.tsx`

```tsx
"use client";

import { type CitationSource, tokenizeCitations } from "@/lib/citations";

interface Props {
  content: string;
  sources: CitationSource[];
  onNavigate: (source: CitationSource) => void;
}

/** 답변 본문 — 마커 자리에 눌러서 원문으로 갈 수 있는 번호를 넣는다. */
export function CitedText({ content, sources, onNavigate }: Props) {
  const tokens = tokenizeCitations(content, sources.length);
  return (
    <p className="whitespace-pre-wrap text-[17px] leading-[1.7] text-slate-900">
      {tokens.map((t, i) =>
        t.kind === "text" ? (
          <span key={i}>{t.text}</span>
        ) : (
          <button
            key={i}
            type="button"
            onClick={() => onNavigate(sources[t.claimIndex])}
            aria-label={`${sources[t.claimIndex].pageNumber}쪽 근거 보기`}
            className="mx-0.5 rounded bg-blue-50 px-1.5 align-baseline text-xs font-medium text-blue-700 hover:bg-blue-100 focus:outline-2 focus:outline-offset-2 focus:outline-blue-600"
          >
            {t.claimIndex + 1}
          </button>
        ),
      )}
    </p>
  );
}

/** 본문 아래 출처 목록. 사용자가 그 근거를 얼마나 믿을지 판단할 수 있어야 한다. */
export function SourceList({ sources, onNavigate }: Omit<Props, "content">) {
  if (sources.length === 0) return null;
  return (
    <section className="mt-4 border-t border-slate-200 pt-3">
      <h3 className="mb-2 text-xs font-medium text-slate-500">출처</h3>
      <ol className="flex flex-col gap-1">
        {sources.map((s, i) => (
          <li key={i}>
            <button
              type="button"
              onClick={() => onNavigate(s)}
              className="w-full rounded px-1 py-1 text-left text-sm text-slate-700 hover:bg-slate-50 focus:outline-2 focus:outline-offset-2 focus:outline-blue-600"
            >
              <span className="mr-1.5 text-xs font-medium text-blue-700">{i + 1}</span>
              {s.pageNumber}쪽
              {s.sectionTitle ? ` · ${s.sectionTitle}` : ""}
              {s.sourceMethod === "ocr" ? " · 스캔 인식" : ""}
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}
```

- [ ] **Step 4: 통과를 확인한다**

실행: `npm test -- citations`
예상: PASS (유틸·컴포넌트 둘 다).

- [ ] **Step 5: 커밋**

```bash
git add apps/web/components/citations.tsx apps/web/components/__tests__/citations.test.tsx
git commit -m "feat: 인용 번호·출처 목록 공유 컴포넌트"
```

---

## Task 7: 질문 탭 답변 렌더 교체 (폴백 포함)

**파일**
- 수정: `apps/web/types/qa.ts`, `apps/web/components/document-qa.tsx:322-404`
- 테스트: `apps/web/components/__tests__/document-qa.test.tsx`

**인터페이스**
- 소비: Task 6의 `CitedText`, `SourceList`; Task 3의 `MessageOut.followups`
- 산출: `AssistantMessage`가 마커가 있으면 인용 렌더, 없으면 기존 claim 목록 렌더

**회귀 주의:** `components/__tests__/document-qa.test.tsx`가 이미 있다. 기존 테스트를 통과시킨 채로 바꾼다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

기존 `document-qa.test.tsx`에 추가한다. 이 파일에는 이미 `apiMock` · `streamMock` · `answerDetail` · `renderQa()`가 있다. **새 헬퍼를 만들지 말고 그것들을 쓴다** — `apiMock.getThread`가 돌려줄 `QaThreadDetail`을 갈아끼운 뒤 `renderQa()`를 부르는 방식이다.

```tsx
describe("답변 렌더", () => {
  it("마커가 있으면 인용 번호를 보여준다", async () => {
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m1", role: "assistant", content: "심장은 혈액을 보냅니다[c0].",
          status: "completed", sequenceNumber: 2, retrievalMode: "hybrid", followups: [],
          claims: [{
            text: "심장은 혈액을 보낸다", verificationStatus: "supported",
            sourceRefs: [{ pageNumber: 3, blockId: "b1", bbox: [0, 0, 1, 1],
                           readingOrder: 0, sourceMethod: "digital" }],
          }],
        },
      ],
    });
    renderQa();
    expect(await screen.findByRole("button", { name: /3쪽 근거 보기/ })).toBeInTheDocument();
  });

  it("마커가 없으면 기존 주장 목록으로 떨어진다", async () => {
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m2", role: "assistant", content: "심장은 혈액을 보냅니다.",
          status: "completed", sequenceNumber: 2, retrievalMode: "hybrid", followups: [],
          claims: [{
            text: "심장은 혈액을 보낸다", verificationStatus: "supported",
            sourceRefs: [{ pageNumber: 3, blockId: "b1", bbox: [0, 0, 1, 1],
                           readingOrder: 0, sourceMethod: "digital" }],
          }],
        },
      ],
    });
    renderQa();
    expect(await screen.findByText("심장은 혈액을 보낸다")).toBeInTheDocument();
  });
});
```

`apiMock` 안의 실제 키 이름(`getThread` 등)은 파일 상단 `vi.hoisted` 블록에서 확인해 맞춘다.

- [ ] **Step 2: 실패를 확인한다**

실행: `npm test -- document-qa`
예상: 첫 번째 테스트 FAIL.

- [ ] **Step 3: 타입을 넓힌다**

`apps/web/types/qa.ts`

```ts
export interface QaMessage {
  id: string;
  role: QaMessageRole;
  content: string;
  status: QaMessageStatus;
  sequenceNumber: number;
  retrievalMode: string | null;
  claims: QaClaim[];
  /** 모델이 제안한 다음 질문. 옛 메시지는 빈 배열이다. */
  followups: string[];
}
```

- [ ] **Step 4: `AssistantMessage`를 바꾼다**

`document-qa.tsx`의 `AssistantMessage`를 교체한다. `SourceBadges` / `ClaimSources` / `StreamSources`는 폴백과 스트리밍이 계속 쓰므로 **지우지 않는다.**

```tsx
function AssistantMessage({ message, onNavigate }: {
  message: QaMessage;
  onNavigate: (ref: NavigateRef) => void;
}) {
  const notice = statusNotice(message.status);
  // 확정 사실로 보여줄 근거 있는 주장만 출처를 표시한다
  const supportedClaims = message.claims.filter(
    (c) => c.verificationStatus === "supported" || c.verificationStatus === "conflicting",
  );
  // 인용 번호 i는 claims[i]에 대응한다 — 서버가 그렇게 다시 쓴다.
  const sources: CitationSource[] = message.claims.map((c) => ({
    pageNumber: c.sourceRefs[0]?.pageNumber ?? 1,
    sectionTitle: c.sourceRefs[0]?.sectionTitle ?? null,
    sourceMethod: c.sourceRefs[0]?.sourceMethod ?? "digital",
    bbox: c.sourceRefs[0]?.bbox ?? [0, 0, 0, 0],
  }));
  const cited = hasCitations(tokenizeCitations(message.content, sources.length));

  return (
    <div>
      {notice && <p className="mb-1 text-xs text-slate-500">{notice}</p>}
      {cited ? (
        <>
          <CitedText
            content={message.content}
            sources={sources}
            onNavigate={(s) => onNavigate({ pageNumber: s.pageNumber, bbox: s.bbox })}
          />
          <SourceList
            sources={sources}
            onNavigate={(s) => onNavigate({ pageNumber: s.pageNumber, bbox: s.bbox })}
          />
        </>
      ) : (
        <>
          {/* 마커 없는 옛 메시지 — 기존 렌더를 그대로 유지한다 */}
          <p className="whitespace-pre-wrap text-[17px] leading-[1.7] text-slate-900">
            {message.content}
          </p>
          {supportedClaims.length > 0 && (
            <ul className="mt-2 flex flex-col gap-1.5">
              {supportedClaims.map((c, i) => (
                <li key={i} className="border-t border-slate-200 pt-1.5">
                  <p className="text-sm leading-snug text-slate-600">{c.text}</p>
                  <ClaimSources claim={c} onNavigate={onNavigate} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
```

import를 추가한다.

```tsx
import { CitedText, SourceList } from "@/components/citations";
import { type CitationSource, hasCitations, tokenizeCitations } from "@/lib/citations";
```

- [ ] **Step 5: 통과를 확인한다**

실행: `npm test -- document-qa && npm run typecheck`
예상: PASS.

- [ ] **Step 6: 커밋**

```bash
git add apps/web/types/qa.ts apps/web/components/document-qa.tsx apps/web/components/__tests__/document-qa.test.tsx
git commit -m "feat: 답변 문장 안에서 근거를 짚는다"
```

---

## Task 8: 검색형 빈 상태 · 후속 질문 칩 · 학습 수준 칩

**파일**
- 수정: `apps/web/components/document-qa.tsx`, `apps/web/lib/api/qa.ts`, `apps/web/lib/api/qa-stream.ts`, `apps/web/hooks/use-qa-stream.ts`
- 테스트: `apps/web/components/__tests__/document-qa.test.tsx`

**인터페이스**
- 소비: Task 4의 요청 필드 `learnerLevel`, Task 3의 `followups`
- 산출: `ask(threadId, question, learnerLevel)` 시그니처

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```tsx
describe("질문 탭 진입", () => {
  it("대화가 없으면 예시 질문과 학습 수준을 보여준다", async () => {
    apiMock.getThread.mockResolvedValue({ thread, messages: [] });
    renderQa();
    expect(await screen.findByText("이 문서의 핵심 내용은 무엇인가요?")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "간호학생" })).toBeChecked();
  });

  it("후속 질문을 누르면 바로 전송한다", async () => {
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [{
        id: "m1", role: "assistant", content: "답변입니다.", status: "completed",
        sequenceNumber: 2, retrievalMode: "hybrid", claims: [],
        followups: ["더 자세히 알려줘"],
      }],
    });
    renderQa();
    await userEvent.click(await screen.findByRole("button", { name: "더 자세히 알려줘" }));
    // streamMock.ask가 그 질문으로 불렸는지 — 입력창을 거치지 않고 바로 나가야 한다.
    expect(streamMock.ask).toHaveBeenCalledWith(
      expect.anything(), "더 자세히 알려줘", expect.anything(),
    );
  });
});
```

- [ ] **Step 2: 실패를 확인한다**

실행: `npm test -- document-qa`
예상: FAIL.

- [ ] **Step 3: API 레이어에 수준을 태운다**

`lib/api/qa-stream.ts`의 요청 바디에 `learnerLevel`을 넣고, `hooks/use-qa-stream.ts`의 `ask(threadId, question)`을 `ask(threadId, question, learnerLevel)`로 넓힌다. `lib/api/qa.ts`의 `retryAnswer(documentId, threadId)`도 `learnerLevel`을 선택 인자로 받아 바디에 싣는다.

- [ ] **Step 4: 화면을 만든다**

`document-qa.tsx`에 상수와 상태를 추가한다.

```tsx
const LEVELS = [
  { key: "concise", label: "간단히" },
  { key: "nursing_student", label: "간호학생" },
  { key: "experienced_nurse", label: "경력간호사" },
] as const;

type LearnerLevel = (typeof LEVELS)[number]["key"];
const LEVEL_STORAGE_KEY = "medbridge.qa.learnerLevel";
```

```tsx
  const [level, setLevel] = useState<LearnerLevel>("nursing_student");

  // 마지막 선택만 브라우저에 기억한다 — 서버에 저장하지 않는다.
  useEffect(() => {
    const saved = window.localStorage.getItem(LEVEL_STORAGE_KEY);
    if (LEVELS.some((l) => l.key === saved)) setLevel(saved as LearnerLevel);
  }, []);

  function chooseLevel(next: LearnerLevel) {
    setLevel(next);
    window.localStorage.setItem(LEVEL_STORAGE_KEY, next);
  }
```

`submit()`이 `askQuestion(q)`를 부르도록 바꾸고, 후속 질문 칩이 같은 함수를 부른다.

```tsx
  async function askQuestion(q: string) {
    const question = q.trim();
    if (!question || busy) return;
    let threadId = effectiveThreadId;
    if (threadId === null) {
      const created = await createThread(doc.id);
      threadId = created.thread.id;
      setSelectedThreadId(threadId);
      void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
    }
    setPendingQuestion(question);
    setInput("");
    await stream.ask(threadId, question, level);
    setPendingQuestion(null);
    void queryClient.invalidateQueries({ queryKey: ["qa-thread", doc.id, threadId] });
    void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
  }
```

빈 상태를 검색 화면으로 바꾼다.

```tsx
        {messages.length === 0 && !busy && (
          <div className="flex flex-col items-center gap-4 px-2 py-8 text-center">
            <p className="text-lg font-semibold text-slate-900">이 문서에 대해 질문해 보세요.</p>
            <fieldset className="flex gap-1.5">
              <legend className="sr-only">학습 수준</legend>
              {LEVELS.map((l) => (
                <label
                  key={l.key}
                  className={`cursor-pointer rounded-full border px-3 py-1 text-xs ${
                    level === l.key
                      ? "border-blue-600 bg-blue-50 text-blue-700"
                      : "border-slate-200 text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <input
                    type="radio"
                    name="learner-level"
                    className="sr-only"
                    checked={level === l.key}
                    onChange={() => chooseLevel(l.key)}
                  />
                  {l.label}
                </label>
              ))}
            </fieldset>
            <ul className="flex w-full flex-col gap-2">
              {EXAMPLE_QUESTIONS.map((q) => (
                <li key={q}>
                  <button
                    type="button"
                    onClick={() => void askQuestion(q)}
                    className="w-full rounded-lg border border-slate-200 px-4 py-3 text-left text-sm hover:bg-slate-50"
                  >
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
```

마지막 assistant 메시지 아래에 후속 질문 칩을 붙인다.

```tsx
        {!busy && lastFollowups.length > 0 && (
          <ul className="mt-3 flex flex-wrap gap-1.5">
            {lastFollowups.map((q) => (
              <li key={q}>
                <button
                  type="button"
                  onClick={() => void askQuestion(q)}
                  className="rounded-full border border-slate-200 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
                >
                  {q}
                </button>
              </li>
            ))}
          </ul>
        )}
```

```tsx
  const lastFollowups =
    messages.filter((m) => m.role === "assistant").at(-1)?.followups ?? [];
```

- [ ] **Step 5: 통과를 확인한다**

실행: `npm test && npm run typecheck && npm run lint`
예상: PASS.

- [ ] **Step 6: 커밋**

```bash
git add apps/web/components/document-qa.tsx apps/web/lib/api apps/web/hooks apps/web/components/__tests__/document-qa.test.tsx
git commit -m "feat: 질문 탭을 검색 화면으로 바꾸고 후속 질문을 띄운다"
```

---

## Task 9: 기본 탭 전환 + 요약 탭에 같은 출처 패턴

**파일**
- 수정: `apps/web/app/documents/view/page.tsx:29`, `apps/web/components/summary-view.tsx`
- 테스트: `apps/web/components/__tests__/summary-view.test.tsx`

**인터페이스**
- 소비: Task 6의 `SourceList`, `CitationSource`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`summary-view.test.tsx`에 추가한다.

```tsx
it("아티팩트 출처를 공유 목록으로 보여준다", async () => {
  renderSummary({
    artifacts: [{
      artifactType: "overview", title: "개요", position: 0,
      content: { text: "이 문서는 순환계를 다룬다" },
      sourceRefs: [{ pageNumber: 5, blockId: "b1", bbox: [0, 0, 1, 1],
                     readingOrder: 0, sourceMethod: "ocr" }],
    }],
  });
  expect(await screen.findByText(/스캔 인식/)).toBeInTheDocument();
});
```

- [ ] **Step 2: 실패를 확인한다**

실행: `npm test -- summary-view`
예상: FAIL.

- [ ] **Step 3: 기본 탭을 바꾼다**

`app/documents/view/page.tsx` — `useState` 초기값을 고정값에서 문서 상태 기반으로 바꾼다.

```tsx
  const [mainTab, setMainTab] = useState<"preview" | "extraction" | "summary" | "qa" | null>(null);
```

`doc`이 확정된 뒤 유효 탭을 계산한다.

```tsx
  // 추출이 끝난 문서는 "무엇을 물어볼까"가 먼저 보이게 한다. 아직 처리 중이면
  // 질문할 대상이 없으므로 문서 보기로 연다.
  const effectiveTab = mainTab ?? (hasExtraction(doc.processingStatus) ? "qa" : "preview");
```

이하 `mainTab === "..."` 비교를 전부 `effectiveTab`으로 바꾼다. 탭 버튼의 `aria-selected`도 같다.

- [ ] **Step 4: 요약에 `SourceList`를 붙인다**

`summary-view.tsx`에서 아티팩트별 출처를 렌더하던 부분을 공유 컴포넌트로 교체한다.

```tsx
import { SourceList } from "@/components/citations";
import type { CitationSource } from "@/lib/citations";
```

```tsx
const sources: CitationSource[] = artifact.sourceRefs.map((r) => ({
  pageNumber: r.pageNumber,
  sectionTitle: null,
  sourceMethod: r.sourceMethod,
  bbox: r.bbox,
}));
```

```tsx
<SourceList sources={sources} onNavigate={(s) => navigate({ pageNumber: s.pageNumber, bbox: s.bbox })} />
```

- [ ] **Step 5: 통과를 확인한다**

실행: `npm test && npm run typecheck`
예상: PASS.

- [ ] **Step 6: 커밋**

```bash
git add apps/web/app/documents/view/page.tsx apps/web/components/summary-view.tsx apps/web/components/__tests__/summary-view.test.tsx
git commit -m "feat: 문서를 열면 질문이 먼저 보이고, 요약도 같은 출처 패턴을 쓴다"
```

---

## Task 10: 브랜드 마크 · 타이포 · DESIGN.md

**파일**
- 생성: `apps/web/components/brand-mark.tsx`
- 수정: `apps/web/app/layout.tsx:27-29`, `DESIGN.md`
- 테스트: 없음 (시각 변경). `npm run build`로 회귀만 확인한다.

- [ ] **Step 1: 마크 컴포넌트를 만든다**

`docs/brand/medbridge-logo-concept-1.svg`의 150px 아이콘 도형을 그대로 옮긴다. drop-shadow는 뺀다 — 헤더에서는 크기가 작아 흐릿해지기만 한다.

```tsx
/** 브랜드 마크 — DESIGN.md의 플랫 규칙에서 유일하게 그라디언트가 허용되는 자리다. */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 150 150" role="img" aria-label="MedBridge Study">
      <defs>
        <linearGradient id="mb-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#2563EB" />
          <stop offset="100%" stopColor="#0D9488" />
        </linearGradient>
      </defs>
      <rect width="150" height="150" rx="38" fill="url(#mb-mark)" />
      <rect x="31" y="32" width="20" height="84" rx="10" fill="#FFFFFF" />
      <rect x="99" y="32" width="20" height="84" rx="10" fill="#FFFFFF" />
      <path d="M41 74 C54 42,96 42,109 74" fill="none" stroke="#FFFFFF" strokeWidth="14" strokeLinecap="round" />
      <path d="M42 99 H108" fill="none" stroke="#CCFBF1" strokeWidth="11" strokeLinecap="round" />
      <rect x="70" y="70" width="10" height="34" rx="5" fill="#0F766E" />
      <rect x="58" y="82" width="34" height="10" rx="5" fill="#0F766E" />
    </svg>
  );
}
```

- [ ] **Step 2: 헤더를 교체한다**

`app/layout.tsx`

```tsx
              <Link href="/documents" className="flex items-center gap-2 text-lg font-bold text-slate-900">
                <BrandMark />
                MedBridge Study
              </Link>
```

`import { BrandMark } from "@/components/brand-mark";`를 추가한다.

- [ ] **Step 3: DESIGN.md에 예외를 적는다**

`## 4. Elevation`의 Named Rules 아래에 절을 추가한다.

```markdown
### Named Rules
**브랜드 마크 예외.** 그라디언트(Trust Blue #2563eb → Clinical Teal #0D9488)는 **앱
아이콘과 헤더 마크에만** 허용한다. UI 컴포넌트에는 여전히 금지다. `Clinical Teal`은
브랜드 색이며 상태 색·행동 색으로 쓰지 않는다 — 상태 4쌍과 Study Blue 규칙은 그대로다.
이 예외를 적어두지 않으면 "로고가 그라디언트니까 버튼도"로 번진다.
```

`## 3. Typography`의 Body 항목에서 "*현재값이며, 읽기 패널은 상향 대상이다*" 주석을 지우고 실제 값을 적는다.

```markdown
- **Reading** (400, 1.0625rem/17px, lh 1.7): 답변·요약 본문. 사용자가 '공부하는' 글.
- **Body** (400, 0.875rem/14px, lh 1.625): UI 안내문 전용.
```

- [ ] **Step 4: 읽기 폭을 제한한다**

설계 §8의 `max-width: 68ch`. 질문 탭 답변 패널은 440px 고정이라 이미 그보다 좁으므로 실효는 요약 탭에서 난다 — 한 줄이 너무 길면 눈이 다음 줄 첫머리를 놓친다.

`summary-view.tsx`의 본문 컨테이너에 `max-w-[68ch]`를 준다. 질문 탭 `CitedText`에도 같은 클래스를 넣어 패널이 넓어져도 규칙이 따라오게 한다.

```tsx
<p className="max-w-[68ch] whitespace-pre-wrap text-[17px] leading-[1.7] text-slate-900">
```

- [ ] **Step 5: 빌드를 확인한다**

실행: `npm run build && npm test`
예상: 성공.

- [ ] **Step 6: 커밋**

```bash
git add apps/web/components/brand-mark.tsx apps/web/app/layout.tsx apps/web/components DESIGN.md
git commit -m "feat: 새 브랜드 마크를 헤더에 올리고 읽기 폭·예외 범위를 못박는다"
```

---

## 실행 후 확인

전체 회귀:

```bash
cd apps/api && .\.venv\Scripts\python.exe -m pytest -q
cd apps/web && npm test && npm run typecheck && npm run lint && npm run build
```

관찰 항목 (설계 §12):
- `qa_citations_dropped` 로그가 자주 뜨면 프롬프트가 안 먹는 것이다.
- 답변에 마커가 아예 없어 폴백만 계속 타면 이 설계의 절반이 죽은 것이다. 실기기에서 몇 번 물어보고 확인한다.
