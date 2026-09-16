/** Tauri 셸 연동 헬퍼. 브라우저 개발 모드에서는 전부 no-op으로 동작한다. */

import { useSyncExternalStore } from "react";

export const OLLAMA_INSTALL_URL = "https://ollama.com/download/windows";
const ALLOWED_EXTERNAL_URLS = new Set([OLLAMA_INSTALL_URL]);

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

const noSubscription = () => () => {}; // Tauri 여부는 마운트 후 바뀌지 않으므로 구독이 필요 없다

/**
 * 하이드레이션 안전한 isTauri — 렌더 중 isTauri()를 직접 분기하면 정적 export
 * 빌드(Node, window 없음 → 항상 false)와 실제 Tauri 런타임(true)의 첫 렌더
 * 결과가 달라져 하이드레이션 오류(React #418)가 난다. useSyncExternalStore의
 * getServerSnapshot(항상 false, 빌드와 동일)으로 첫 렌더를 맞추고,
 * getSnapshot(실제 isTauri())은 마운트 이후에만 반영된다.
 */
export function useIsTauri(): boolean {
  return useSyncExternalStore(noSubscription, isTauri, () => false);
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

/**
 * 외부 URL을 기본 브라우저로 연다(공식 설치 페이지 안내용). Tauri에서는 opener 플러그인,
 * 브라우저 개발 모드에서는 새 탭. 이 앱이 안내하는 정확한 공식 URL만 허용한다.
 *
 * 반환값 false는 브라우저 팝업 차단처럼 명령 자체는 실패하지 않았지만 창을 만들지 못한
 * 경우다. Tauri 권한 거부·OS opener 실패는 reject해 호출자가 반드시 사용자에게 알린다.
 */
export async function openExternalUrl(url: string): Promise<boolean> {
  if (!ALLOWED_EXTERNAL_URLS.has(url)) {
    throw new Error("허용되지 않은 외부 URL입니다.");
  }
  if (!isTauri()) {
    if (typeof window === "undefined") return false;

    // window.open(url, ..., "noopener")는 브라우저에 따라 성공해도 null을 반환해
    // 팝업 차단과 구분할 수 없다. 같은 출처의 빈 창을 먼저 만들고 opener를 끊은 뒤
    // allowlist URL로 이동하면 차단 여부를 판정하면서 reverse-tabnabbing도 막을 수 있다.
    const popup = window.open("about:blank", "_blank");
    if (!popup) return false;
    try {
      popup.opener = null;
      popup.location.href = url;
      return true;
    } catch {
      popup.close();
      return false;
    }
  }
  const { openUrl } = await import("@tauri-apps/plugin-opener");
  await openUrl(url);
  return true;
}
