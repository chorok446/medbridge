import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
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
    // vi.mock 팩토리가 만든 vi.fn()의 호출 기록은 restoreAllMocks가 지우지 않는다.
    // 지우지 않으면 앞 테스트의 호출이 다음 테스트로 새어, "호출되지 않았다"를
    // 확인하는 단언이 남의 호출을 보고 실패한다.
    vi.clearAllMocks();
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

  it("삭제 전 앱 안의 확인 대화상자를 띄우고, 취소하면 삭제하지 않는다", async () => {
    // window.confirm이 아니라 앱 다이얼로그다 — DESIGN.md가 시스템 네이티브
    // confirm/prompt로 앱의 목소리를 끊지 말라고 못박았다.
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("삭제할까요?");
    // 무엇을 지우는지 이름이 보여야 잘못 누른 것을 알아차린다.
    expect(dialog).toHaveTextContent("심전도 강의");

    await userEvent.click(within(dialog).getByRole("button", { name: "취소" }));
    expect(apiMock.deleteDocument).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("확인하면 삭제를 실행한다", async () => {
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));

    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "삭제" }));

    await waitFor(() => expect(apiMock.deleteDocument).toHaveBeenCalled());
    expect(apiMock.deleteDocument.mock.calls[0][0]).toBe("d1");
  });

  it("이름 변경은 입력값으로 renameDocument를 호출한다", async () => {
    apiMock.renameDocument.mockResolvedValue(listing.items[0]);
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "이름 변경" }));

    const input = await screen.findByLabelText("자료 이름");
    // 현재 이름이 채워진 채로 열려야 일부만 고칠 수 있다.
    expect(input).toHaveValue("심전도 강의");
    await userEvent.clear(input);
    await userEvent.type(input, "새 제목");
    await userEvent.click(screen.getByRole("button", { name: "바꾸기" }));

    await waitFor(() => expect(apiMock.renameDocument).toHaveBeenCalledWith("d1", "새 제목"));
  });

  it("이름을 바꾸지 않고 확인하면 요청을 보내지 않는다", async () => {
    renderPage();
    await screen.findByText("심전도 강의");
    await userEvent.click(screen.getByRole("button", { name: "이름 변경" }));
    await screen.findByLabelText("자료 이름");
    await userEvent.click(screen.getByRole("button", { name: "바꾸기" }));

    expect(apiMock.renameDocument).not.toHaveBeenCalled();
  });
});
