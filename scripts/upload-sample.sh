#!/usr/bin/env bash
# 샘플 PDF 업로드 E2E 검증 스크립트 (스택이 기동돼 있어야 한다: make up)
set -euo pipefail

API="${API_URL:-http://localhost:8000}"
JAR="$(mktemp)"
EMAIL="sample-$(date +%s)@example.com"

echo "1) 회원가입"
curl -sf -c "$JAR" -X POST "$API/api/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"sample-pass-123\",\"displayName\":\"샘플 사용자\"}" > /dev/null

echo "2) 샘플 PDF 생성"
PDF="$(mktemp -t sample).pdf"
python3 - "$PDF" <<'EOF'
import sys
# 최소 유효 1페이지 PDF
body = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj
trailer << /Root 1 0 R >>
%%EOF
"""
open(sys.argv[1], "wb").write(body)
EOF

echo "3) 업로드"
DOC_ID=$(curl -sf -b "$JAR" -X POST "$API/api/documents" \
  -F "file=@$PDF;type=application/pdf" -F "title=샘플 문서" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["id"])')
echo "   document id: $DOC_ID"

echo "4) 검증 완료 대기"
for _ in $(seq 1 30); do
  STATUS=$(curl -sf -b "$JAR" "$API/api/documents/$DOC_ID" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["processingStatus"])')
  echo "   status: $STATUS"
  [ "$STATUS" = "ready" ] && break
  [ "$STATUS" = "failed" ] && { echo "검증 실패"; exit 1; }
  sleep 1
done
[ "$STATUS" = "ready" ] || { echo "시간 초과"; exit 1; }

echo "5) presigned URL 확인"
curl -sf -b "$JAR" "$API/api/documents/$DOC_ID/download-url" \
  | python3 -c 'import json,sys;print("   url ok:", json.load(sys.stdin)["data"]["url"][:80], "...")'

echo "OK: 업로드 → 검증(ready) → 미리보기 URL 전체 흐름 통과"
