import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ExtractedTable } from "@/components/extracted-table";
import type { ExtractionTable } from "@/types/extraction";

function table(overrides: Partial<ExtractionTable> = {}): ExtractionTable {
  return {
    id: "t1",
    pageNumber: 3,
    tableIndex: 0,
    x0: 72,
    y0: 120,
    x1: 492,
    y1: 210,
    rowCount: 3,
    columnCount: 3,
    cells: [
      ["항목", "정상 범위", "단위"],
      ["심박수", "60~100", "회/분"],
      ["수축기 혈압", "90", "mmHg"],
    ],
    markdownText: "|항목|정상 범위|단위|\n|---|---|---|\n|심박수|60~100|회/분|",
    // 추출기는 성공한 표에 0.7을 상수로 넣는다 — 이 값이 경고를 켜면 안 된다.
    confidence: 0.7,
    extractionStatus: "extracted",
    ...overrides,
  };
}

describe("ExtractedTable", () => {
  it("마크다운 파이프 원문 대신 진짜 표로 그린다", () => {
    render(<ExtractedTable table={table()} onLocate={vi.fn()} />);

    const grid = screen.getByRole("table");
    expect(within(grid).getByRole("columnheader", { name: "정상 범위" })).toBeInTheDocument();
    // 행 머리글이 있어야 옆으로 밀며 읽어도 "무슨 항목"인지 잃지 않는다.
    expect(within(grid).getByRole("rowheader", { name: "심박수" })).toBeInTheDocument();
    expect(within(grid).getByRole("cell", { name: "mmHg" })).toBeInTheDocument();

    // DESIGN.md가 이름을 들어 금지한 것 — 사용자에게 `| 파이프 |`를 보이지 않는다.
    expect(screen.queryByText(/\|---\|/)).not.toBeInTheDocument();
    expect(document.querySelector("pre")).toBeNull();
  });

  it("정상 추출된 표에는 경고를 붙이지 않는다", () => {
    // 이전 화면은 confidence < 0.8로 판정해 모든 표에 경고가 떴고, 그래서 경고가
    // 아무 정보도 주지 못한 채 표마다 자리만 차지했다.
    render(<ExtractedTable table={table()} onLocate={vi.fn()} />);
    expect(screen.queryByText(/읽어내지 못했어요/)).not.toBeInTheDocument();
  });

  it("읽어내지 못한 표는 경고와 원문 확인 경로를 준다", () => {
    render(
      <ExtractedTable
        table={table({ cells: [], rowCount: 0, columnCount: 0, extractionStatus: "failed" })}
        onLocate={vi.fn()}
      />,
    );
    expect(screen.getByText(/읽어내지 못했어요/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("행·열 수는 머리글을 뺀 실제 자료 기준으로 센다", () => {
    // API의 rowCount는 머리글 행을 포함해 한 행 많다.
    render(<ExtractedTable table={table()} onLocate={vi.fn()} />);
    expect(screen.getByText("2행 × 3열")).toBeInTheDocument();
  });

  it("제목을 누르면 원문 위치로 보낸다", async () => {
    const onLocate = vi.fn();
    render(<ExtractedTable table={table()} onLocate={onLocate} />);
    await userEvent.click(screen.getByRole("button", { name: "3쪽의 표 1" }));
    expect(onLocate).toHaveBeenCalledOnce();
  });
});
