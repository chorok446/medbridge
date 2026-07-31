# 시스템 개요 (Sprint 1, 데스크톱)

## 아키텍처

```text
Tauri 2 Desktop App (Windows x64 배포 / macOS 개발 실행)
├─ GUI: Next.js 정적 내보내기 (React + TypeScript, 웹뷰에 포함)
├─ Sidecar: FastAPI (PyInstaller 단일 실행 파일, 동적 포트 + 토큰)
│   ├─ SQLite (aiosqlite, WAL, FK 강제) — Alembic 자동 마이그레이션
│   ├─ LocalFileStorage — OS 앱 데이터 경로에 원자적 저장
│   └─ LocalTaskRunner — asyncio 인프로세스 작업 (동시 2개 제한)
└─ 앱 데이터: %LOCALAPPDATA%\MedBridge  (medbridge.db, documents/, backups/, logs/, cache/)
```

외부 인프라(PostgreSQL·Redis·MinIO·Docker) 없음. 최종 사용자는 설치 파일만 실행한다.

## 기동 흐름

1. Tauri가 빈 포트 선택 + 토큰(UUID) 생성
2. sidecar 실행: `MEDBRIDGE_APP_DATA_DIR`(OS 앱 데이터 경로)·`MEDBRIDGE_API_TOKEN` 주입,
   127.0.0.1 바인딩 (외부 연결 불가, 토큰 없는 요청 401)
3. sidecar lifespan: 디렉터리 생성 → 리비전 다르면 DB 백업 → `alembic upgrade head`
   (실패 시 앱은 정상 상태로 기동되지 않음) → 중단 작업 복구
4. GUI는 `invoke("sidecar_info")`로 주소·토큰을 받아 fetch (화면에 노출 금지)
5. 창 종료 시 Tauri가 sidecar를 종료

## 업로드 데이터 흐름

```text
GUI 파일 선택(개인정보 확인 체크 필수)
→ POST /api/documents  크기 제한 스트리밍 → PDF 시그니처 → SHA-256 → 중복 검사
→ documents 행 생성(created→uploading) → 임시파일 + os.replace 원자적 저장(→uploaded)
→ document_jobs 생성(→queued) → LocalTaskRunner.enqueue
→ [비동기] validate: 암호화·손상·페이지 수·해시 일치 → ready | failed(코드+한국어 안내)
→ GUI 폴링으로 상태 표시, ready 후 GET /api/documents/{id}/file 로 미리보기
```

보상 처리: 저장 실패→failed+키 비움 / DB 실패→객체 삭제 시도 / 큐 실패→failed(재시도 가능)
/ 삭제 중 파일 삭제 실패→deleting 유지.

## 상태 머신

`created → uploading → uploaded → queued → validating → ready`,
실패 시 `failed`(→`queued` 재시도), 삭제 `ready|failed → deleting → deleted`.
전이는 `state_machine.transition()` 한 곳에서만 수행.

## 단일 사용자

로그인 없음. `app_profile` 1행이 로컬 프로필이며 모든 문서의 소유자다.
클라이언트가 보낸 user_id는 신뢰하지 않는다(테스트로 강제). 서버 모드가 필요해지면
`api/deps.get_current_user` 한 지점만 교체한다.

## 향후 데스크톱 확장에서 교체·추가될 것

- Sprint 1.5: updater 서명 키 발급, 백업·복원 GUI, Windows 실기기 검증
- Sprint 2: PDF 텍스트 추출·OCR·개인정보 탐지 (LocalTaskRunner에 작업 추가)
- PDF.js 커스텀 뷰어 (페이지 좌표 이동·하이라이트)
