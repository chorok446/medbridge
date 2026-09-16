import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  deleteSummaryApiKey,
  testSummaryConnection,
  updateSummarySettings,
} from "@/lib/api/settings";

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/client", () => ({ api: apiMock }));

describe("요약 모델 설정 API 계약", () => {
  beforeEach(() => apiMock.mockReset());

  it("화면 draft를 JSON으로 저장한다", async () => {
    apiMock.mockResolvedValue({ enabled: true });
    const patch = {
      enabled: true,
      providerType: "openai_compatible",
      endpoint: "https://draft.example.com/v1",
      modelName: "draft-model",
      isLocal: false,
      apiKey: "secret",
    };

    await updateSummarySettings(patch);

    expect(apiMock).toHaveBeenCalledWith("/api/settings/summary", {
      method: "PUT",
      body: JSON.stringify(patch),
    });
  });

  it("저장된 설정의 연결 확인 endpoint를 POST로 호출한다", async () => {
    apiMock.mockResolvedValue({ ok: true, message: "ok" });

    await testSummaryConnection();

    expect(apiMock).toHaveBeenCalledWith("/api/settings/summary/test", { method: "POST" });
  });

  it("저장된 API 키 삭제 endpoint를 DELETE로 호출한다", async () => {
    apiMock.mockResolvedValue({ hasApiKey: false });

    await deleteSummaryApiKey();

    expect(apiMock).toHaveBeenCalledWith("/api/settings/summary/key", { method: "DELETE" });
  });
});
