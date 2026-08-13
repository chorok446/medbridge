import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StartupGate } from "@/components/startup-gate";

const tauriMock = vi.hoisted(() => ({
  sidecarStatus: vi.fn(),
  relaunchApp: vi.fn(),
  saveErrorReport: vi.fn(),
}));

vi.mock("@/lib/tauri", () => ({
  useIsTauri: () => true,
  sidecarStatus: tauriMock.sidecarStatus,
  relaunchApp: tauriMock.relaunchApp,
  saveErrorReport: tauriMock.saveErrorReport,
}));

describe("StartupGate (데스크톱 sidecar 지속 감시)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    tauriMock.sidecarStatus.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("준비 완료 뒤 sidecar가 죽으면 전역 복구 화면으로 돌아간다", async () => {
    tauriMock.sidecarStatus.mockResolvedValueOnce("ready").mockResolvedValue("failed");
    render(
      <StartupGate>
        <p>메인 화면</p>
      </StartupGate>,
    );

    await act(async () => vi.advanceTimersByTimeAsync(700));
    expect(screen.getByText("메인 화면")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(700));
    expect(screen.getByRole("heading", { name: "MedBridge를 시작하지 못했습니다." }))
      .toBeInTheDocument();
    expect(screen.queryByText("메인 화면")).not.toBeInTheDocument();
  });
});
