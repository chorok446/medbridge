# 로컬 데이터

## 위치와 구조

```text
%LOCALAPPDATA%\MedBridge\          (macOS 개발: ~/Library/Application Support/MedBridge)
├─ medbridge.db (+ -wal/-shm)      SQLite, WAL 모드, FK 강제
├─ documents\{document_id}\original.pdf
├─ cache\
├─ logs\sidecar.log                구조화 로그 (개인정보·토큰 없음)
├─ backups\pre-migration-*.db      마이그레이션 전 자동 백업
│          pre-update-*.db         업데이트 전 백업 (checkpoint 후)
└─ runtime\sidecar.json            실행 중 sidecar {pid, port} (중복 실행 방지)
```

## 보장 사항 (테스트로 강제)

- 저장소·설치 디렉터리·작업 디렉터리에 사용자 데이터를 만들지 않는다
- 파일 경로에 원본 파일명을 쓰지 않는다 (UUID만)
- 경로는 앱 데이터 루트 밖으로 나갈 수 없다 (경로 순회 방지)
- 저장은 임시 파일 + 원자적 이동, 실패 시 임시 파일 정리
- 앱 재실행 후 DB·PDF 유지
- 업데이트·재설치는 이 디렉터리를 건드리지 않는다 (NSIS는 설치 디렉터리만 교체)
- 프로그램 제거 시에도 사용자 데이터는 자동 삭제하지 않는다
  ("프로그램만 제거 / 데이터까지 제거" 선택 GUI는 백업·복원 기능과 함께 후속 제공)
