import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CitedText, SourceList } from "@/components/citations";
import type { CitationSource } from "@/lib/citations";

const SOURCES: CitationSource[] = [
  { pageNumber: 3, sectionTitle: "순환계", sourceMethod: "digital", bbox: [0, 0, 1, 1] },
  { pageNumber: 7, sectionTitle: null, sourceMethod: "ocr", bbox: [1, 1, 2, 2] },
];

describe("CitedText", () => {
  it("renders a clickable number for each marker", () => {
    const onNavigate = vi.fn();
    render(
      <CitedText
        content="심장은 혈액을 보냅니다[c0]."
        sources={SOURCES}
        onNavigate={onNavigate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /3쪽 근거 보기/ }));
    expect(onNavigate).toHaveBeenCalledWith(SOURCES[0]);
  });

  it("numbers citations from 1 for the reader", () => {
    render(<CitedText content="답변[c1]" sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByRole("button", { name: /7쪽 근거 보기/ })).toHaveTextContent("2");
  });

  it("renders prose unchanged when there is no marker", () => {
    render(<CitedText content="마커 없는 답변" sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByText("마커 없는 답변")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("SourceList", () => {
  it("labels a scanned source in plain Korean", () => {
    render(<SourceList sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByText(/스캔 인식/)).toBeInTheDocument();
    // 기술 용어는 화면에 내지 않는다 (PRODUCT.md anti-reference)
    expect(screen.queryByText(/OCR/i)).toBeNull();
  });

  it("shows the section title when present", () => {
    render(<SourceList sources={SOURCES} onNavigate={() => {}} />);
    expect(screen.getByText(/순환계/)).toBeInTheDocument();
  });

  it("navigates to the picked source", () => {
    const onNavigate = vi.fn();
    render(<SourceList sources={SOURCES} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: /7쪽/ }));
    expect(onNavigate).toHaveBeenCalledWith(SOURCES[1]);
  });

  it("renders nothing without sources", () => {
    const { container } = render(<SourceList sources={[]} onNavigate={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
