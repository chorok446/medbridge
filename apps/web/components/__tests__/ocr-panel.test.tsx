import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OcrPanel } from "@/components/ocr-panel";

const apiMock = vi.hoisted(() => ({
  getOcrStatus: vi.fn(),
  startOcr: vi.fn(),
  retryOcr: vi.fn(),
  cancelOcr: vi.fn(),
}));
vi.mock("@/lib/api/ocr", () => apiMock);

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OcrPanel documentId="d1" ocrPageCount={3} />
    </QueryClientProvider>,
  );
}

describe("OcrPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("상태 조회가 실패하면 조용히 숨지 않고 다시 시도 가능한 오류를 보여준다", async () => {
    apiMock.getOcrStatus.mockRejectedValue(new Error("network down"));
    const { container } = renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("이미지 페이지 읽기 상태를 확인하지 못했습니다.");
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    // 패널이 완전히 비지 않았는지(container가 비어있지 않은지) 확인
    expect(container).not.toBeEmptyDOMElement();
  });

  it("다시 시도 버튼을 누르면 상태 조회를 재요청한다", async () => {
    apiMock.getOcrStatus.mockRejectedValue(new Error("network down"));
    renderPanel();
    await screen.findByRole("alert");
    const callsBeforeRetry = apiMock.getOcrStatus.mock.calls.length;

    apiMock.getOcrStatus.mockResolvedValue({
      available: true,
      running: false,
      totalTargets: 0,
      done: 0,
      failed: 0,
      lowConfidencePages: [],
      remainingOcrPages: [],
    });
    await userEvent.click(screen.getByRole("button", { name: "다시 시도" }));

    await waitFor(() =>
      expect(apiMock.getOcrStatus.mock.calls.length).toBeGreaterThan(callsBeforeRetry),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
