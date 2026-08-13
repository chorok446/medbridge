import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  beforeEach(() => {
    for (const mock of Object.values(apiMock)) mock.mockReset();
  });
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
    renderSection();
    await screen.findByText("요약 모델 사용");
    expect(screen.getByText("(설정됨)")).toBeInTheDocument();
    // 키 입력란은 비어 있어야 한다(값을 되돌려 채우지 않는다).
    //
    // CSS 셀렉터(input[type="password"])가 아니라 라벨로 찾는다. 셀렉터로 찾으면
    // 라벨과 입력란의 연결이 끊겨도 통과해서, 정작 "스크린리더 사용자가 이 칸을
    // 찾을 수 있는가"라는 이 테스트가 보증해야 할 성질이 검증되지 않는다.
    expect(screen.getByLabelText(/API 키/)).toHaveValue("");
  });

  it("연결 확인은 화면의 draft를 먼저 저장한 뒤 같은 설정을 검사한다", async () => {
    const saved = settings({
      enabled: true,
      providerType: "openai_compatible",
      endpoint: "https://draft.example.com/v1",
      modelName: "draft-model",
      hasApiKey: true,
    });
    apiMock.getSummarySettings.mockResolvedValue(settings({ enabled: true }));
    apiMock.updateSummarySettings.mockResolvedValue(saved);
    apiMock.testSummaryConnection.mockResolvedValue({ ok: true, message: "연결에 성공했습니다." });
    renderSection();
    await screen.findByText("요약 모델 사용");

    await userEvent.type(
      screen.getByRole("textbox", { name: /서비스 주소/ }),
      "https://draft.example.com/v1",
    );
    await userEvent.type(screen.getByRole("textbox", { name: /모델 이름/ }), "draft-model");
    await userEvent.type(screen.getByLabelText(/API 키/), "new-secret");
    await userEvent.click(screen.getByRole("button", { name: "연결 확인" }));

    await waitFor(() =>
      expect(apiMock.updateSummarySettings).toHaveBeenCalledWith({
        enabled: true,
        providerType: "openai_compatible",
        endpoint: "https://draft.example.com/v1",
        modelName: "draft-model",
        isLocal: false,
        apiKey: "new-secret",
      }),
    );
    expect(apiMock.updateSummarySettings.mock.invocationCallOrder[0]).toBeLessThan(
      apiMock.testSummaryConnection.mock.invocationCallOrder[0],
    );
    await waitFor(() =>
      expect(screen.getByText("연결에 성공했습니다.")).toBeInTheDocument(),
    );
    expect(screen.getByText(/입력한 설정을 저장한 뒤 연결을 확인했습니다/)).toBeInTheDocument();
  });

  it("draft 저장에 실패하면 이전 저장값으로 연결 확인을 진행하지 않는다", async () => {
    apiMock.getSummarySettings.mockResolvedValue(settings({ enabled: true }));
    apiMock.updateSummarySettings.mockRejectedValue(new Error("invalid draft"));
    renderSection();

    await userEvent.type(
      await screen.findByRole("textbox", { name: /서비스 주소/ }),
      "https://draft.example.com/v1",
    );
    await userEvent.click(screen.getByRole("button", { name: "연결 확인" }));

    expect(
      await screen.findByText(/설정을 저장하지 못해 연결을 확인하지 못했습니다/),
    ).toBeInTheDocument();
    expect(apiMock.testSummaryConnection).not.toHaveBeenCalled();
  });

  it("저장된 API 키는 확인 후 delete API로 삭제한다", async () => {
    apiMock.getSummarySettings.mockResolvedValue(
      settings({ enabled: true, providerType: "openai_compatible", hasApiKey: true }),
    );
    apiMock.deleteSummaryApiKey.mockResolvedValue(
      settings({ enabled: true, providerType: "openai_compatible", hasApiKey: false }),
    );
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: "저장된 API 키 삭제" }),
    );
    expect(screen.getByRole("alertdialog")).toHaveTextContent("저장된 API 키를 삭제할까요?");
    expect(apiMock.deleteSummaryApiKey).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "API 키 삭제" }));
    await waitFor(() => expect(apiMock.deleteSummaryApiKey).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("저장된 API 키를 삭제했습니다.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "저장된 API 키 삭제" })).toBeNull();
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
