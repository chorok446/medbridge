import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PdfViewer } from "@/components/pdf-viewer";

const pdfjs = vi.hoisted(() => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: vi.fn(() => ({
    promise: new Promise(() => undefined),
    destroy: vi.fn().mockResolvedValue(undefined),
  })),
}));
vi.mock("pdfjs-dist", () => pdfjs);

describe("PdfViewer 로컬 렌더링 리소스", () => {
  it("한글 매핑·기본 글꼴·이미지 디코더를 앱 자체 주소에서 읽는다", async () => {
    render(
      <PdfViewer
        fileUrl="blob:synthetic-document"
        page={1}
        pageCount={2}
        onPageChange={vi.fn()}
        highlights={[]}
      />,
    );

    await waitFor(() =>
      expect(pdfjs.getDocument).toHaveBeenCalledWith({
        url: "blob:synthetic-document",
        cMapUrl: new URL("/pdfjs/cmaps/", window.location.href).href,
        cMapPacked: true,
        standardFontDataUrl: new URL("/pdfjs/standard_fonts/", window.location.href).href,
        wasmUrl: new URL("/pdfjs/wasm/", window.location.href).href,
      }),
    );
  });
});
