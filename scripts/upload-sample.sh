#!/usr/bin/env bash
# 샘플 PDF 업로드 전체 흐름 검증 (sidecar가 떠 있어야 한다: make dev-api)
set -euo pipefail

API="${API_URL:-http://127.0.0.1:8765}"

echo "1) sidecar 상태 확인"
curl -sf "$API/health" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   ok, revision:", d["migrationRevision"])'

echo "2) 샘플 PDF 생성"
PDF="$(mktemp -t sample).pdf"
uv run --project "$(dirname "$0")/../apps/api" python - "$PDF" <<'EOF'
import sys
from pypdf import PdfWriter
w = PdfWriter()
w.add_blank_page(width=612, height=792)
w.add_blank_page(width=612, height=792)
with open(sys.argv[1], "wb") as f:
    w.write(f)
EOF

echo "3) 업로드"
DOC_ID=$(curl -sf -X POST "$API/api/documents" \
  -F "file=@$PDF;type=application/pdf" -F "title=샘플 문서" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["id"])')
echo "   document id: $DOC_ID"

echo "4) 검증 완료 대기"
STATUS=""
for _ in $(seq 1 30); do
  STATUS=$(curl -sf "$API/api/documents/$DOC_ID" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["processingStatus"])')
  echo "   status: $STATUS"
  [ "$STATUS" = "ready" ] && break
  [ "$STATUS" = "failed" ] && { echo "검증 실패"; exit 1; }
  sleep 1
done
[ "$STATUS" = "ready" ] || { echo "시간 초과"; exit 1; }

echo "5) PDF 파일 응답 확인"
curl -sf "$API/api/documents/$DOC_ID/file" -o /dev/null -w "   content-type ok: %{content_type}\n"

echo "6) 삭제"
curl -sf -X DELETE "$API/api/documents/$DOC_ID" \
  | python3 -c 'import json,sys;print("   deleted:", json.load(sys.stdin)["data"]["processingStatus"])'

echo "OK: 업로드 → 검증(ready) → 미리보기 → 삭제 전체 흐름 통과"
