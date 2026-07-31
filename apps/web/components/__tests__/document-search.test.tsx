import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentSearch } from "@/components/document-search";

const apiMock = vi.hoisted(() => ({
  getChunkStatus: vi.fn(),
  rebuildChunks: vi.fn(),
  searchDocument: vi.fn(),
}));
vi.mock("@/lib/api/search", () => apiMock);

const readyStatus = {
  chunkCount: 3,
  lastRebuiltAt: "2026-07-01T00:00:00Z",
  jobStatus: "succeeded",
  embeddingAvailable: false,
};

const notPreparedStatus = {
  chunkCount: 0,
  lastRebuiltAt: null,
  jobStatus: null,
  embeddingAvailable: false,
};

const sampleResult = {
  chunkId: "c1",
  preview: "심장은 우리 몸의 중심 장기입니다.",
  sectionTitle: "순환계",
  pageStart: 2,
  pageEnd: 2,
  sourceRefs: [
    {
      pageNumber: 2,
      blockId: "b1",
      bbox: [10, 20, 100, 40] as [number, number, number, number],
      readingOrder: 0,
      sourceMethod: "digital" as const,
    },
  ],
  matchType: "keyword" as const,
};

function renderSearch(onNavigate = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <DocumentSearch documentId="d1" onNavigate={onNavigate} />
    </QueryClientProvider>,
  );
  return { ...utils, onNavigate };
}

describe("DocumentSearch", () => {
  afterEach(() => vi.restoreAllMocks());

  it("청크가 없으면 준비 버튼을 보여준다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(notPreparedStatus);
    renderSearch();
    expect(await screen.findByRole("button", { name: "문서 검색 준비하기" })).toBeInTheDocument();
  });

  it("상태 조회가 실패하면 다시 시도 가능한 오류를 보여준다", async () => {
    apiMock.getChunkStatus.mockRejectedValue(new Error("network down"));
    renderSearch();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("검색 준비 상태를 확인하지 못했습니다.");
  });

  it("빈 검색어는 검증 안내를 보여주고 검색을 요청하지 않는다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    renderSearch();
    await screen.findByRole("searchbox");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    expect(await screen.findByText("검색어를 입력해 주세요.")).toBeInTheDocument();
    expect(apiMock.searchDocument).not.toHaveBeenCalled();
  });

  it("검색 결과가 없으면 안내 문구를 보여준다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    apiMock.searchDocument.mockResolvedValue([]);
    renderSearch();

    await userEvent.type(await screen.findByRole("searchbox"), "존재하지않는단어");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    expect(await screen.findByText("검색 결과가 없어요. 다른 단어로 찾아보세요.")).toBeInTheDocument();
  });

  it("검색 실패 시 다시 시도할 수 있다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    apiMock.searchDocument.mockRejectedValue(new Error("boom"));
    renderSearch();

    await userEvent.type(await screen.findByRole("searchbox"), "심장");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    expect(await screen.findByText("검색하지 못했습니다.")).toBeInTheDocument();
  });

  it("의미 검색이 비활성 상태이고 결과가 있으면 단어 검색 결과임을 알린다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    apiMock.searchDocument.mockResolvedValue([sampleResult]);
    renderSearch();

    await userEvent.type(await screen.findByRole("searchbox"), "심장");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    expect(
      await screen.findByText("의미 검색을 사용할 수 없어 단어 검색 결과만 표시합니다."),
    ).toBeInTheDocument();
  });

  it("의미 검색이 비활성 상태이면 결과가 0건이어도 단어 검색 결과임을 알린다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    apiMock.searchDocument.mockResolvedValue([]);
    renderSearch();

    await userEvent.type(await screen.findByRole("searchbox"), "존재하지않는단어");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    expect(await screen.findByText("검색 결과가 없어요. 다른 단어로 찾아보세요.")).toBeInTheDocument();
    expect(
      screen.getByText("의미 검색을 사용할 수 없어 단어 검색 결과만 표시합니다."),
    ).toBeInTheDocument();
  });

  it("재생성 작업이 대기(queued) 상태여도 준비 버튼을 비활성화한다", async () => {
    apiMock.getChunkStatus.mockResolvedValue({ ...notPreparedStatus, jobStatus: "queued" });
    renderSearch();

    expect(await screen.findByRole("button", { name: "문서 검색 준비하기" })).toBeDisabled();
    expect(screen.getByText("준비하는 중이에요…")).toBeInTheDocument();
  });

  it("결과를 클릭하면 페이지 번호와 bbox로 이동한다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    apiMock.searchDocument.mockResolvedValue([sampleResult]);
    const { onNavigate } = renderSearch();

    await userEvent.type(await screen.findByRole("searchbox"), "심장");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    const resultButton = await screen.findByText(sampleResult.preview);
    await userEvent.click(resultButton);

    expect(onNavigate).toHaveBeenCalledWith(2, [10, 20, 100, 40]);
  });

  it("포트·모델명 등 기술 용어를 노출하지 않는다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(readyStatus);
    apiMock.searchDocument.mockResolvedValue([sampleResult]);
    const { container } = renderSearch();

    await userEvent.type(await screen.findByRole("searchbox"), "심장");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));
    await screen.findByText(sampleResult.preview);

    const text = container.textContent ?? "";
    expect(text).not.toMatch(/localhost|127\.0\.0\.1|:\d{4,5}|bm25|embedding|deterministic/i);
  });

  it("문서 검색 준비 버튼을 누르면 재생성 요청을 보낸다", async () => {
    apiMock.getChunkStatus.mockResolvedValue(notPreparedStatus);
    apiMock.rebuildChunks.mockResolvedValue({ jobId: "j1", started: true });
    renderSearch();

    await userEvent.click(await screen.findByRole("button", { name: "문서 검색 준비하기" }));
    await waitFor(() => expect(apiMock.rebuildChunks).toHaveBeenCalledWith("d1"));
  });
});
