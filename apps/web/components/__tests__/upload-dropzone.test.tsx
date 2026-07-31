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
  const input = screen.getByLabelText("PDF 파일 선택", { selector: "input" });
  return input as HTMLInputElement;
}

const created: DocumentCreated = {
  id: "doc-1",
  title: "sample.pdf",
  processingStatus: "queued",
  processingStage: "file_validation",
  processingProgress: 0,
  duplicate: false,
};

describe("UploadDropzone", () => {
  beforeEach(() => {
    uploadDocument.mockReset();
  });

  it("PDF가 아닌 파일을 쉬운 한국어로 거부한다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    const txt = new File(["hello"], "notes.txt", { type: "text/plain" });
    await userEvent.upload(getInput(), txt, { applyAccept: false });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "PDF 파일만 업로드할 수 있습니다",
    );
  });

  it("최대 크기를 초과한 파일을 거부한다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile("big.pdf", 801 * 1024 * 1024));
    expect(await screen.findByRole("alert")).toHaveTextContent("최대 크기");
  });

  it("개인정보 확인 체크박스 없이는 업로드 버튼이 비활성화된다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile());
    const uploadButton = screen.getByRole("button", { name: "업로드" });
    expect(uploadButton).toBeDisabled();

    await userEvent.click(
      screen.getByRole("checkbox", {
        name: /실제 환자를 알아볼 수 있는 정보/,
      }),
    );
    expect(uploadButton).toBeEnabled();
  });

  it("체크박스를 켜지 않으면 클릭해도 업로드가 실행되지 않는다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile());
    await userEvent.click(screen.getByRole("button", { name: "업로드" }));
    expect(uploadDocument).not.toHaveBeenCalled();
  });

  it("키보드만으로 파일 선택과 업로드가 가능하다", async () => {
    uploadDocument.mockReturnValue({ promise: Promise.resolve(created), abort: vi.fn() });
    const onUploaded = vi.fn();
    render(<UploadDropzone onUploaded={onUploaded} />);

    // 파일 선택 버튼과 input이 키보드 접근 가능
    const selectButton = screen.getByRole("button", { name: "PDF 파일 선택" });
    selectButton.focus();
    expect(selectButton).toHaveFocus();

    await userEvent.upload(getInput(), pdfFile());
    await userEvent.keyboard("{Tab}");
    // 체크박스 → 스페이스로 체크 → 업로드 버튼 엔터
    const checkbox = screen.getByRole("checkbox");
    checkbox.focus();
    await userEvent.keyboard(" ");
    const uploadButton = screen.getByRole("button", { name: "업로드" });
    uploadButton.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(created));
  });

  it("선택한 파일명과 크기를 보여준다", async () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile("lecture.pdf"));
    expect(screen.getByText(/lecture\.pdf/)).toBeInTheDocument();
  });

  it("업로드 중 진행률(텍스트+막대)과 취소 버튼을 보여준다", async () => {
    let progressCb: (p: number) => void = () => {};
    uploadDocument.mockImplementation(
      (_f: File, _t: undefined, onProgress: (p: number) => void) => {
        progressCb = onProgress;
        return { promise: new Promise(() => {}), abort: vi.fn() };
      },
    );
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile());
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "업로드" }));

    progressCb(42);
    await waitFor(() =>
      expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42"),
    );
    expect(screen.getByText("42% 완료")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "취소" })).toBeInTheDocument();
  });

  it("업로드 실패 시 이해 가능한 한국어 메시지를 보여준다 (내부 예외 노출 금지)", async () => {
    const rejected = Promise.reject(
      new Error("연결에 문제가 생겨 파일을 올리지 못했습니다. 다시 시도해 주세요."),
    );
    rejected.catch(() => {}); // 테스트 러너의 unhandled rejection 경고 방지
    uploadDocument.mockReturnValue({ promise: rejected, abort: vi.fn() });
    render(<UploadDropzone onUploaded={vi.fn()} />);
    await userEvent.upload(getInput(), pdfFile());
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "업로드" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("다시 시도해 주세요");
    expect(alert.textContent).not.toMatch(/Traceback|Error:|stack/i);
  });

  it("업로드 금지 자료 예시를 안내한다", () => {
    render(<UploadDropzone onUploaded={vi.fn()} />);
    expect(screen.getByText(/이런 자료는 올리면 안 돼요/)).toBeInTheDocument();
    expect(screen.getByText(/실제 환자 이름/)).toBeInTheDocument();
  });
});
