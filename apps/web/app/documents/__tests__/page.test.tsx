import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DocumentsPage from "@/app/documents/page";
import type { DocumentList } from "@/types/api";

const apiMock = vi.hoisted(() => ({
  listDocuments: vi.fn(),
  deleteDocument: vi.fn(),
  retryDocument: vi.fn(),
  renameDocument: vi.fn(),
  reportError: vi.fn(),
  uploadDocument: vi.fn(),
}));
vi.mock("@/lib/api/documents", () => apiMock);

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DocumentsPage />
    </QueryClientProvider>,
  );
}

const listing: DocumentList = {
  items: [
    {
      id: "d1",
      title: "심전도 강의",
      originalFilename: "ecg.pdf",
      fileSize: 1000,
      pageCount: 3,
      documentType: "unknown",
      processingStatus: "ready",
      processingStage: "file_validation",
      processingProgress: 100,
      failureCode: null,
      failureMessage: null,
      createdAt: "2026-07-31T10:00:00Z",
      updatedAt: "2026-07-31T10:01:00Z",
    },
  ],
  nextCursor: null,
};

describe("DocumentsPage", () => {
  beforeEach(() => {
    apiMock.listDocuments.mockResolvedValue(listing);
    apiMock.deleteDocument.mockResolvedValue({ id: "d1", processingStatus: "deleted" });
  });
  afterEach(() => vi.restoreAllMocks());

  it("로딩 후 문서 목록을 보여준다", async () => {
    renderPage();
    expect(screen.getByText(/불러오는 중/)).toBeInTheDocument();
    expect(await screen.findByText("심전도 강의")).toBeInTheDocument();
  });

  it("목록 조회 실패 시 사용자 문구와 다시 시도 버튼을 보여준다", async () => {
    apiMock.listDocuments.mockRejectedValue(new Error("ECONNREFUSED 127.0.0.1"));
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("학습자료 목록을 불러오지 못했습니다.");
    // 내부 오류 원문이 노출되지 않는다
    expect(alert.textContent).not.toContain("ECONNREFUSED");
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
  });

  it("삭제 전 확인 모달을 띄우고, 취소하면 삭제하지 않는다", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(apiMock.deleteDocument).not.toHaveBeenCalled();
  });

  it("확인하면 삭제를 실행한다", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(apiMock.deleteDocument).toHaveBeenCalled());
    expect(apiMock.deleteDocument.mock.calls[0][0]).toBe("d1");
  });

  it("이름 변경은 입력값으로 renameDocument를 호출한다", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("새 제목");
    apiMock.renameDocument.mockResolvedValue(listing.items[0]);
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "이름 변경" }));
    await waitFor(() => expect(apiMock.renameDocument).toHaveBeenCalledWith("d1", "새 제목"));
  });
});
