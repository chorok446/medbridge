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

// 데스크톱 앱에서만 진단 파일을 저장할 수 있다 — 오류 안내의 저장 버튼을 검증하려면
// 데스크톱으로 가정해야 한다.
const tauriMock = vi.hoisted(() => ({
  useIsTauri: () => true,
  saveErrorReport: vi.fn().mockResolvedValue(true),
}));
vi.mock("@/lib/tauri", () => tauriMock);

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
    failureCategory: null,
    partial: false,
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

  it("컨텍스트 초과는 무엇을 바꿔야 하는지 알려준다", async () => {
    // 대형 문서에서 실제로 발생한 실패 — "응답 형식 오류"로 뭉뚱그리면 사용자가
    // 손쓸 방법을 알 수 없다.
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: "context_overflow", canRetry: true }),
    );
    renderView();

    expect(await screen.findByText(/한 번에 볼 수 있는 크기를 넘었어요/)).toBeInTheDocument();
    expect(screen.queryByText(/응답 형식이 올바르지 않아/)).toBeNull();
    // 앱이 num_ctx를 직접 지정하므로 "설정을 늘리라"는 안내는 효과가 없다
    expect(screen.queryByText(/컨텍스트 크기를 늘린/)).toBeNull();
  });

  it("시간 초과 실패는 안전한 안내와 재시도 가능한 생성 버튼을 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: "timeout", canRetry: true }),
    );
    renderView();

    expect(await screen.findByText(/요약 모델의 응답이 늦어/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "요약 만들기" })).toBeInTheDocument();
  });

  it("잘못된 모델 응답은 안전한 안내만 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: "invalid_response", canRetry: true }),
    );
    const { container } = renderView();

    expect(await screen.findByText(/응답 형식이 올바르지 않아/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "요약 만들기" })).toBeInTheDocument();
    expect(container.textContent).not.toContain("invalid_response");
  });

  it("분류되지 않은 실패는 일반 안내와 생성 버튼을 보여준다", async () => {
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: null, canRetry: true }),
    );
    renderView();

    expect(
      await screen.findByText("요약을 만들지 못했어요. 다시 시도해 주세요."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "요약 만들기" })).toBeInTheDocument();
  });

  it("요약이 실패하면 그 자리에서 오류 정보를 저장할 수 있다", async () => {
    // 설정 화면까지 찾아 들어가야 하면 사용자는 대개 그냥 포기하고, 지원 요청에는
    // "안 돼요"만 남아 원인을 좁힐 수 없다.
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: "invalid_response", canRetry: true }),
    );
    renderView();

    const button = await screen.findByRole("button", { name: "오류 정보 저장" });
    await userEvent.click(button);

    expect(tauriMock.saveErrorReport).toHaveBeenCalled();
    expect(await screen.findByText("저장했어요")).toBeInTheDocument();
  });

  it("이후 재시도가 실패해도 부분 요약 경고는 남는다", async () => {
    // 경고를 최신 run 기준으로 계산하면, 재시도가 실패하는 순간 경고만 사라지고
    // 불완전한 요약은 그대로 남아 사용자가 그것을 완결된 요약으로 신뢰한다.
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: "timeout", partial: true, canRetry: true }),
    );
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    renderView();

    expect(await screen.findByText(/요약에\s*담기지 못했어요/)).toBeInTheDocument();
  });

  it("내용이 빠진 요약은 완결된 요약처럼 보이지 않는다", async () => {
    // 컨텍스트 초과로 일부 조각이 요약에 담기지 못한 run. 표시하지 않으면 사용자는
    // 특정 절이 통째로 사라진 요약을 완료된 요약으로 신뢰하게 된다.
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ partial: true, canRetry: true }),
    );
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    renderView();

    expect(await screen.findByText(/요약에\s*담기지 못했어요/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 요약하기" })).toBeInTheDocument();
    // 있는 요약은 그대로 보여준다 — 부분 결과를 버리지 않는다
    expect(screen.getByText("이 문서는 심부전을 다룹니다.")).toBeInTheDocument();
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
    apiMock.getSummaryStatus.mockResolvedValue(
      status({ status: "failed", failureCategory: "timeout", canRetry: true }),
    );
    apiMock.getSummaries.mockResolvedValue({ stale: false, artifacts: [overviewArtifact] });
    renderView();

    expect(await screen.findByText("이 문서는 심부전을 다룹니다.")).toBeInTheDocument();
    expect(screen.getByText(/최근 다시 요약이 실패되어 이전 요약을 보여드려요/)).toBeInTheDocument();
    expect(screen.getByText(/요약 모델의 응답이 늦어/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
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
