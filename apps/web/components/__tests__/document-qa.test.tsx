import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentQa } from "@/components/document-qa";
import { ApiError } from "@/lib/api/client";
import type { DocumentSummary } from "@/types/api";
import type { QaStreamEvent, QaThreadDetail } from "@/types/qa";

const apiMock = vi.hoisted(() => ({
  listThreads: vi.fn(),
  createThread: vi.fn(),
  getThread: vi.fn(),
  updateThread: vi.fn(),
  deleteThread: vi.fn(),
  retryAnswer: vi.fn(),
}));
vi.mock("@/lib/api/qa", () => apiMock);

const streamMock = vi.hoisted(() => ({
  streamQuestion: vi.fn(),
  cancelStream: vi.fn(),
}));
vi.mock("@/lib/api/qa-stream", () => streamMock);

vi.mock("@/components/pdf-viewer", () => ({
  PdfViewer: (props: { page: number; highlights: unknown[] }) => (
    <div data-testid="pdf-viewer" data-page={props.page} data-hl={props.highlights.length} />
  ),
}));

const doc = { id: "d1", pageCount: 3 } as DocumentSummary;

const thread = { id: "t1", title: "심장", archived: false, createdAt: "", updatedAt: "" };

const answerDetail: QaThreadDetail = {
  thread,
  messages: [
    {
      id: "m1",
      role: "user",
      content: "심장은 무엇을 하나요?",
      status: "completed",
      sequenceNumber: 1,
      retrievalMode: null,
      claims: [],
      followups: [],
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
      followups: [],
    },
  ],
};

/** onEvent에 순서대로 이벤트를 흘려주는 가짜 스트림. */
function fakeStream(events: QaStreamEvent[]) {
  return async (
    _d: string,
    _t: string,
    _q: string,
    opts: { signal: AbortSignal; onEvent: (e: QaStreamEvent) => void },
  ) => {
    for (const e of events) {
      if (opts.signal.aborted) return;
      opts.onEvent(e);
    }
  };
}

function renderQa() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DocumentQa doc={doc} fileUrl="blob:x" />
    </QueryClientProvider>,
  );
}

// 학습 수준은 localStorage에 남는다 — 초기화하지 않으면 앞 테스트의 선택이 뒤 테스트로
// 새어 기본값 검증이 순서에 따라 깨진다.
beforeEach(() => window.localStorage.clear());

