"""CI 실행 요약(ci_summary) 회귀 테스트.

repo 루트의 scripts/ci_summary.py를 경로로 로드해 순수 렌더러를 검증한다.
요약이 조용히 거짓말을 하면(실패를 성공처럼, 표가 깨져 내용이 사라짐) 로그가 없는
상황에서 유일한 단서가 무용지물이 되므로, 그 경로들을 여기서 못박는다.
"""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[4] / "scripts" / "ci_summary.py"
_spec = importlib.util.spec_from_file_location("ci_summary", _PATH)
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


class TestParseRows:
    def test_name_outcome_detail(self):
        assert ci.parse_rows("테스트|success|622 passed") == [
            ("테스트", "success", "622 passed")
        ]

    def test_detail_is_optional(self):
        assert ci.parse_rows("Lint|failure") == [("Lint", "failure", "")]

    def test_blank_lines_and_nameless_rows_are_skipped(self):
        assert ci.parse_rows("\n  \n|success|무명\nLint|success") == [
            ("Lint", "success", "")
        ]

    def test_detail_may_contain_pipes(self):
        """비고에 파이프가 있어도 이름·결과 파싱이 밀리지 않는다."""
        assert ci.parse_rows("빌드|success|a|b") == [("빌드", "success", "a|b")]


class TestOverall:
    def test_failure_wins_over_success(self):
        assert ci.overall(["success", "failure", "success"]) == "failure"

    def test_cancelled_beats_skipped_and_success(self):
        assert ci.overall(["success", "skipped", "cancelled"]) == "cancelled"

    def test_all_success(self):
        assert ci.overall(["success", "success"]) == "success"

    def test_empty_is_success(self):
        assert ci.overall([]) == "success"

    def test_unknown_outcome_is_not_swallowed(self):
        """모르는 값을 성공으로 접으면 요약이 통과했다고 거짓말한다."""
        assert ci.overall(["success", "장애"]) == "장애"


class TestRender:
    def test_section_verdict_reflects_the_worst_step(self):
        out = ci.render("백엔드", [("Lint", "success", ""), ("테스트", "failure", "3 failed")])
        assert out.splitlines()[0] == "### ❌ 백엔드"
        assert "| 테스트 | ❌ | 3 failed |" in out

    def test_unknown_outcome_renders_as_unknown_not_success(self):
        out = ci.render("백엔드", [("테스트", "", "")])
        assert "❔" in out
        assert "✅" not in out

    def test_pipe_in_detail_does_not_break_the_table(self):
        import re

        out = ci.render("빌드", [("설치 파일", "success", "a|b")])
        row = next(line for line in out.splitlines() if line.startswith("| 설치 파일"))
        # 이스케이프되지 않은 파이프만 열 구분자다 — 열 3개면 4개여야 한다
        assert len(re.findall(r"(?<!\\)\|", row)) == 4
        assert r"a\|b" in row

    def test_multiline_detail_is_flattened(self):
        out = ci.render("백엔드", [("테스트", "failure", "첫 줄\n둘째 줄")])
        assert "첫 줄 둘째 줄" in out
        assert len([line for line in out.splitlines() if line.startswith("| 테스트")]) == 1

    def test_long_detail_is_truncated(self):
        out = ci.render("백엔드", [("테스트", "failure", "가" * 500)])
        row = next(line for line in out.splitlines() if line.startswith("| 테스트"))
        assert len(row) < 300
        assert "…" in row

    def test_no_rows_says_so_instead_of_pretending_to_pass(self):
        out = ci.render("백엔드", [])
        assert "기록된 항목이 없습니다" in out

    def test_first_column_can_be_renamed(self):
        """종합 표는 단계가 아니라 잡을 나열한다 — 머리글이 맞아야 한다."""
        out = ci.render("전체 결과", [("백엔드", "success", "")], column="잡")
        assert "| 잡 | 결과 | 비고 |" in out


class TestMain:
    def test_appends_to_step_summary_file(self, tmp_path, monkeypatch):
        target = tmp_path / "summary.md"
        target.write_text("기존 내용\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
        monkeypatch.setenv("CI_SUMMARY_TITLE", "백엔드")
        monkeypatch.setenv("CI_SUMMARY_ROWS", "테스트|success|622 passed")

        assert ci.main() == 0

        written = target.read_text(encoding="utf-8")
        assert written.startswith("기존 내용\n")  # 다른 잡의 요약을 덮지 않는다
        assert "### ✅ 백엔드" in written
        assert "622 passed" in written

    def test_falls_back_to_stdout_without_the_env(self, capsys, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.setenv("CI_SUMMARY_TITLE", "로컬")
        monkeypatch.setenv("CI_SUMMARY_ROWS", "Lint|success")

        assert ci.main() == 0
        assert "### ✅ 로컬" in capsys.readouterr().out

    def test_stdout_without_reconfigure_still_works(self, monkeypatch):
        """아이콘은 비-UTF-8 콘솔에서 UnicodeEncodeError를 낸다(실측: Windows cp949).

        reconfigure가 없는 스트림에서 예외로 죽으면 요약을 만들다가 잡을 실패시킨다.
        """
        import io

        class _NoReconfigure(io.StringIO):
            reconfigure = None  # 속성 접근 시 호출 불가 → AttributeError 경로

            def __getattribute__(self, name):
                if name == "reconfigure":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        stream = _NoReconfigure()
        monkeypatch.setattr(ci.sys, "stdout", stream)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.setenv("CI_SUMMARY_TITLE", "로컬")
        monkeypatch.setenv("CI_SUMMARY_ROWS", "Lint|success")

        assert ci.main() == 0
        assert "### ✅ 로컬" in stream.getvalue()
