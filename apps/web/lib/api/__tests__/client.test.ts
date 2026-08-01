import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api/client";
import { resetApiConfigCache } from "@/lib/api/base";

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

describe("api() — endpoint 준비 이후에만 요청한다", () => {
  beforeEach(() => {
    resetApiConfigCache();
    invokeMock.mockReset();
    markAsTauri();
  });

  it("설정·목록 조회 요청은 sidecar_info가 알려준 런타임 포트로 간다 (하드코딩 포트 아님)", async () => {
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:47000", token: "secret-token" });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ data: { items: [] } }), { status: 200 }),
      );

    await api("/api/documents");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:47000/api/documents",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-MedBridge-Token": "secret-token" }),
      }),
    );
    fetchMock.mockRestore();
  });

  it("endpoint 확인(invoke)이 끝나기 전에는 네트워크 요청을 보내지 않는다", async () => {
    let resolveInvoke!: (v: { base: string; token: string }) => void;
    invokeMock.mockReturnValue(
      new Promise((resolve) => {
        resolveInvoke = resolve;
      }),
    );
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ data: {} }), { status: 200 }),
    );

    const pending = api("/api/documents");
    await Promise.resolve(); // 마이크로태스크 한 틱 양보
    expect(fetchMock).not.toHaveBeenCalled();

    resolveInvoke({ base: "http://127.0.0.1:33000", token: "t" });
    await pending;
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:33000/api/documents",
      expect.anything(),
    );
    fetchMock.mockRestore();
  });

  it("오류의 안전한 details와 correlationId를 보존한다", async () => {
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:33000", token: "t" });
    const details = {
      failureCategory: "db_locked",
      stage: "commit",
      sqliteErrorCode: 5,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "DB_LOCKED",
            message: "안전한 메시지",
            retryable: true,
            details,
          },
          meta: { correlationId: "activation-cid" },
        }),
        { status: 503 },
      ),
    );

    await expect(api("/api/local-ai/activate")).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      code: "DB_LOCKED",
      retryable: true,
      details,
      correlationId: "activation-cid",
    } satisfies Partial<ApiError>);
    fetchMock.mockRestore();
  });

  it("잘리거나 다른 형식의 오류 응답도 안전한 기본값으로 처리한다", async () => {
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:33000", token: "t" });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("not-json", {
        status: 500,
        headers: { "X-Correlation-ID": "header-cid" },
      }),
    );

    await expect(api("/api/local-ai/activate")).rejects.toMatchObject({
      status: 500,
      code: "INTERNAL_ERROR",
      retryable: false,
      details: null,
      correlationId: "header-cid",
    } satisfies Partial<ApiError>);
    fetchMock.mockRestore();
  });
});