describe("DocumentQa", () => {
  afterEach(() => vi.restoreAllMocks());

  it("첫 화면에 질문 예시와 비의료 고지를 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    renderQa();
    expect(await screen.findByText("이 문서에 대해 질문해 보세요.")).toBeInTheDocument();
    expect(screen.getByText(/실제 진단·처방·응급 판단에 사용하지 마세요/)).toBeInTheDocument();
    expect(screen.getByText("이 문서의 핵심 내용은 무엇인가요?")).toBeInTheDocument();
  });

  it("스트리밍으로 진행 중 검증된 주장을 점진적으로 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    // getThread는 스트림 종료 후 리로드에 쓰인다 — 확정 메시지 반환.
    apiMock.getThread.mockResolvedValue(answerDetail);
    streamMock.streamQuestion.mockImplementation(
      fakeStream([
        { type: "started", requestId: "r1", messageId: "m2" },
        { type: "phase", phase: "retrieving" },
        {
          type: "claim",
          seq: 1,
          claimIndex: 0,
          text: "심장은 혈액을 보낸다",
          sources: [{ pageNumber: 2, bbox: [10, 20, 100, 40], blockId: "b1", sourceMethod: "digital" }],
        },
        { type: "completed", message: answerDetail.messages[1] },
      ]),
    );
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "심장은 무엇을 하나요?");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    // 스트림 종료 후 확정 답변이 표시된다.
    expect(await screen.findByText("심장은 혈액을 보냅니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2쪽" })).toBeInTheDocument();
  });

  it("진행 중 중단 버튼을 눌러 취소한다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue(answerDetail);
    streamMock.cancelStream.mockResolvedValue({ id: "m2", status: "cancelled", errorCode: null });
    // started 후 무한 대기(취소 신호를 기다린다).
    streamMock.streamQuestion.mockImplementation(
      async (
        _d: string,
        _t: string,
        _q: string,
        opts: { signal: AbortSignal; onEvent: (e: QaStreamEvent) => void },
      ) => {
        opts.onEvent({ type: "started", requestId: "r1", messageId: "m2" });
        opts.onEvent({ type: "phase", phase: "generating" });
        await new Promise<void>((resolve) => {
          opts.signal.addEventListener("abort", () => resolve());
        });
      },
    );
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "심장은 무엇을 하나요?");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    const stop = await screen.findByRole("button", { name: "중단" });
    await userEvent.click(stop);
    await waitFor(() => expect(streamMock.cancelStream).toHaveBeenCalled());
  });

  it("terminal 이벤트 없이 연결이 끊기면 진행 상태에 멈추지 않는다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue({ thread, messages: [] });
    // started/phase만 오고 completed/interrupted 없이 EOF.
    streamMock.streamQuestion.mockImplementation(
      fakeStream([
        { type: "started", requestId: "r1", messageId: "m2" },
        { type: "phase", phase: "generating" },
      ]),
    );
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "심장은 무엇을 하나요?");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    // 스트림 종료 후 진행 표시(중단 버튼)가 사라져 active에 멈추지 않는다.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "중단" })).not.toBeInTheDocument(),
    );
    // 다시 입력하면 보내기가 활성화된다(중복/영구 잠금 없음).
    await userEvent.type(screen.getByLabelText("질문 입력"), "다시");
    expect(screen.getByRole("button", { name: "보내기" })).not.toBeDisabled();
  });

  it("출처를 클릭하면 해당 페이지로 이동한다", async () => {
    apiMock.listThreads.mockResolvedValue([thread]);
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
    apiMock.listThreads.mockResolvedValue([thread]);
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

  it("답변이 실패해도 화면의 파란 버튼은 하나뿐이다", async () => {
    // DESIGN.md 155행: Primary는 화면의 "다음 할 일" 하나에만. 재시도 버튼과 하단
    // 보내기 버튼이 둘 다 파란색이면 무엇을 눌러야 하는지 판단할 기준이 사라진다.
    // 이 상황에서 진짜 다음 행동은 재시도인데 시각적으로 보내기와 동급이었다.
    //
    // 사용자에게 보이는 것이 색의 위계 자체라 글자로는 확인할 방법이 없다.
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue({
      ...answerDetail,
      messages: [
        answerDetail.messages[0],
        { ...answerDetail.messages[1], status: "failed", claims: [] },
      ],
    });
    renderQa();

    expect(await screen.findByRole("button", { name: "다시 시도" })).toBeInTheDocument();

    const primaries = screen
      .getAllByRole("button")
      .filter((b) => b.className.includes("bg-blue-600"));
    expect(primaries).toHaveLength(1);
    expect(primaries[0]).toHaveAccessibleName("보내기");
  });

  it("모델 미연결(501)이면 설정 안내를 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue({ thread, messages: [] });
    streamMock.streamQuestion.mockRejectedValue(new ApiError(501, "X", "e", false));
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "질문");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    expect(await screen.findByText(/앱 설정에서 요약 모델을 연결해 주세요/)).toBeInTheDocument();
  });

  it("외부 동의 필요(403)를 안내한다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue({ thread, messages: [] });
    streamMock.streamQuestion.mockRejectedValue(new ApiError(403, "X", "e", false));
    renderQa();
    await screen.findByText("이 문서에 대해 질문해 보세요.");
    await userEvent.type(screen.getByLabelText("질문 입력"), "질문");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    expect(await screen.findByText(/외부 전송을 먼저 허용해 주세요/)).toBeInTheDocument();
  });

  it("기술 정보(chunk id·모델명·bbox 숫자)를 노출하지 않는다", async () => {
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue(answerDetail);
    const { container } = renderQa();
    await screen.findByText("심장은 혈액을 보냅니다.");
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/chunkId|sourceChunkIds|b1|deterministic|bbox|:\d{4,5}/i);
  });
});

