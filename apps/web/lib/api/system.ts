import { api } from "@/lib/api/client";

/** 업데이트 직전 안전 처리: 새 작업 차단 → 진행 중 작업 대기 → DB checkpoint+백업 */
export function prepareUpdate(): Promise<{ ready: boolean; backupFile: string | null }> {
  return api("/api/system/prepare-update", { method: "POST" });
}

/** 사용자가 업데이트를 미룬 경우 작업 차단 해제 */
export function resumeAfterUpdateCancel(): Promise<undefined> {
  return api("/api/system/resume", { method: "POST" });
}
