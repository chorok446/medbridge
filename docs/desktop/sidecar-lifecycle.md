# Sidecar 생명주기

## 시작

```text
Tauri 시작
→ 빈 포트 선택(127.0.0.1:0) + 실행별 토큰(UUID) 생성
→ 앱 데이터 디렉터리 생성
→ sidecar 스폰 (MEDBRIDGE_APP_DATA_DIR / MEDBRIDGE_API_TOKEN / MEDBRIDGE_BOUND_PORT / APP_ENV 주입)
→ [sidecar] runtime/sidecar.json으로 중복 실행 검사 (stale pid 파일은 정리)
→ [sidecar] 마이그레이션 필요 시 pre-migration 백업 → alembic upgrade head
→ [sidecar] 중단 작업 복구 → /health 응답
→ [Tauri 백그라운드 스레드] /health 검증 성공 → sidecar_status = ready
→ GUI StartupGate가 ready를 확인하고 메인 화면 표시
```

- 마이그레이션 실패 → sidecar 기동 실패 → status=failed → GUI가 "시작하지 못했습니다"
  화면(다시 시작 / 오류 정보 저장)을 표시. 정상 화면은 절대 표시하지 않는다.
- production(패키징)에서 토큰이 없으면 sidecar가 기동을 거부한다 (fail-closed).

## 보안

127.0.0.1 바인딩 · 실행별 토큰(`X-MedBridge-Token`) · query 토큰은 `/file` 경로만 ·
토큰은 로그에 남기지 않음 · 포트/토큰/경로는 GUI 미표시.

## 종료

- 창 파괴·앱 종료(RunEvent::Exit) 모두에서 자식 프로세스 kill+wait
- sidecar는 종료 시 runtime/sidecar.json 제거
- 남은 한계: 셸 강제 종료(작업관리자 kill -9) 시 고아 가능 —
  Windows Job Object(kill-on-close) 도입 예정 (후속 Windows 하드닝, stale 파일 정리가
  다음 실행에서 잔재를 청소한다)

## 재시작 복구

queued/validating → 재검증 큐 재등록, uploaded → 재큐잉,
created/uploading → "다시 업로드해 주세요" 실패 확정.
