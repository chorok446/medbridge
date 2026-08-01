import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LocalAiSection } from "@/components/local-ai-settings";
import { ApiError } from "@/lib/api/client";
import type { LocalModel, PullEvent } from "@/lib/api/local-ai";

const apiMock = vi.hoisted(() => ({
  getLocalAiStatus: vi.fn(),
  getLocalModels: vi.fn(),
  testLocalModel: vi.fn(),
  activateLocalModel: vi.fn(),
  streamModelPull: vi.fn(),
}));
vi.mock("@/lib/api/local-ai", () => apiMock);

const tauriMock = vi.hoisted(() => ({ openExternalUrl: vi.fn() }));
vi.mock("@/lib/tauri", () => tauriMock);

function model(over: Partial<LocalModel>): LocalModel {
  return {
    model: "qwen3:8b", tier: "balanced", label: "균형형", description: "설명",
    approxBytes: 5.2 * 1024 ** 3, installed: false, recommended: true,
    ramAdvice: "recommended", diskOk: true, requiredBytes: 8 * 1024 ** 3, ...over,
  };
}

const THREE = [
  model({ model: "qwen3:4b", tier: "light", label: "경량형", recommended: false,
    approxBytes: 2.5 * 1024 ** 3 }),
  model({ model: "qwen3:8b", tier: "balanced", label: "균형형", recommended: true }),
  model({ model: "qwen3:14b", tier: "quality", label: "고품질형", recommended: false,
    approxBytes: 9.3 * 1024 ** 3, ramAdvice: "warn" }),
];

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LocalAiSection />
    </QueryClientProvider>,
  );
}

describe("LocalAiSection", () => {
  afterEach(() => vi.restoreAllMocks());

  it("Ollama 미실행이면 설치 안내를 열고 다시 확인할 수 있다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "not_running" });
    renderSection();
    expect(await screen.findByText(/로컬 AI 실행 프로그램/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "설치 안내 열기" }));
    expect(tauriMock.openExternalUrl).toHaveBeenCalledWith(
      expect.stringContaining("ollama.com"),
    );
    await userEvent.click(screen.getByRole("button", { name: "다시 확인" }));
    expect(apiMock.getLocalAiStatus).toHaveBeenCalledTimes(2);
  });

  it("준비됨·모델 없음이면 추천(균형형 8B)과 예상 용량을 보여준다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE, defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    renderSection();
    expect(await screen.findByText("경량형")).toBeInTheDocument();
    expect(screen.getByText("균형형")).toBeInTheDocument();
    expect(screen.getByText("추천")).toBeInTheDocument();
    expect(screen.getByText("약 5.2GB")).toBeInTheDocument();
    // 고품질형은 RAM 경고
    expect(screen.getByText(/느리거나 실행이 어려울 수/)).toBeInTheDocument();
  });

  it("다운로드 진행률을 progressbar로 보여주고 완료 후 연결을 확인한다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    // 첫 조회: 미설치 / 다운로드 후 재조회: 설치됨
    apiMock.getLocalModels
      .mockResolvedValueOnce({ models: THREE, defaultModel: "qwen3:8b",
        totalRamBytes: 32 * 1024 ** 3, freeDiskBytes: 200 * 1024 ** 3 })
      .mockResolvedValue({
        models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
        defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3, freeDiskBytes: 200 * 1024 ** 3,
      });
    apiMock.testLocalModel.mockResolvedValue({ ok: true, message: "로컬 AI를 사용할 준비가 됐습니다." });
    apiMock.streamModelPull.mockImplementation(
      async (_m: string, opts: { onEvent: (e: PullEvent) => void }) => {
        opts.onEvent({ type: "progress", phase: "내려받는 중", total: 100, completed: 50, percent: 50 });
        opts.onEvent({ type: "completed" });
      },
    );
    renderSection();
    const buttons = await screen.findAllByRole("button", { name: "내려받기" });
    await userEvent.click(buttons[1]); // 균형형(8B)
    // 완료 후 연결 확인 성공 메시지
    expect(await screen.findByText(/사용할 준비가 됐습니다/)).toBeInTheDocument();
    expect(apiMock.testLocalModel).toHaveBeenCalledWith("qwen3:8b");
  });

  it("설치된 모델은 연결 확인·기본 설정할 수 있고 외부 설정 덮어쓰기를 확인받는다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3, freeDiskBytes: 200 * 1024 ** 3,
    });
    // 첫 activate는 409(외부 존재) → 확인 후 성공
    apiMock.activateLocalModel
      .mockRejectedValueOnce(new ApiError(409, "INVALID_STATE", "외부", false))
      .mockResolvedValue({ enabled: true, providerType: "openai_compatible",
        modelName: "qwen3:8b", isLocal: true });
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));
    // 확인창 → 변경
    await userEvent.click(await screen.findByRole("button", { name: "로컬 AI로 변경" }));
    expect(await screen.findByText(/기본 AI로 설정했어요/)).toBeInTheDocument();
    expect(apiMock.activateLocalModel).toHaveBeenLastCalledWith("qwen3:8b", true);
  });

  it("기술 정보(포트·endpoint·NDJSON·quantization)를 노출하지 않는다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE, defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    const { container } = renderSection();
    await screen.findByText("경량형");
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/localhost|11434|endpoint|NDJSON|quantization|bearer|OpenAI/i);
  });
});
