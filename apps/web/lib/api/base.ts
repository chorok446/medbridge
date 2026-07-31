/** sidecar 주소·토큰 해석.
 *
 * Tauri 안에서는 셸이 발급한 동적 포트·토큰을 invoke로 받아오고,
 * 브라우저 개발 모드에서는 NEXT_PUBLIC_API_URL(기본 127.0.0.1:8765)을 쓴다.
 * 포트·토큰 값은 GUI에 표시하지 않는다.
 */

export interface ApiConfig {
  base: string;
  token: string;
}

let cached: ApiConfig | null = null;

export async function getApiConfig(): Promise<ApiConfig> {
  if (cached) return cached;
  if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
    const { invoke } = await import("@tauri-apps/api/core");
    // invoke가 실패하면 여기서 그대로 throw한다 — 개발용 기본 포트로
    // 조용히 대체하지 않는다. 패키징 환경에서 잘못된 포트로 캐시가
    // 굳어버리는 것보다 요청이 실패하는 편이 훨씬 진단하기 쉽다.
    const resolved = await invoke<ApiConfig>("sidecar_info");
    // 포트만 남긴다 — 토큰은 절대 로그에 남기지 않는다.
    console.info(`[medbridge] sidecar endpoint resolved: ${new URL(resolved.base).host}`);
    cached = resolved;
  } else {
    cached = {
      base: process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8765",
      token: "",
    };
  }
  return cached;
}

export function resetApiConfigCache(): void {
  cached = null;
}