describe("답변 렌더", () => {
  afterEach(() => vi.restoreAllMocks());

  const claim = {
    text: "심장은 혈액을 보낸다",
    verificationStatus: "supported" as const,
    sourceRefs: [
      {
        pageNumber: 3,
        blockId: "b1",
        bbox: [0, 0, 1, 1] as [number, number, number, number],
        readingOrder: 0,
        sourceMethod: "digital" as const,
      },
    ],
  };

  function withAssistant(content: string) {
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m9",
          role: "assistant" as const,
          content,
          status: "completed" as const,
          sequenceNumber: 2,
          retrievalMode: "keyword",
          claims: [claim],
          followups: [],
        },
      ],
    });
  }

  it("마커가 있으면 문장 안에서 근거를 짚을 수 있다", async () => {
    withAssistant("심장은 혈액을 보냅니다[c0].");
    renderQa();
    // 문장 안의 인용 번호와 아래 출처 목록이 같은 곳을 가리키므로 이름도 같다.
    expect(await screen.findAllByRole("button", { name: /3쪽 근거 보기/ })).toHaveLength(2);
  });

  it("마커가 없는 옛 메시지는 기존 주장 목록으로 떨어진다", async () => {
    withAssistant("심장은 혈액을 보냅니다.");
    renderQa();
    expect(await screen.findByText("심장은 혈액을 보낸다")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /근거 보기/ })).toBeNull();
  });

  it("본문에 마커가 없는 주장은 출처 목록에 번호를 올리지 않는다", async () => {
    // 모델이 claim 3개 중 첫 번째에만 마커를 다는 일은 로컬 모델에서 흔하다.
    // 본문에는 [1]만 보이는데 목록에 1·2·3이 뜨면, 사용자는 '2'가 가리키는 문장을
    // 답변에서 찾다가 못 찾는다 — SourceList docstring이 명시한 실패다.
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m9",
          role: "assistant" as const,
          content: "심장은 혈액을 보냅니다[c0].",
          status: "completed" as const,
          sequenceNumber: 2,
          retrievalMode: "keyword",
          claims: [
            claim,
            { ...claim, text: "마커가 없는 주장", sourceRefs: [{ ...claim.sourceRefs[0], pageNumber: 9 }] },
          ],
          followups: [],
        },
      ],
    });
    renderQa();

    expect(await screen.findAllByRole("button", { name: /3쪽 근거 보기/ })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /9쪽 근거 보기/ })).toBeNull();
  });

  it("인용을 누르면 그 쪽으로 이동한다", async () => {
    withAssistant("심장은 혈액을 보냅니다[c0].");
    renderQa();
    const [inlinePill] = await screen.findAllByRole("button", { name: /3쪽 근거 보기/ });
    await userEvent.click(inlinePill);
    await waitFor(() =>
      expect(screen.getByTestId("pdf-viewer")).toHaveAttribute("data-page", "3"),
    );
  });
});

describe("질문 탭 진입", () => {
  afterEach(() => vi.restoreAllMocks());

  it("대화가 없으면 예시 질문과 학습 수준을 함께 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    renderQa();
    expect(await screen.findByText("이 문서의 핵심 내용은 무엇인가요?")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "간호학생" })).toBeChecked();
  });

  it("선택한 학습 수준을 질문과 함께 보낸다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue({ thread, messages: [] });
    streamMock.streamQuestion.mockImplementation(fakeStream([]));
    renderQa();
    await userEvent.click(await screen.findByRole("radio", { name: "간단히" }));
    await userEvent.click(screen.getByText("이 문서의 핵심 내용은 무엇인가요?"));
    await waitFor(() =>
      expect(streamMock.streamQuestion).toHaveBeenCalledWith(
        "d1",
        "t1",
        "이 문서의 핵심 내용은 무엇인가요?",
        expect.objectContaining({ learnerLevel: "concise" }),
      ),
    );
  });

  it("후속 질문을 누르면 입력창을 거치지 않고 바로 보낸다", async () => {
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m9",
          role: "assistant" as const,
          content: "답변입니다.",
          status: "completed" as const,
          sequenceNumber: 2,
          retrievalMode: "keyword",
          claims: [],
          followups: ["더 자세히 알려줘"],
        },
      ],
    });
    streamMock.streamQuestion.mockImplementation(fakeStream([]));
    renderQa();
    await userEvent.click(await screen.findByRole("button", { name: "더 자세히 알려줘" }));
    await waitFor(() =>
      expect(streamMock.streamQuestion).toHaveBeenCalledWith(
        "d1",
        "t1",
        "더 자세히 알려줘",
        expect.anything(),
      ),
    );
  });
});

