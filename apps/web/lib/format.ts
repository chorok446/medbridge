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

export const STATUS_LABELS: Record<ProcessingStatus, string> = {
  created: "생성됨",
  uploading: "업로드 중",
  uploaded: "업로드 완료",
  queued: "검증 대기",
  validating: "파일 검증 중",
  ready: "준비 완료",
  failed: "실패",
  deleting: "삭제 중",
  deleted: "삭제됨",
};

export const STAGE_LABELS: Record<string, string> = {
  upload: "업로드",
  file_validation: "파일 검증",
};

/** 처리 진행 중(폴링 필요) 상태 */
export function isActive(status: ProcessingStatus): boolean {
  return ["created", "uploading", "uploaded", "queued", "validating", "deleting"].includes(status);
}
