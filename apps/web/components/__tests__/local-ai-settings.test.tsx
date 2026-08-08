import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  const view = render(
    <QueryClientProvider client={client}>
      <LocalAiSection />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

describe("LocalAiSection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

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
      .mockRejectedValueOnce(
        new ApiError(409, "EXTERNAL_AI_OVERWRITE_REQUIRED", "외부", false, {
          details: { failureCategory: "external_settings_conflict" },
          correlationId: "cid-overwrite",
        }),
      )
      .mockResolvedValue({ enabled: true, providerType: "openai_compatible",
        modelName: "qwen3:8b", isLocal: true });
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));
    // 확인창 → 변경
    await userEvent.click(await screen.findByRole("button", { name: "로컬 AI로 변경" }));
    expect(await screen.findByText(/기본 AI로 설정했어요/)).toBeInTheDocument();
    expect(apiMock.activateLocalModel).toHaveBeenLastCalledWith("qwen3:8b", true);
  });

  it("외부 설정 충돌이 아닌 409는 덮어쓰기 확인창을 열지 않는다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    apiMock.activateLocalModel.mockRejectedValue(
      new ApiError(409, "INVALID_STATE", "기술 원문", false, {
        details: { failureCategory: "model_not_installed" },
      }),
    );

    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));

    expect(await screen.findByText("설정을 저장하지 못했어요. 다시 시도해 주세요.")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /바꿀지 확인/ })).not.toBeInTheDocument();
    expect(screen.queryByText("기술 원문")).not.toBeInTheDocument();
  });

  it("덮어쓰기 확인은 이름이 있는 묶음으로 알린다", async () => {
    // role="alertdialog"였는데 초점 트랩도, 최초 초점 이동도, Esc도, backdrop도
    // 없는 그냥 카드였다. 스크린리더는 "경고 대화상자가 열렸다"고 안내하지만
    // 초점은 그대로라 사용자는 무엇을 확인하라는 것인지 찾지 못한다. 게다가
    // 이름(aria-label)조차 없어 "경고 대화상자"라고만 읽혔다(WCAG 4.1.2).
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3, freeDiskBytes: 200 * 1024 ** 3,
    });
    apiMock.activateLocalModel.mockRejectedValue(
      new ApiError(409, "EXTERNAL_AI_OVERWRITE_REQUIRED", "외부", false, {
        details: { failureCategory: "external_settings_conflict" },
      }),
    );

    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));

    const confirm = await screen.findByRole("group", { name: /바꿀지 확인/ });
    expect(confirm).toHaveTextContent("이미 외부 AI가 설정되어 있어요");
    // 모달이 아니므로 대화상자라고 주장하지 않는다.
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("retryable DB lock은 기술 정보를 숨기고 재시도 안내만 보여준다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    apiMock.activateLocalModel.mockRejectedValue(
      new ApiError(503, "DB_LOCKED", "C:/private/medbridge.db token=secret", true, {
        details: { failureCategory: "db_locked", stage: "commit", sqliteErrorCode: 5 },
        correlationId: "cid-secret",
      }),
    );

    const { container } = renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));

    expect(
      await screen.findByText(/다른 작업이 저장 중이라 지금은 설정을 바꿀 수 없어요/),
    ).toBeInTheDocument();
    const visible = container.textContent ?? "";
    for (const hidden of [
      "medbridge.db",
      "token=secret",
      "db_locked",
      "commit",
      "sqliteErrorCode",
      "cid-secret",
    ]) {
      expect(visible).not.toContain(hidden);
    }
  });

  it("영구 저장 실패는 서버 원문 없이 일반 안전 오류만 보여준다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    apiMock.activateLocalModel.mockRejectedValue(
      new ApiError(500, "INTERNAL_ERROR", "raw SQL and private path", false, {
        details: { failureCategory: "db_io", stage: "flush" },
      }),
    );

    const { container } = renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));

    expect(await screen.findByText("설정을 저장하지 못했어요. 다시 시도해 주세요.")).toBeInTheDocument();
    expect(container.textContent).not.toContain("raw SQL and private path");
  });

  it("commit 후 조회 실패는 저장 실패로 안내하지 않고 캐시를 무효화한다", async () => {
    // 서버가 details.committed=true로 "저장은 됐다"고 알린 경우 — 실제로 행이 저장된
    // 상태라 "저장하지 못했어요"라고 말하면 사용자에게 거짓을 알리게 된다.
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    apiMock.activateLocalModel.mockRejectedValue(
      new ApiError(500, "POST_COMMIT_VIEW_FAILED", "설정은 저장했지만", false, {
        details: {
          failureCategory: "post_commit_view_failed",
          stage: "reload_settings_view",
          committed: true,
        },
      }),
    );

    const { client } = renderSection();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));

    expect(await screen.findByText(/설정은 저장했지만/)).toBeInTheDocument();
    expect(screen.queryByText("설정을 저장하지 못했어요. 다시 시도해 주세요.")).toBeNull();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["summary-settings"] });
  });

  it("활성화 성공 후 summary settings를 무효화한다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    apiMock.activateLocalModel.mockResolvedValue({
      enabled: true, providerType: "openai_compatible", modelName: "qwen3:8b", isLocal: true,
    });

    const { client } = renderSection();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));

    expect(await screen.findByText(/기본 AI로 설정했어요/)).toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["summary-settings"] });
  });

  it("확인 버튼을 연속 클릭해도 overwrite 요청은 한 번만 보낸다", async () => {
    apiMock.getLocalAiStatus.mockResolvedValue({ status: "ready" });
    apiMock.getLocalModels.mockResolvedValue({
      models: THREE.map((m) => (m.model === "qwen3:8b" ? { ...m, installed: true } : m)),
      defaultModel: "qwen3:8b", totalRamBytes: 32 * 1024 ** 3,
      freeDiskBytes: 200 * 1024 ** 3,
    });
    let resolveOverwrite!: (value: {
      enabled: boolean;
      providerType: string;
      modelName: string;
      isLocal: boolean;
    }) => void;
    apiMock.activateLocalModel
      .mockRejectedValueOnce(
        new ApiError(409, "EXTERNAL_AI_OVERWRITE_REQUIRED", "safe", false, {
          details: { failureCategory: "external_settings_conflict" },
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOverwrite = resolve;
        }),
      );

    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "기본 모델로 사용" }));
    const confirm = await screen.findByRole("button", { name: "로컬 AI로 변경" });
    await waitFor(() => expect(confirm).toBeEnabled());
    // 여기만 userEvent가 아니라 fireEvent다. userEvent는 상호작용을 순차 처리해
    // 앞 클릭의 처리가 끝난 뒤에야 다음 클릭을 보내므로, 정작 재현하려는 "한 프레임
    // 안의 연타"가 만들어지지 않는다. 잠금이 풀린 틈으로 두 번째 요청이 들어가는
    // 경쟁 상태를 보려면 합성 이벤트를 연달아 쏘아야 한다.
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(apiMock.activateLocalModel).toHaveBeenCalledTimes(2));
    expect(apiMock.activateLocalModel).toHaveBeenLastCalledWith("qwen3:8b", true);
    await waitFor(() => expect(confirm).toBeDisabled());
    resolveOverwrite({
      enabled: true,
      providerType: "openai_compatible",
      modelName: "qwen3:8b",
      isLocal: true,
    });
    expect(await screen.findByText(/기본 AI로 설정했어요/)).toBeInTheDocument();
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
