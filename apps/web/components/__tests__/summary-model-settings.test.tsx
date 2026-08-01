import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SummaryModelSection } from "@/components/summary-model-settings";

const apiMock = vi.hoisted(() => ({
  getSummarySettings: vi.fn(),
  updateSummarySettings: vi.fn(),
  testSummaryConnection: vi.fn(),
  deleteSummaryApiKey: vi.fn(),
}));
vi.mock("@/lib/api/settings", () => apiMock);

function settings(overrides: Record<string, unknown> = {}) {
  return {
    enabled: false,
    providerType: "disabled",
    endpoint: null,
    modelName: null,
    isLocal: false,
    hasApiKey: false,
    ...overrides,
  };
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <SummaryModelSection />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

describe("SummaryModelSection", () => {
  afterEach(() => vi.restoreAllMocks());

  it("외부 모델을 켜면 외부 전송 경고를 보여준다", async () => {
    apiMock.getSummarySettings.mockResolvedValue(settings());
    renderSection();
    await screen.findByText("요약 모델 사용");
    await userEvent.click(screen.getByLabelText("요약 모델 사용"));
    expect(
      screen.getByText(/문서의 일부 내용이 선택한 모델 서비스로 전송될 수 있습니다/),
    ).toBeInTheDocument();
  });

  it("로컬 모델이면 외부 전송 경고를 숨긴다", async () => {
    apiMock.getSummarySettings.mockResolvedValue(
      settings({ enabled: true, isLocal: true, providerType: "openai_compatible" }),
    );
    renderSection();
    await screen.findByText("요약 모델 사용");
    expect(
      screen.queryByText(/문서의 일부 내용이 선택한 모델 서비스로 전송될 수 있습니다/),
    ).not.toBeInTheDocument();
  });

  it("API 키가 설정되어 있으면 설정됨 표시만 하고 값은 노출하지 않는다", async () => {
    apiMock.getSummarySettings.mockResolvedValue(
      settings({ enabled: true, providerType: "openai_compatible", hasApiKey: true }),
    );
    const { container } = renderSection();
    await screen.findByText("요약 모델 사용");
    expect(screen.getByText("(설정됨)")).toBeInTheDocument();
    // 키 입력란은 비어 있어야 한다(값을 되돌려 채우지 않는다)
    const keyInput = container.querySelector('input[type="password"]') as HTMLInputElement;
    expect(keyInput.value).toBe("");
  });

  it("연결 확인 버튼을 누르면 결과 메시지를 보여준다", async () => {
    apiMock.getSummarySettings.mockResolvedValue(settings({ enabled: true }));
    apiMock.testSummaryConnection.mockResolvedValue({ ok: true, message: "연결에 성공했습니다." });
    renderSection();
    await screen.findByText("요약 모델 사용");
    await userEvent.click(screen.getByRole("button", { name: "연결 확인" }));
    await waitFor(() =>
      expect(screen.getByText("연결에 성공했습니다.")).toBeInTheDocument(),
    );
  });

  it("summary-settings 무효화 후 서버의 로컬 모델 값으로 폼을 다시 맞춘다", async () => {
    apiMock.getSummarySettings
      .mockResolvedValueOnce(settings())
      .mockResolvedValue(
        settings({
          enabled: true,
          providerType: "openai_compatible",
          endpoint: "http://127.0.0.1:11434/v1",
          modelName: "qwen3:8b",
          isLocal: true,
        }),
      );

    const { client } = renderSection();
    const enabled = await screen.findByRole("checkbox", { name: "요약 모델 사용" });
    expect(enabled).not.toBeChecked();

    await client.invalidateQueries({ queryKey: ["summary-settings"] });

    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: "요약 모델 사용" })).toBeChecked(),
    );
    expect(screen.getByRole("checkbox", { name: /내 컴퓨터/ })).toBeChecked();
    expect(screen.getByRole("textbox", { name: /서비스 주소/ })).toHaveValue(
      "http://127.0.0.1:11434/v1",
    );
    expect(screen.getByRole("textbox", { name: /모델 이름/ })).toHaveValue("qwen3:8b");
  });
});
