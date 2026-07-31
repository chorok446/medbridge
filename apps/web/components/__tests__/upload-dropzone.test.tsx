import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UploadDropzone } from "@/components/upload-dropzone";
import type { DocumentCreated } from "@/types/api";

const uploadDocument = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/documents", () => ({ uploadDocument }));

function pdfFile(name = "sample.pdf", size?: number): File {
  const f = new File(["%PDF-1.4 test"], name, { type: "application/pdf" });
  if (size !== undefined) Object.defineProperty(f, "size", { value: size });
  return f;
}

function getInput(): HTMLInputElement {
  const input = document.querySelector('input[type="file"]');
  if (!input) throw new Error("file input not found");
  return input as HTMLInputElement;
}

describe("UploadDropzone", () => {
  beforeEach(() => {
    uploadDocument.mockReset();
  });

  it("PDF가 아닌 파일을 거부한다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    const txt = new File(["hello"], "notes.txt", { type: "text/plain" });
    await userEvent.upload(getInput(), txt, { applyAccept: false });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "PDF 파일만 업로드할 수 있습니다",
    );
  });

  it("최대 크기를 초과한 파일을 거부한다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile("big.pdf", 51 * 1024 * 1024));
    expect(await screen.findByRole("alert")).toHaveTextContent("최대 크기");
  });

  it("선택한 파일명과 크기를 보여준다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile("lecture.pdf"));
    expect(screen.getByText(/lecture\.pdf/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "업로드" })).toBeInTheDocument();
  });

  it("업로드 중 진행률과 취소 버튼을 보여준다", async () => {
    let progressCb: (p: number) => void = () => {};
    uploadDocument.mockImplementation((_f: File, _t: undefined, onProgress: (p: number) => void) => {
      progressCb = onProgress;
      return { promise: new Promise(() => {}), abort: vi.fn() };
    });
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile());
    await userEvent.click(screen.getByRole("button", { name: "업로드" }));

    progressCb(42);
    await waitFor(() =>
      expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42"),
    );
    expect(screen.getByRole("button", { name: "취소" })).toBeInTheDocument();
  });

  it("업로드 성공 시 onUploaded를 호출한다", async () => {
    const created: DocumentCreated = {
      id: "doc-1",
      title: "sample.pdf",
      processingStatus: "queued",
      processingStage: "file_validation",
      processingProgress: 0,
      duplicate: false,
    };
    uploadDocument.mockReturnValue({ promise: Promise.resolve(created), abort: vi.fn() });
    const onUploaded = vi.fn();
    render(<UploadDropzone onUploaded={onUploaded} />);
    await userEvent.upload(getInput(), pdfFile());
    await userEvent.click(screen.getByRole("button", { name: "업로드" }));
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(created));
  });

  it("업로드 실패 시 오류를 보여준다", async () => {
    uploadDocument.mockReturnValue({
      promise: Promise.reject(new Error("네트워크 오류로 업로드에 실패했습니다.")),
      abort: vi.fn(),
    });
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile());
    await userEvent.click(screen.getByRole("button", { name: "업로드" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("네트워크 오류");
  });
});
