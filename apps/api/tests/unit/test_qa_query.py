"""검색어 전처리는 요청 표현만 제거하고 값·약어·부정 표현은 보존한다."""

import pytest

from app.services.qa.query import keyword_query


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("심부전의 정의를 설명하는 원문 한 문장과 출처를 보여주세요.", "심부전"),
        ("심장은 무엇인가요?", "심장"),
        ("당뇨병의 진단 기준은 무엇인가요?", "당뇨병"),
        ("심장의 구조적 또는 기능적 이상", "심장 구조적 또는 기능적 이상"),
        ("심장 혈압 간암", "심장 혈압 간암"),
        ("비타민 D 0.5 mg/dL IL-6 B12", "비타민 D 0.5 mg/dL IL-6 B12"),
        ("심부전 아닌 호흡부전", "심부전 아닌 호흡부전"),
        ("신경과 정의", "신경과"),
        ("Heart failure", "Heart failure"),
        ("정의 의미", "정의 의미"),
        ("심부전 심부전의", "심부전"),
        ("심부전의 분류에 대해 설명해주세요.", "심부전"),
        ("심부전의 분류에 관한 원문", "심부전"),
        ("이 문서의 원문 한 문장과 출처를 보여주세요.", ""),
        ("?!", ""),
        ("", ""),
    ],
)
def test_keyword_query(question, expected):
    assert keyword_query(question) == expected
