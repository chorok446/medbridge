import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExtractionReview } from "@/components/extraction-review";
import type { DocumentSummary } from "@/types/api";
import type { PageDetail, PageSummary } from "@/types/extraction";

const extractionApiMock = vi.hoisted(() => ({
  getExtractionStatus: vi.fn(),
  listPages: vi.fn(),
  getPage: vi.fn(),
  getPageBlocks: vi.fn(),
  listTables: vi.fn(),
  retryExtraction: vi.fn(),
  cancelExtraction: vi.fn(),
}));
vi.mock("@/lib/api/extraction", () => extractionApiMock);

const ocrApiMock = vi.hoisted(() => ({
  getOcrStatus: vi.fn(),
  startOcr: vi.fn(),
  retryOcr: vi.fn(),
  cancelOcr: vi.fn(),
  startPageOcr: vi.fn(),
}));
vi.mock("@/lib/api/ocr", () => ocrApiMock);

// 캔버스 렌더링(pdfjs-dist)은 이 테스트 대상이 아니므로 최소 스텁으로 대체한다.
vi.mock("@/components/pdf-viewer", () => ({
  PdfViewer: () => <div data-testid="pdf-viewer-stub" />,
}));

const doc: DocumentSummary = {
  id: "doc1",
  title: "이미지 전용 문서",
  originalFilename: "scan.pdf",
  fileSize: 1000,
  pageCount: 1,
  documentType: "unknown",
  processingStatus: "extracted",
  processingStage: "done",
  processingProgress: 100,
  failureCode: null,
  failureMessage: null,
  createdAt: "2026-07-31T10:00:00Z",
  updatedAt: "2026-07-31T10:01:00Z",
};

const imageOnlyPage: PageSummary = {
  pageNumber: 1,
  width: 595,
  height: 842,
  rotation: 0,
  extractionStatus: "ocr_required",
  scanVerdict: "scanned",
  requiresOcr: true,
  readingOrderConfidence: 0.5,
  textCharacterCount: 0,
  tableCount: 0,
};

const imageOnlyPageDetail: PageDetail = {
  ...imageOnlyPage,
  rawText: "",
  normalizedText: "",
};

/** 글자가 정상적으로 읽힌 쪽 — 구역별 보기 탭을 검증할 때 쓴다. */
const textPage: PageSummary = {
  pageNumber: 1,
  width: 595,
  height: 842,
  rotation: 0,
  extractionStatus: "extracted",
  scanVerdict: "digital",
  requiresOcr: false,
  readingOrderConfidence: 0.95,
  textCharacterCount: 120,
  tableCount: 0,
};

const textBlocks = [
  {
    id: "b1",
    pageNumber: 1,
    blockIndex: 0,
    blockType: "text",
    x0: 72,
    y0: 100,
    x1: 520,
    y1: 130,
    text: "심장은 혈액을 온몸으로 보낸다.",
    readingOrder: 0,
    isHeader: false,
    isFooter: false,
    isCaption: false,
    isTable: false,
    confidence: 1,
  },
  {
    id: "b2",
    pageNumber: 1,
    blockIndex: 1,
    blockType: "text",
    x0: 72,
    y0: 140,
    x1: 520,
    y1: 170,
    text: "정상 심박수는 분당 60에서 100회다.",
    readingOrder: 1,
    isHeader: false,
    isFooter: false,
    isCaption: false,
    isTable: false,
    confidence: 1,
  },
];

function renderReview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ExtractionReview doc={doc} fileUrl="http://example.com/file.pdf" />
    </QueryClientProvider>,
  );
}

