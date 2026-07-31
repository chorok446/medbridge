import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SummaryView } from "@/components/summary-view";
import type { DocumentSummary } from "@/types/api";

const apiMock = vi.hoisted(() => ({
  createSummary: vi.fn(),
  retrySummary: vi.fn(),
  getSummaryStatus: vi.fn(),
  getSummaries: vi.fn(),
  cancelSummary: vi.fn(),
  deleteSummaries: vi.fn(),
}));
vi.mock("@/lib/api/summary", () => apiMock);

// PdfViewer는 무거운 pdf.js 렌더러라 목으로 대체 (요약 UI만 검증)
vi.mock("@/components/pdf-viewer", () => ({
  PdfViewer: (props: { page: number; highlights: unknown[] }) => (
    <div data-testid="pdf-viewer" data-page={props.page} data-highlights={props.highlights.length} />
  ),
}));

const doc = { id: "d1", pageCount: 3 } as DocumentSummary;

function status(overrides: Record<string, unknown> = {}) {
  return {
    providerAvailable: true,
    status: "succeeded",
    stale: false,
    sourceRevision: 2,
    currentRevision: 2,
    progress: 100,
    canRetry: false,
    ...overrides,
  };
}

const overviewArtifact = {
  artifactType: "overview" as const,
  title: null,
  position: 0,
  content: { text: "이 문서는 심부전을 다룹니다." },
  sourceRefs: [
    {
      pageNumber: 2,
      blockId: "b1",
      bbox: [10, 20, 100, 40] as [number, number, number, number],
      readingOrder: 0,
      sourceMethod: "digital" as const,
    },
  ],
};

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SummaryView doc={doc} fileUrl="blob:test" />
    </QueryClientProvider>,
  );
}

describe("SummaryView", () => {
  afterEach(() => vi.restoreAllMocks());

  it("모델 미설정이면 설정 안내를 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status({ providerAvailable: false, status: null }));
    renderView();
    expect(
      await screen.findByText("요약 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요."),
    ).toBeInTheDocument();
  });

  it("아직 생성 안 함 상태면 요약 만들기 버튼을 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status({ status: null }));
    renderView();
    expect(await screen.findByRole("button", { name: "요약 만들기" })).toBeInTheDocument();
  });

  it("생성 중이면 진행 안내를 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status({ status: "running", progress: 50 }));
    renderView();
    expect(await screen.findByText(/요약을 만드는 중이에요/)).toBeInTheDocument();
  });

  it("실패 상태면 다시 시도할 수 있다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status({ status: "failed" }));
    renderView();
    expect(await screen.findByText(/요약을 만들지 못했어요/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "요약 만들기" })).toBeInTheDocument();
  });

  it("완료 상태면 요약 항목과 비의료 고지를 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status());
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    renderView();
    expect(await screen.findByText("이 문서는 심부전을 다룹니다.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "이 내용은 학습 보조용이며 실제 환자의 진단·처방·응급 판단에 사용하지 마세요.",
      ),
    ).toBeInTheDocument();
  });

  it("오래된 요약이면 안내와 다시 요약 버튼을 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status({ stale: true }));
    apiMock.getSummaries.mockResolvedValue({ stale: true, artifacts: [overviewArtifact] });
    renderView();
    expect(await screen.findByText(/문서가 바뀌어 이 요약은 오래된 내용/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 요약하기" })).toBeInTheDocument();
  });

  it("출처를 클릭하면 해당 페이지로 이동한다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status());
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    renderView();
    const sourceBtn = await screen.findByRole("button", { name: "2쪽" });
    expect(screen.getByTestId("pdf-viewer").getAttribute("data-page")).toBe("1");
    await userEvent.click(sourceBtn);
    expect(screen.getByTestId("pdf-viewer").getAttribute("data-page")).toBe("2");
    expect(screen.getByTestId("pdf-viewer").getAttribute("data-highlights")).toBe("1");
  });

  it("최신 시도가 실패해도 이전 성공 요약을 계속 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status({ status: "failed" }));
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    renderView();
    // 이전 성공 요약이 보이고, 실패 안내 + 다시 시도 링크가 함께 뜬다
    expect(await screen.findByText("이 문서는 심부전을 다룹니다.")).toBeInTheDocument();
    expect(screen.getByText(/최근 다시 요약이 실패되어 이전 요약을 보여드려요/)).toBeInTheDocument();
    // "요약 만들기" 초기 프롬프트는 뜨지 않는다(이전 요약이 있으므로)
    expect(screen.queryByRole("button", { name: "요약 만들기" })).not.toBeInTheDocument();
  });

  it("기술 정보(chunk id·모델명·토큰)를 노출하지 않는다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(status());
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    const { container } = renderView();
    await screen.findByText("이 문서는 심부전을 다룹니다.");
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/chunkId|sourceChunkIds|token|bm25|deterministic|:\d{4,5}/i);
  });
});
