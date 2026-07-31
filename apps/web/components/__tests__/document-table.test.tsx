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

const noop = { onRetry: vi.fn(), onRename: vi.fn(), onDelete: vi.fn(), onReport: vi.fn() };

describe("DocumentTable", () => {
  it("빈 목록이면 새 문서 추가 행동을 보여준다", () => {
    render(<DocumentTable items={[]} {...noop} />);
    expect(screen.getByText("아직 학습자료가 없어요")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "PDF 추가하기" })).toBeInTheDocument();
  });

  it("문서에 제목·파일명·사용자 문구 상태를 보여준다", () => {
    render(<DocumentTable items={[doc()]} {...noop} />);
    expect(screen.getByText("심부전 논문")).toBeInTheDocument();
    expect(screen.getByText(/heart-failure\.pdf/)).toBeInTheDocument();
    expect(screen.getByText("학습 준비 완료")).toBeInTheDocument();
  });

  it("개발 용어와 내부 식별자를 표시하지 않는다", () => {
    const { container } = render(
      <DocumentTable items={[doc({ processingStatus: "validating" })]} {...noop} />,
    );
    const text = container.textContent ?? "";
    for (const term of [
      "SHA",
      "UUID",
      "MIME",
      "storage",
      "correlation",
      "queue",
      "worker",
      "d1", // 내부 ID
      "validating", // 내부 enum 원문
    ]) {
      expect(text).not.toContain(term);
    }
    // 대신 사용자 문구가 보인다
    expect(text).toContain("파일을 확인하는 중");
  });

  it("실패 문서는 쉬운 원인 설명과 다시 시도·오류 신고 버튼을 보여준다", () => {
    render(
      <DocumentTable items={[doc({ processingStatus: "failed", failureCode: "ENCRYPTED_PDF" })]} {...noop} />,
    );
    expect(screen.getByText("파일을 처리하지 못했습니다.")).toBeInTheDocument();
    expect(screen.getByText(/암호로 보호되어 있습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "오류 신고" })).toBeInTheDocument();
  });

  it("처리 중 상태는 텍스트와 진행률로 표시한다", () => {
    render(
      <DocumentTable
        items={[doc({ processingStatus: "validating", processingProgress: 60 })]}
        {...noop}
      />,
    );
    expect(screen.getByText(/파일을 확인하는 중/)).toBeInTheDocument();
    expect(screen.getByText(/60%/)).toBeInTheDocument();
  });

  it("행동 버튼(열기·이름 변경·삭제)이 콜백을 호출한다", async () => {
    const onRename = vi.fn();
    const onDelete = vi.fn();
    render(<DocumentTable items={[doc()]} {...noop} onRename={onRename} onDelete={onDelete} />);
    expect(screen.getByRole("link", { name: "열기" })).toHaveAttribute(
      "href",
      "/documents/view?id=d1",
    );
    await userEvent.click(screen.getByRole("button", { name: "이름 변경" }));
    expect(onRename).toHaveBeenCalledWith("d1", "심부전 논문");
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));
    expect(onDelete).toHaveBeenCalledWith("d1");
  });

  it("다시 시도 버튼이 onRetry를 호출한다", async () => {
    const onRetry = vi.fn();
    render(
      <DocumentTable items={[doc({ processingStatus: "failed" })]} {...noop} onRetry={onRetry} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(onRetry).toHaveBeenCalledWith("d1");
  });
});
