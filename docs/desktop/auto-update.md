# 자동 업데이트

## 흐름

```text
앱 실행 → 1회 자동 확인 (설정 화면에서 수동 확인도 가능)
→ 새 버전 있음 → "새 업데이트가 있습니다" + 버전·릴리스 노트 + [나중에][업데이트]
→ [업데이트] 클릭
   1. POST /api/system/prepare-update
      - 새 문서 작업 차단 → 실행 중 작업 완료 대기
      - SQLite WAL checkpoint(TRUNCATE) → backups/pre-update-{버전}-{시각}.db
   2. updater 다운로드 (진행률 % 표시, "MedBridge를 종료하지 마세요")
   3. 서명 검증 후 passive 설치 → 앱 재시작
   4. 새 sidecar가 자동 마이그레이션(사전 백업 포함) → health → 메인 화면
→ [나중에] 클릭 → POST /api/system/resume (작업 차단 해제)
```

실패 시: "업데이트를 완료하지 못했습니다 / 기존 학습자료는 그대로 보관되어 있습니다"
+ [다시 시도][나중에][오류 정보 저장]. 사용자 DB·PDF는 어떤 실패 경로에서도 삭제되지 않는다.

## 구성

- 플러그인: `tauri-plugin-updater` + `tauri-plugin-process`(재시작)
- endpoint: `https://github.com/chorok446/medbridge-releases/releases/latest/download/latest.json`
- 서명: updater 개인키는 GitHub Secrets에만 존재 (`TAURI_SIGNING_PRIVATE_KEY`),
  공개키는 릴리스 빌드 때 tauri.conf에 주입 — 잘못된 서명 패키지는 설치 거부됨
- GUI 컴포넌트: `apps/web/components/update-manager.tsx`
  (기술 오류·URL·서명값은 사용자에게 표시하지 않음)

## Windows 실기기 검증 체크리스트 (완료 게이트)

0.1.0 설치 → PDF 추가 → 0.1.1 릴리스 → 앱에서 감지 → GUI 업데이트 → 재시작 →
버전 0.1.1 확인 → 기존 PDF·DB 유지 확인. 이 시나리오가 통과해야 Sprint 1.5 완료.
