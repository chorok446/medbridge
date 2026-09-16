#!/usr/bin/env python3
"""GitHub Actions 실행 요약(Summary 탭) 작성 — 어느 단계가 무엇으로 끝났는지 표로 남긴다.

기본 실행 화면은 잡의 성패만 보여주고, 무엇이 몇 개 통과했는지·어느 단계에서 멈췄는지는
로그를 열어야 알 수 있다. 러너가 통신 두절로 죽어 **로그가 통째로 사라진** 적이 있어
(Tests 단계 45분 정지 후 log not found) 요약만으로도 범위를 좁힐 수 있어야 한다.

입력은 **환경변수로만** 받는다. 단계 출력(테스트 요약 줄, 파일명 등)을 워크플로에서
셸 인자로 보간하면 그 안의 문자가 명령으로 해석될 수 있다 — fork PR이 테스트 이름으로
주입할 수 있는 실제 경로다. env로 넘기면 셸이 값을 다시 파싱하지 않는다.

  CI_SUMMARY_TITLE    섹션 제목
  CI_SUMMARY_ROWS     줄마다 "이름|결과|비고" (비고는 생략 가능)
  CI_SUMMARY_COLUMN   첫 열 이름(기본 "단계"; 종합 표에서는 "잡")
  CI_SUMMARY_ICON     잡을 가리키는 고유 기호(선택). 판정 아이콘 앞에 붙는다
  GITHUB_STEP_SUMMARY 출력 파일 경로(없으면 표준출력)

결과 값은 Actions의 outcome/result 어휘를 그대로 쓴다(success/failure/cancelled/skipped).
"""

from __future__ import annotations

import os
import sys

# Actions의 outcome/result 어휘 → 표시 아이콘. 모르는 값은 삼키지 않고 ❔로 드러낸다
# (조용히 성공처럼 보이면 요약이 거짓말을 한다).
_ICONS = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "⛔",
    "skipped": "⏭️",
}
_UNKNOWN_ICON = "❔"

# 실패가 하나라도 있으면 실패다. 아래 순서대로 먼저 걸리는 값이 섹션 제목의 아이콘이 된다.
_SEVERITY = ("failure", "cancelled", "skipped", "success")

# 비고는 한 줄 요약이다. 모델·테스트 출력이 길면 표가 무너지므로 자른다.
_DETAIL_MAX = 200


def icon_for(outcome: str) -> str:
    return _ICONS.get(outcome.strip().lower(), _UNKNOWN_ICON)


def _cell(value: str) -> str:
    """표 한 칸으로 안전하게 만든다 — 줄바꿈·제어문자 제거, 파이프 이스케이프, 길이 제한."""
    flat = " ".join(value.split())  # 줄바꿈·탭·연속 공백을 하나로
    flat = "".join(c for c in flat if c.isprintable())
    flat = flat.replace("|", r"\|")
    if len(flat) > _DETAIL_MAX:
        flat = flat[: _DETAIL_MAX - 1] + "…"
    return flat


def parse_rows(raw: str) -> list[tuple[str, str, str]]:
    """"이름|결과|비고" 줄들을 파싱한다. 빈 줄은 건너뛴다."""
    rows: list[tuple[str, str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        name = parts[0].strip()
        outcome = parts[1].strip() if len(parts) > 1 else ""
        detail = "|".join(parts[2:]).strip() if len(parts) > 2 else ""
        if not name:
            continue
        rows.append((name, outcome, detail))
    return rows


def overall(outcomes: list[str]) -> str:
    """섹션 전체 판정 — 가장 나쁜 결과를 택한다."""
    normalized = [o.strip().lower() for o in outcomes]
    # 빈 값·모르는 값은 성공으로 접지 않는다. 섹션 제목만 ✅인데 안의 줄은 ❔면
    # 훑어보는 사람은 통과했다고 읽는다.
    unknown = [o for o in normalized if o not in _ICONS]
    if unknown:
        return unknown[0]
    for level in _SEVERITY:
        if level in normalized:
            return level
    return "success"


def render(
    title: str,
    rows: list[tuple[str, str, str]],
    *,
    column: str = "단계",
    icon: str = "",
) -> str:
    """마크다운 섹션 문자열. 순수 함수 — 테스트가 파일·환경 없이 검증한다.

    `column`은 첫 열 이름이다 — 잡 안에서는 '단계', 종합 표에서는 '잡'을 가리킨다.
    `icon`은 잡을 가리키는 고유 기호다. 실행 화면에서는 섹션이 세로로 이어 붙는데
    제목이 전부 같은 판정 아이콘으로 시작하면 어느 잡인지 훑어서 구분되지 않는다.
    """
    verdict = overall([outcome for _name, outcome, _detail in rows])
    heading = " ".join(
        part for part in (_cell(icon), icon_for(verdict), _cell(title) or "(제목 없음)") if part
    )
    lines = [f"### {heading}", ""]
    if not rows:
        lines.append("_기록된 항목이 없습니다._")
        return "\n".join(lines) + "\n"
    lines += [f"| {_cell(column) or '항목'} | 결과 | 비고 |", "| --- | :---: | --- |"]
    for name, outcome, detail in rows:
        lines.append(f"| {_cell(name)} | {icon_for(outcome)} | {_cell(detail)} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    title = os.environ.get("CI_SUMMARY_TITLE", "").strip()
    rows = parse_rows(os.environ.get("CI_SUMMARY_ROWS", ""))
    column = os.environ.get("CI_SUMMARY_COLUMN", "단계").strip() or "단계"
    icon = os.environ.get("CI_SUMMARY_ICON", "").strip()
    section = render(title, rows, column=column, icon=icon)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(section + "\n")
    else:
        # 아이콘이 비-UTF-8 콘솔(예: Windows cp949)에서 UnicodeEncodeError로 죽는다.
        # 요약을 만들다가 잡을 실패시키면 본말이 전도된다 — 가능하면 UTF-8로 바꾸고,
        # 바꿀 수 없는 스트림(테스트 캡처 등)이면 그대로 쓴다.
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            pass
        sys.stdout.write(section)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
