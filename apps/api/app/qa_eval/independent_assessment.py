"""독립 묶음 선택 후 전체 상충/불확실성 가드. 평가 전용이며 의미 검증은 아니다."""

from collections.abc import Callable

Assessment = tuple[str, set[int]]


def independent_assessment(payload: dict, assess: Callable[[dict, int], Assessment]) -> Assessment:
    bundles = payload["bundles"]
    if len(bundles) == 1:
        return assess(payload, 1)
    selected: set[int] = set()
    statuses = []
    for index, bundle in enumerate(bundles):
        # 판단 스키마의 지역 번호만 0으로 맞춘다. 원문·출처 위치·질문·이력은 유지한다.
        single = {**payload, "bundles": [{**bundle, "bundleIndex": 0}]}
        status, indices = assess(single, 1)
        statuses.append(status)
        if indices:
            selected.add(index)
    # 개별 묶음만 보면 서로 다른 묶음의 사실 상충을 발견할 수 없다.
    # 원래 전체 판단을 가드로 유지하되 정상 선택 집합은 개별 판단에서 얻는다.
    global_status, global_selected = assess(payload, len(bundles))
    statuses.append(global_status)
    if "conflicting_evidence" in statuses:
        return "conflicting_evidence", selected | global_selected
    if "insufficient_evidence" in statuses or bool(selected) != bool(global_selected):
        return "insufficient_evidence", set()
    return ("selected", selected) if selected else ("not_found", set())
