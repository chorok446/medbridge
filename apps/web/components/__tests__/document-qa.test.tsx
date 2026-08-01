import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentQa } from "@/components/document-qa";
import type { DocumentSummary } from "@/types/api";
import type { QaThreadDetail } from "@/types/qa";

const apiMock = vi.hoisted(() => ({
  listThreads: vi.fn(),
  createThread: vi.fn(),
  getThread: vi.fn(),
  updateThread: vi.fn(),
  deleteThread: vi.fn(),
  askQuestion: vi.fn(),
  retryAnswer: vi.fn(),
}));
vi.mock("@/lib/api/qa", () => apiMock);

vi.mock("@/components/pdf-viewer", () => ({
  PdfViewer: (props: { page: number; highlights: unknown[] }) => (
    <div data-testid="pdf-viewer" data-page={props.page} data-hl={props.highlights.length} />
  ),
}));

const doc = { id: "d1", pageCount: 3 } as DocumentSummary;

class ApiError extends Error {
  status: number;
  constructor(status: number) {
    super("err");
    this.status = status;
  }
}

const answerDetail: QaThreadDetail = {
  thread: { id: "t1", title: "심장", archived: false, createdAt: "", updatedAt: "" },
  messages: [
    {
      id: "m1",
      role: "user",
      content: "심장은 무엇을 하나요?",
      status: "completed",
      sequenceNumber: 1,
      retrievalMode: null,
      claims: [],
    },
    {
      id: "m2",
      role: "assistant",
      content: "심장은 혈액을 보냅니다.",
      status: "completed",
      sequenceNumber: 2,
      retrievalMode: "keyword",
      claims: [
        {
          text: "심장은 혈액을 보낸다",
          verificationStatus: "supported",
          sourceRefs: [
            {
              pageNumber: 2,
              blockId: "b1",
              bbox: [10, 20, 100, 40],
              readingOrder: 0,
              sourceMethod: "digital",
            },
          ],
        },
      ],
    },
  ],
};

function renderQa() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DocumentQa doc={doc} fileUrl="blob:x" />
    </QueryClientProvider>,
  );
}

describe("DocumentQa", () => {
  afterEach(() => vi.restoreAllMocks());

  it("첫 화면에 질문 예시와 비의료 고지를 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    renderQa();
    expect(await screen.findByText("이 문서에 대해 질문해 보세요.")).toBeInTheDocument();
    expect(screen.getByText(/실제 진단·처방·응급 판단에 사용하지 마세요/)).toBeInTheDocument();
    expect(screen.getByText("이 문서의 핵심 내용은 무엇인가요?")).toBeInTheDocument();
  });

  it("질문을 보내면 답변과 출처가 표시된다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread: answerDetail.thread });
    apiMock.askQuestion.mockResolvedValue(answerDetail);
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "심장은 무엇을 하나요?");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    expect(await screen.findByText("심장은 혈액을 보냅니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2쪽" })).toBeInTheDocument();
  });

  it("출처를 클릭하면 해당 페이지로 이동한다", async () => {
    apiMock.listThreads.mockResolvedValue([answerDetail.thread]);
    apiMock.getThread.mockResolvedValue(answerDetail);
    renderQa();
    const src = await screen.findByRole("button", { name: "2쪽" });
    expect(screen.getByTestId("pdf-viewer").getAttribute("data-page")).toBe("1");
    await userEvent.click(src);
    expect(screen.getByTestId("pdf-viewer").getAttribute("data-page")).toBe("2");
    expect(screen.getByTestId("pdf-viewer").getAttribute("data-hl")).toBe("1");
  });

  it("빈 입력이면 보내기 버튼이 비활성화된다(중복/빈 전송 방지)", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    expect(screen.getByRole("button", { name: "보내기" })).toBeDisabled();
  });

  it("자료에 없음 상태를 안내로 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([answerDetail.thread]);
    apiMock.getThread.mockResolvedValue({
      ...answerDetail,
      messages: [
        answerDetail.messages[0],
        { ...answerDetail.messages[1], content: "확인할 수 없습니다.", status: "not_found", claims: [] },
      ],
    });
    renderQa();
    expect(await screen.findByText(/이 자료에서는 확인할 수 없는 내용/)).toBeInTheDocument();
  });

  it("모델 미연결(501)이면 설정 안내를 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread: answerDetail.thread });
    apiMock.askQuestion.mockRejectedValue(new ApiError(501));
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "질문");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    expect(
      await screen.findByText(/앱 설정에서 요약 모델을 연결해 주세요/),
    ).toBeInTheDocument();
  });

  it("외부 동의 필요(403)를 안내한다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread: answerDetail.thread });
    apiMock.askQuestion.mockRejectedValue(new ApiError(403));
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "질문");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    expect(await screen.findByText(/외부 전송을 먼저 허용해 주세요/)).toBeInTheDocument();
  });

  it("기술 정보(chunk id·모델명·bbox 숫자)를 노출하지 않는다", async () => {
    apiMock.listThreads.mockResolvedValue([answerDetail.thread]);
    apiMock.getThread.mockResolvedValue(answerDetail);
    const { container } = renderQa();
    await screen.findByText("심장은 혈액을 보냅니다.");
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/chunkId|sourceChunkIds|b1|deterministic|bbox|:\d{4,5}/i);
  });
});
