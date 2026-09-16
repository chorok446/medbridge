import { beforeEach, describe, expect, it, vi } from "vitest";
import { resetApiConfigCache } from "@/lib/api/base";
import { reportError, uploadDocument } from "@/lib/api/documents";

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

describe("업로드·오류 보고서도 같은 런타임 포트를 쓴다", () => {
  beforeEach(() => {
    resetApiConfigCache();
    invokeMock.mockReset();
    markAsTauri();
    invokeMock.mockResolvedValue({ base: "http://127.0.0.1:59001", token: "tok" });
  });

  it("PDF 업로드는 XHR을 sidecar_info가 알려준 런타임 포트로 연다", async () => {
    const openSpy = vi.spyOn(XMLHttpRequest.prototype, "open");
    const headerSpy = vi.spyOn(XMLHttpRequest.prototype, "setRequestHeader");
    const sendSpy = vi
      .spyOn(XMLHttpRequest.prototype, "send")
      .mockImplementation(() => undefined);

    const file = new File(["dummy"], "test.pdf", { type: "application/pdf" });
    uploadDocument(file, undefined, () => undefined);

    // xhr.open은 getApiConfig()가 resolve된 뒤에 불린다. setTimeout(0) 한 번으로
    // 기다리면 그 체인이 한 틱만 길어져도 깨진다 — 로컬에서는 통과하고 부하 걸린
    // CI에서만 실패하거나, 반대로 우연히 맞아 회귀를 놓친다. 조건이 성립할 때까지
    // 기다린다.
    await vi.waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        "POST",
        "http://127.0.0.1:59001/api/documents/stream",
      ),
    );
    expect(headerSpy).toHaveBeenCalledWith("Content-Type", "application/pdf");
    expect(headerSpy).toHaveBeenCalledWith("X-MedBridge-Filename", "test.pdf");
    expect(headerSpy).toHaveBeenCalledWith("X-MedBridge-File-Size", "5");
    expect(sendSpy).toHaveBeenCalledWith(file);
    openSpy.mockRestore();
    headerSpy.mockRestore();
    sendSpy.mockRestore();
  });

  it("오류 보고서 요청도 별도 상수 없이 동일한 endpoint provider(getApiConfig)를 거친다", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ data: { received: true } }), { status: 200 }));

    await reportError(null, "테스트 신고");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:59001/api/reports",
      expect.anything(),
    );
    fetchMock.mockRestore();
  });
});