describe("ExtractionReview — 이미지 전용 페이지의 본문 탭", () => {
  afterEach(() => vi.restoreAllMocks());

  it("본문이 비어 있으면 수동 OCR 진입 버튼을 보여준다 (자동 판정 실패 대비)", async () => {
    extractionApiMock.getExtractionStatus.mockResolvedValue({
      processingStatus: "extracted",
      processingProgress: 100,
      pageCount: 1,
      pagesDone: 1,
      pagesOcrRequired: 1,
      pagesFailed: 0,
    });
    extractionApiMock.listPages.mockResolvedValue([imageOnlyPage]);
    extractionApiMock.getPage.mockResolvedValue(imageOnlyPageDetail);
    extractionApiMock.getPageBlocks.mockResolvedValue([]);
    ocrApiMock.getOcrStatus.mockResolvedValue({
      available: true,
      running: false,
      totalTargets: 1,
      done: 0,
      failed: 0,
      lowConfidencePages: [],
      remainingOcrPages: [1],
    });

    renderReview();

    expect(await screen.findByText("이 페이지에서 읽을 수 있는 글자를 찾지 못했어요.")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "현재 페이지 이미지로 읽기" });
    expect(button).toBeInTheDocument();

    await userEvent.click(button);
    expect(ocrApiMock.startPageOcr).toHaveBeenCalledWith("doc1", 1);
  });

  it("본문 없음 안내와 '정상 완료' 안내가 동시에 나타나지 않는다", async () => {
    extractionApiMock.getExtractionStatus.mockResolvedValue({
      processingStatus: "extracted",
      processingProgress: 100,
      pageCount: 1,
      pagesDone: 1,
      pagesOcrRequired: 1,
      pagesFailed: 0,
    });
    extractionApiMock.listPages.mockResolvedValue([imageOnlyPage]);
    extractionApiMock.getPage.mockResolvedValue(imageOnlyPageDetail);
    extractionApiMock.getPageBlocks.mockResolvedValue([]);
    ocrApiMock.getOcrStatus.mockResolvedValue({
      available: true,
      running: false,
      totalTargets: 1,
      done: 0,
      failed: 0,
      lowConfidencePages: [],
      remainingOcrPages: [1],
    });

    renderReview();
    await screen.findByText("이 페이지에서 읽을 수 있는 글자를 찾지 못했어요.");

    await userEvent.click(screen.getByRole("tab", { name: "안내" }));
    expect(
      screen.queryByText("본문을 읽었습니다. 특별한 주의사항이 없어요."),
    ).not.toBeInTheDocument();
  });
});

describe("ExtractionReview — 구역별 보기", () => {
  afterEach(() => vi.restoreAllMocks());

  function setupTextPage() {
    extractionApiMock.getExtractionStatus.mockResolvedValue({
      processingStatus: "extracted",
      processingProgress: 100,
      pageCount: 1,
      pagesDone: 1,
      pagesOcrRequired: 0,
      pagesFailed: 0,
    });
    extractionApiMock.listPages.mockResolvedValue([textPage]);
    extractionApiMock.getPage.mockResolvedValue({
      ...textPage,
      rawText: "심장은 혈액을 온몸으로 보낸다.",
      normalizedText: "심장은 혈액을 온몸으로 보낸다.",
    });
    extractionApiMock.getPageBlocks.mockResolvedValue(textBlocks);
    ocrApiMock.getOcrStatus.mockResolvedValue({
      available: true,
      running: false,
      totalTargets: 0,
      done: 0,
      failed: 0,
      lowConfidencePages: [],
      remainingOcrPages: [],
    });
  }

  it("선택한 구역을 색이 아닌 방법으로도 알린다", async () => {
    // 선택 상태를 배경색으로만 표시하면 스크린리더 사용자는 어느 구역이 지금
    // 원문에서 하이라이트되고 있는지 알 수 없다(WCAG 1.4.1 색에만 의존 금지).
    // 원문 대조가 이 화면의 핵심 기능이라 그 자리에서 막힌다.
    setupTextPage();
    renderReview();

    await userEvent.click(await screen.findByRole("tab", { name: "구역별 보기" }));

    const first = await screen.findByRole("button", { name: /심장은 혈액을/ });
    const second = screen.getByRole("button", { name: /정상 심박수는/ });
    expect(first).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(first);

    expect(first).toHaveAttribute("aria-pressed", "true");
    // 한 번에 하나만 선택된다 — 이전 선택이 남아 있으면 어느 것이 하이라이트인지 흐려진다.
    expect(second).toHaveAttribute("aria-pressed", "false");
  });
});