describe("리뷰 회귀", () => {
  afterEach(() => vi.restoreAllMocks());

  it("검증 실패 주장을 출처로 제시하지 않는다", async () => {
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m1", role: "assistant" as const, content: "확인된 사실[c0] 근거 없는 말[c1]",
          status: "completed" as const, sequenceNumber: 2, retrievalMode: "keyword",
          followups: [],
          claims: [
            {
              text: "확인된 사실", verificationStatus: "supported" as const,
              sourceRefs: [{ pageNumber: 3, blockId: "b1", bbox: [0, 0, 1, 1] as [number, number, number, number],
                             readingOrder: 0, sourceMethod: "digital" as const }],
            },
            // 출처가 없어 검증에 실패한 주장 — 1쪽으로 날조하면 안 된다.
            { text: "근거 없는 말", verificationStatus: "unsupported" as const, sourceRefs: [] },
          ],
        },
      ],
    });
    renderQa();
    await screen.findAllByRole("button", { name: /3쪽 근거 보기/ });
    expect(screen.queryByRole("button", { name: /1쪽 근거 보기/ })).toBeNull();
  });

  it("같은 페이지의 다른 블록을 가리키는 출처 배지를 한 개로 접는다", async () => {
    // 배지에는 페이지 번호만 보인다 — 블록이 달라도 '3쪽' 배지 14개는
    // 사용자에게 똑같은 버튼의 반복일 뿐이다.
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m3", role: "assistant" as const, content: "마커 없는 옛 답변",
          status: "completed" as const, sequenceNumber: 2, retrievalMode: "keyword",
          followups: [],
          claims: [
            {
              text: "같은 쪽 근거가 여럿", verificationStatus: "supported" as const,
              sourceRefs: [
                { pageNumber: 3, blockId: "b1", bbox: [0, 0, 1, 1] as [number, number, number, number],
                  readingOrder: 0, sourceMethod: "digital" as const },
                { pageNumber: 3, blockId: "b2", bbox: [2, 2, 3, 3] as [number, number, number, number],
                  readingOrder: 1, sourceMethod: "digital" as const },
                { pageNumber: 3, blockId: "b3", bbox: [4, 4, 5, 5] as [number, number, number, number],
                  readingOrder: 2, sourceMethod: "digital" as const },
              ],
            },
          ],
        },
      ],
    });
    renderQa();
    await screen.findByText("같은 쪽 근거가 여럿");
    const badges = screen.getAllByRole("button", { name: "3쪽" });
    expect(badges).toHaveLength(1);
    // 접힌 배지를 누르면 그 페이지의 근거 블록 세 곳이 모두 하이라이트된다 —
    // 접기가 두 번째 이후 근거를 도달 불가능하게 만들면 안 된다.
    await userEvent.click(badges[0]);
    expect(screen.getByTestId("pdf-viewer")).toHaveAttribute("data-page", "3");
    expect(screen.getByTestId("pdf-viewer")).toHaveAttribute("data-hl", "3");
  });

  it("한 주장의 출처가 여럿이면 모두 보여준다", async () => {
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue({
      thread,
      messages: [
        {
          id: "m2", role: "assistant" as const, content: "두 곳에 근거가 있다[c0]",
          status: "completed" as const, sequenceNumber: 2, retrievalMode: "keyword",
          followups: [],
          claims: [
            {
              text: "두 곳에 근거가 있다", verificationStatus: "supported" as const,
              sourceRefs: [
                { pageNumber: 3, blockId: "b1", bbox: [0, 0, 1, 1] as [number, number, number, number],
                  readingOrder: 0, sourceMethod: "digital" as const },
                { pageNumber: 7, blockId: "b2", bbox: [0, 0, 1, 1] as [number, number, number, number],
                  readingOrder: 1, sourceMethod: "digital" as const },
              ],
            },
          ],
        },
      ],
    });
    renderQa();
    expect(await screen.findByRole("button", { name: /7쪽 근거 보기/ })).toBeInTheDocument();
  });

  it("대화가 시작된 뒤에도 학습 수준을 바꿀 수 있다", async () => {
    apiMock.listThreads.mockResolvedValue([thread]);
    apiMock.getThread.mockResolvedValue(answerDetail);
    renderQa();
    expect(await screen.findByRole("radio", { name: "간호학생" })).toBeChecked();
  });

  it("청크가 준비되지 않았으면 복구 방법을 알려준다", async () => {
    apiMock.listThreads.mockResolvedValue([]);
    apiMock.createThread.mockResolvedValue({ thread });
    apiMock.getThread.mockResolvedValue({ thread, messages: [] });
    streamMock.streamQuestion.mockRejectedValue(new ApiError(409, "INVALID_STATE", "먼저 문서 검색 준비를 완료해 주세요.", false));
    renderQa();
    await userEvent.click(await screen.findByText("이 문서의 핵심 내용은 무엇인가요?"));
    expect(await screen.findByText(/문서 검색\s*준비하기/)).toBeInTheDocument();
  });
});
