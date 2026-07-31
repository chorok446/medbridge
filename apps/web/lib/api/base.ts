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
    cached = await invoke<ApiConfig>("sidecar_info");
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
