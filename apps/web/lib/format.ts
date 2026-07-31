import type { ProcessingStatus } from "@/types/api";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 내부 상태 → 사용자 문구. 내부 enum 값을 GUI에 직접 노출하지 않는다. */
export const STATUS_LABELS: Record<ProcessingStatus, string> = {
  created: "업로드 준비 중",
  uploading: "파일을 올리는 중",
  uploaded: "파일 업로드 완료",
  queued: "분석을 준비하는 중",
  validating: "파일을 확인하는 중",
  ready: "내용 읽기를 준비하는 중",
  extracting: "문서 내용을 확인하는 중",
  extracted: "학습 준비 완료",
  partially_extracted: "본문을 읽었습니다 (일부 페이지 제외)",
  ocr_required: "이미지로 된 문서예요",
  extraction_failed: "문서 내용을 읽지 못했습니다",
  failed: "파일을 처리하지 못했습니다",
  deleting: "파일을 삭제하는 중",
  deleted: "삭제 완료",
};

/** 실패 코드 → 사용자가 할 수 있는 조치 안내 (기술 용어 금지) */
export const FAILURE_GUIDES: Record<string, string> = {
  ENCRYPTED_PDF:
    "PDF가 암호로 보호되어 있습니다. 암호를 해제한 파일로 다시 업로드해 주세요.",
  CORRUPTED_PDF:
    "PDF가 손상되었을 수 있습니다. 파일을 다시 저장하거나 다른 파일을 선택해 주세요.",
  INVALID_PDF_SIGNATURE: "PDF 형식이 아닌 파일입니다. PDF 파일을 선택해 주세요.",
  INVALID_FILE_TYPE: "PDF 형식이 아닌 파일입니다. PDF 파일을 선택해 주세요.",
  EMPTY_PDF: "내용이 없는 파일입니다. 다른 PDF를 선택해 주세요.",
  FILE_TOO_LARGE: "파일이 너무 큽니다. 더 작은 PDF로 나누어 업로드해 주세요.",
};

export function failureGuide(code: string | null): string {
  return (
    (code && FAILURE_GUIDES[code]) ??
    "PDF가 손상되었거나 암호로 보호되어 있을 수 있습니다. 다른 PDF를 선택하거나 다시 시도해 주세요."
  );
}

/** 처리 진행 중(폴링 필요) 상태 */
export function isActive(status: ProcessingStatus): boolean {
  return [
    "created",
    "uploading",
    "uploaded",
    "queued",
    "validating",
    "ready",
    "extracting",
    "deleting",
  ].includes(status);
}

/** 본문 추출 결과를 볼 수 있는 상태 */
export function hasExtraction(status: ProcessingStatus): boolean {
  return ["extracted", "partially_extracted", "ocr_required"].includes(status);
}
