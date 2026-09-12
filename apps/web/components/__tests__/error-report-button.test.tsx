import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorReportButton } from "@/components/error-report-button";

const tauriMock = vi.hoisted(() => ({
  useIsTauri: vi.fn(() => true),
  saveErrorReport: vi.fn(async () => true),
}));
vi.mock("@/lib/tauri", () => tauriMock);

describe("ErrorReportButton", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    tauriMock.useIsTauri.mockReturnValue(true);
    tauriMock.saveErrorReport.mockResolvedValue(true);
  });
  afterEach(() => vi.restoreAllMocks());

  it("저장에 성공하면 알린다 (기본형)", async () => {
    render(<ErrorReportButton />);

    await userEvent.click(screen.getByRole("button", { name: "오류 정보 저장" }));

    expect(await screen.findByRole("status")).toHaveTextContent("오류 정보를 저장했습니다.");
    expect(tauriMock.saveErrorReport).toHaveBeenCalledOnce();
  });

  it("저장에 성공하면 알린다 (오류 안내에 곁들이는 형)", async () => {
    render(<ErrorReportButton variant="inline" />);

    await userEvent.click(screen.getByRole("button", { name: "오류 정보 저장" }));

    expect(await screen.findByRole("status")).toHaveTextContent("저장했어요");
  });

  it("저장에 실패하면 실패했다고 말한다", async () => {
    // 지금까지는 실패해도 아무 일도 일어나지 않았다. 사용자는 버튼이 죽은 줄
    // 알고 계속 누른다 — PRODUCT.md "오류는 다음 행동과 함께"에 걸린다.
    tauriMock.saveErrorReport.mockResolvedValue(false);
    render(<ErrorReportButton />);

    await userEvent.click(screen.getByRole("button", { name: "오류 정보 저장" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("저장하지 못했");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("예외가 나도 조용히 넘어가지 않는다", async () => {
    tauriMock.saveErrorReport.mockRejectedValue(new Error("disk full"));
    render(<ErrorReportButton />);

    await userEvent.click(screen.getByRole("button", { name: "오류 정보 저장" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("저장하지 못했");
    // 기술 원문은 보이지 않는다.
    expect(alert.textContent).not.toContain("disk full");
  });

  it("브라우저 모드에서는 눌러도 안 되는 버튼을 두지 않는다", () => {
    // 저장할 진단 파일 자체가 데스크톱에만 있다.
    tauriMock.useIsTauri.mockReturnValue(false);
    render(<ErrorReportButton />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/데스크톱 앱에서 사용할 수 있어요/)).toBeInTheDocument();
  });

  it("브라우저 모드에서 곁들임형은 아무것도 그리지 않는다", () => {
    // 오류 안내 문장 안에서는 "쓸 수 없다"는 말조차 군더더기다.
    tauriMock.useIsTauri.mockReturnValue(false);
    const { container } = render(<ErrorReportButton variant="inline" />);

    expect(container).toBeEmptyDOMElement();
  });
});
