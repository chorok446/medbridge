import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getApiConfig, resetApiConfigCache } from "@/lib/api/base";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

function markAsTauri() {
  Object.defineProperty(window, "__TAURI_INTERNALS__", {
    value: {},
    configurable: true,
  });
}

function markAsBrowser() {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
}

describe("getApiConfig", () => {
  const originalEnv = process.env.NEXT_PUBLIC_API_URL;

  beforeEach(() => {
    resetApiConfigCache();
    invokeMock.mockReset();
    markAsBrowser();
  });

  afterEach(() => {
    process.env.NEXT_PUBLIC_API_URL = originalEnv;
  });

  it("Tauri 환경에서는 sidecar_info가 알려준 실제 런타임 포트를 그대로 쓴다", async () => {
    markAsTauri();
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:54321", token: "tok" });

    const config = await getApiConfig();

    expect(invokeMock).toHaveBeenCalledWith("sidecar_info");
    expect(config.base).toBe("http://127.0.0.1:54321");
    expect(config.base).not.toBe("http://127.0.0.1:8765"); // 개발용 기본 포트가 아니어야 한다
  });

  it("Tauri 환경에서 invoke가 실패하면 개발용 기본 포트로 조용히 대체하지 않는다", async () => {
    markAsTauri();
    invokeMock.mockRejectedValue(new Error("IPC not ready"));

    await expect(getApiConfig()).rejects.toThrow("IPC not ready");
  });

  it("브라우저 개발 모드에서는 NEXT_PUBLIC_API_URL(없으면 8765)을 쓴다", async () => {
    markAsBrowser();
    process.env.NEXT_PUBLIC_API_URL = "http://127.0.0.1:9999";

    const config = await getApiConfig();

    expect(invokeMock).not.toHaveBeenCalled();
    expect(config.base).toBe("http://127.0.0.1:9999");
  });

  it("브라우저 개발 모드에서 환경변수가 없으면 8765로 대체한다", async () => {
    markAsBrowser();
    delete process.env.NEXT_PUBLIC_API_URL;

    const config = await getApiConfig();

    expect(config.base).toBe("http://127.0.0.1:8765");
  });

  it("한 번 확정된 값은 재호출해도 다시 invoke하지 않고 캐시를 그대로 준다", async () => {
    markAsTauri();
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:11111", token: "a" });

    const first = await getApiConfig();
    const second = await getApiConfig();

    expect(invokeMock).toHaveBeenCalledTimes(1);
    expect(second).toBe(first);
  });

  it("resetApiConfigCache 이후에는 다시 확인한다", async () => {
    markAsTauri();
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:11111", token: "a" });
    await getApiConfig();

    resetApiConfigCache();
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:22222", token: "b" });
    const config = await getApiConfig();

    expect(invokeMock).toHaveBeenCalledTimes(2);
    expect(config.base).toBe("http://127.0.0.1:22222");
  });
});
