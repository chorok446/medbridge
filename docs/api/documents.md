# 문서 API (sidecar 내부 API)

GUI ↔ sidecar 전용. 127.0.0.1 + 토큰(`X-MedBridge-Token`, 파일 엔드포인트는 `?token=`)으로
보호되며 외부에 노출되지 않는다.

모든 응답은 envelope 형식:

```json
{ "data": { ... }, "meta": { "correlationId": "uuid" } }
{ "error": { "code": "ENCRYPTED_PDF", "message": "…", "retryable": false, "details": null },
  "meta": { "correlationId": "uuid" } }
```

## 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health` | sidecar 상태 + 마이그레이션 리비전 (Tauri 셸 전용) |
| GET/PATCH | `/api/profile` | 앱 설정 (표시 이름·학습 수준) |
| POST | `/api/documents` | PDF multipart 업로드 → 즉시 문서 ID·상태 반환 |
| POST | `/api/documents/stream` | GUI용 raw PDF 스트리밍 업로드(이중 임시파일 방지) |
| GET | `/api/documents` | 목록 (status·search·cursor·limit) |
| GET | `/api/documents/{id}` | 상세·처리 상태 |
| GET | `/api/documents/{id}/file` | PDF 원문 (미리보기) |
| PATCH | `/api/documents/{id}` | 이름 변경 `{title}` |
| DELETE | `/api/documents/{id}` | 삭제 (파일+기록, soft delete) |
| POST | `/api/documents/{id}/retry` | 실패 문서 재검증 |
| GET | `/api/documents/{id}/jobs` | 작업 이력 (GUI에선 접힌 영역) |
| POST | `/api/reports` | 사용자 오류 신고 (구조화 로그로 기록) |

## 오류 코드

`FILE_TOO_LARGE(413)` `INVALID_FILE_TYPE(415)` `INVALID_PDF_SIGNATURE` `ENCRYPTED_PDF`
`CORRUPTED_PDF` `EMPTY_PDF` `STORAGE_UPLOAD_FAILED(502·retryable)` `STORAGE_DELETE_FAILED`
`DUPLICATE_DOCUMENT`(응답은 200 + `duplicate: true`) `QUEUE_ENQUEUE_FAILED` `VALIDATION_FAILED`
`NOT_FOUND(404)` `UNAUTHORIZED(401)` `INVALID_STATE(409)` `INTERNAL_ERROR(500)`

내부 예외 메시지·스택은 응답에 포함하지 않는다. GUI는 코드를 한국어 안내로 변환한다
(`apps/web/lib/format.ts`의 `FAILURE_GUIDES`).
