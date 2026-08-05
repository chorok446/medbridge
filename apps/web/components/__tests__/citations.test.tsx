import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type CitationSlots, CitedText, SourceList } from "@/components/citations";
import type { CitationSource } from "@/lib/citations";

const PAGE3: CitationSource = {
  pageNumber: 3,
  sectionTitle: "순환계",
  sourceMethod: "digital",
  bbox: [0, 0, 1, 1],
};
const PAGE7: CitationSource = {
  pageNumber: 7,
  sectionTitle: null,
  sourceMethod: "ocr",
  bbox: [1, 1, 2, 2],
};
const SLOTS: CitationSlots = [[PAGE3], [PAGE7]];

describe("CitedText", () => {
  it("renders a clickable number for each marker", () => {
    const onNavigate = vi.fn();
    render(
      <CitedText
        content="심장은 혈액을 보냅니다[c0]."
        sources={SLOTS}
        onNavigate={onNavigate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /3쪽 근거 보기/ }));
    expect(onNavigate).toHaveBeenCalledWith(PAGE3);
  });

  it("numbers citations from 1 for the reader", () => {
    render(<CitedText content="답변[c1]" sources={SLOTS} onNavigate={() => {}} />);
    expect(screen.getByRole("button", { name: /7쪽 근거 보기/ })).toHaveTextContent("2");
  });

  it("renders prose unchanged when there is no marker", () => {
    render(<CitedText content="마커 없는 답변" sources={SLOTS} onNavigate={() => {}} />);
    expect(screen.getByText("마커 없는 답변")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("drops a marker whose slot has no verified source", () => {
    // 눌러도 갈 곳이 없는 번호는 '근거가 있다'는 잘못된 신호만 준다.
    render(<CitedText content="답변[c0]" sources={[null]} onNavigate={() => {}} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("답변")).toBeInTheDocument();
  });
});

describe("SourceList", () => {
  const groups = [
    { number: 1, refs: [PAGE3] },
    { number: 2, refs: [PAGE7] },
  ];

  it("labels a scanned source in plain Korean", () => {
    render(<SourceList groups={groups} onNavigate={() => {}} />);
    expect(screen.getByText(/스캔 인식/)).toBeInTheDocument();
    // 기술 용어는 화면에 내지 않는다 (PRODUCT.md anti-reference)
    expect(screen.queryByText(/OCR/i)).toBeNull();
  });

  it("shows the section title when present", () => {
    render(<SourceList groups={groups} onNavigate={() => {}} />);
    expect(screen.getByText(/순환계/)).toBeInTheDocument();
  });

  it("navigates to the picked source", () => {
    const onNavigate = vi.fn();
    render(<SourceList groups={groups} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: /7쪽 근거 보기/ }));
    expect(onNavigate).toHaveBeenCalledWith(PAGE7);
  });

  it("lists every source of a claim, not just the first", () => {
    render(
      <SourceList groups={[{ number: 1, refs: [PAGE3, PAGE7] }]} onNavigate={() => {}} />,
    );
    expect(screen.getByRole("button", { name: /3쪽 근거 보기/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /7쪽 근거 보기/ })).toBeInTheDocument();
  });

  it("omits numbers when the body has no markers to match", () => {
    render(<SourceList groups={[{ number: null, refs: [PAGE3] }]} onNavigate={() => {}} />);
    const btn = screen.getByRole("button", { name: /3쪽 근거 보기/ });
    expect(btn.textContent).not.toMatch(/^1/);
  });

  it("renders nothing without sources", () => {
    const { container } = render(<SourceList groups={[]} onNavigate={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("collapses sources that look identical on screen", () => {
    // 한 주장이 같은 페이지의 블록 여러 개에 근거를 두면 화면에는 같은 줄이
    // 수십 번 반복된다 — 사용자는 어느 줄도 구분할 수 없다. 첫 출처만 남긴다.
    const onNavigate = vi.fn();
    render(
      <SourceList
        groups={[
          {
            number: 1,
            refs: [
              { ...PAGE3, bbox: [0, 0, 1, 1] },
              { ...PAGE3, bbox: [2, 2, 3, 3] },
              { ...PAGE3, bbox: [4, 4, 5, 5] },
            ],
          },
        ]}
        onNavigate={onNavigate}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: /3쪽 근거 보기/ });
    expect(buttons).toHaveLength(1);
    // 이동은 첫 출처의 위치로 — 목록이 접혀도 갈 곳은 있어야 한다.
    fireEvent.click(buttons[0]);
    expect(onNavigate).toHaveBeenCalledWith({ ...PAGE3, bbox: [0, 0, 1, 1] });
  });

  it("keeps same-page sources apart when their section titles differ", () => {
    render(
      <SourceList
        groups={[
          {
            number: 1,
            refs: [PAGE3, { ...PAGE3, sectionTitle: "호흡계", bbox: [9, 9, 10, 10] }],
          },
        ]}
        onNavigate={() => {}}
      />,
    );
    expect(screen.getAllByRole("button", { name: /3쪽 근거 보기/ })).toHaveLength(2);
    expect(screen.getByText(/호흡계/)).toBeInTheDocument();
  });
});
