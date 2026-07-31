/** Tauri 셸 연동 헬퍼. 브라우저 개발 모드에서는 전부 no-op으로 동작한다. */

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export type SidecarStatus = "starting" | "ready" | "failed";

export async function sidecarStatus(): Promise<SidecarStatus> {
  if (!isTauri()) return "ready";
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<SidecarStatus>("sidecar_status");
}

/** 오류 보고서 zip 저장 (저장 위치는 OS 대화상자로 선택). 반환: 저장 여부 */
export async function saveErrorReport(): Promise<boolean> {
  if (!isTauri()) return false;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<boolean>("save_error_report");
}

export async function relaunchApp(): Promise<void> {
  if (!isTauri()) return;
  const { relaunch } = await import("@tauri-apps/plugin-process");
  await relaunch();
}
