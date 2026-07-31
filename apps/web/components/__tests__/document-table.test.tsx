import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DocumentTable } from "@/components/document-table";
import type { DocumentSummary } from "@/types/api";

function doc(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: "d1",
    title: "심부전 논문",
    originalFilename: "heart-failure.pdf",
    fileSize: 1024 * 500,
    pageCount: 12,
    documentType: "unknown",
    processingStatus: "ready",
    processingStage: "file_validation",
    processingProgress: 100,
    failureCode: null,
    failureMessage: null,
    createdAt: "2026-07-31T10:00:00Z",
    updatedAt: "2026-07-31T10:01:00Z",
    ...overrides,
  };
}

describe("DocumentTable", () => {
  it("빈 목록이면 안내 문구를 보여준다", () => {
    render(<DocumentTable items={[]} onRetry={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText("아직 업로드한 문서가 없습니다")).toBeInTheDocument();
  });

  it("문서 행에 제목·파일명·상태를 보여준다", () => {
    render(<DocumentTable items={[doc()]} onRetry={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText("심부전 논문")).toBeInTheDocument();
    expect(screen.getByText("heart-failure.pdf")).toBeInTheDocument();
    expect(screen.getByText("준비 완료")).toBeInTheDocument();
  });

  it("실패 문서는 실패 사유와 재시도 버튼을 보여준다", () => {
    render(
      <DocumentTable
        items={[
          doc({
            processingStatus: "failed",
            failureCode: "ENCRYPTED_PDF",
            failureMessage: "암호화된 PDF는 업로드할 수 없습니다.",
          }),
        ]}
        onRetry={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText(/암호화된 PDF/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "재시도" })).toBeInTheDocument();
  });

  it("재시도 버튼이 onRetry를 호출한다", async () => {
    const onRetry = vi.fn();
    render(
      <DocumentTable
        items={[doc({ processingStatus: "failed" })]}
        onRetry={onRetry}
        onDelete={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "재시도" }));
    expect(onRetry).toHaveBeenCalledWith("d1");
  });

  it("삭제 버튼이 onDelete를 호출한다", async () => {
    const onDelete = vi.fn();
    render(<DocumentTable items={[doc()]} onRetry={vi.fn()} onDelete={onDelete} />);
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));
    expect(onDelete).toHaveBeenCalledWith("d1");
  });

  it("처리 중 문서는 진행 단계를 보여준다", () => {
    render(
      <DocumentTable
        items={[doc({ processingStatus: "validating", processingProgress: 60 })]}
        onRetry={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText(/파일 검증 중/)).toBeInTheDocument();
  });
});
