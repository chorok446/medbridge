import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UpdateManager } from "@/components/update-manager";

const tauriMock = vi.hoisted(() => ({
  isTauri: vi.fn(() => true),
  useIsTauri: vi.fn(() => true),
  relaunchApp: vi.fn(async () => undefined),
  saveErrorReport: vi.fn(async () => true),
}));
vi.mock("@/lib/tauri", () => tauriMock);

const systemMock = vi.hoisted(() => ({
  prepareUpdate: vi.fn(async () => ({ ready: true, backupFile: "backup.db" })),
  resumeAfterUpdateCancel: vi.fn(async () => undefined),
}));
vi.mock("@/lib/api/system", () => systemMock);

const updaterMock = vi.hoisted(() => ({ check: vi.fn() }));
vi.mock("@tauri-apps/plugin-updater", () => updaterMock);

type ProgressEvent = {
  event: "Started" | "Progress" | "Finished";
  data: { contentLength?: number; chunkLength?: number };
};

/** check()가 돌려주는 업데이트 객체. downloadAndInstall의 진행 콜백을 직접 조종한다. */
function fakeUpdate(opts: { fail?: boolean } = {}) {
  return {
    version: "1.4.0",
    body: "표 읽기가 좋아졌어요.",
    downloadAndInstall: vi.fn(async (cb: (e: ProgressEvent) => void) => {
      if (opts.fail) throw new Error("network died");
      cb({ event: "Started", data: { contentLength: 1000 } });
      cb({ event: "Progress", data: { chunkLength: 500 } });
      cb({ event: "Finished", data: {} });
    }),
  };
}

describe("UpdateManager (데스크톱)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    tauriMock.isTauri.mockReturnValue(true);
    tauriMock.useIsTauri.mockReturnValue(true);
    systemMock.prepareUpdate.mockResolvedValue({ ready: true, backupFile: "backup.db" });
  });
  afterEach(() => vi.restoreAllMocks());

  it("업데이트가 있으면 버전과 변경 내용을 보여준다", async () => {
    updaterMock.check.mockResolvedValue(fakeUpdate());

    render(<UpdateManager autoCheck />);

    expect(await screen.findByText("새 업데이트가 있습니다.")).toBeInTheDocument();
    expect(screen.getByText("MedBridge 1.4.0")).toBeInTheDocument();
    expect(screen.getByText("표 읽기가 좋아졌어요.")).toBeInTheDocument();
  });

  it("내려받기 전에 반드시 백업을 먼저 만든다", async () => {
    // 이 순서가 뒤집히거나 prepareUpdate가 빠지면, 설치 중 실패했을 때 되돌릴
    // 지점이 없어 학습 기록을 잃는다. 앱 업데이트에서 가장 비싼 회귀다.
    const update = fakeUpdate();
    updaterMock.check.mockResolvedValue(update);

    render(<UpdateManager autoCheck />);
    await userEvent.click(await screen.findByRole("button", { name: "지금 업데이트" }));

    await waitFor(() => expect(update.downloadAndInstall).toHaveBeenCalled());
    expect(systemMock.prepareUpdate).toHaveBeenCalledOnce();
    expect(systemMock.prepareUpdate.mock.invocationCallOrder[0]).toBeLessThan(
      update.downloadAndInstall.mock.invocationCallOrder[0],
    );
  });

  it("내려받기가 끝나면 앱을 다시 시작한다", async () => {
    updaterMock.check.mockResolvedValue(fakeUpdate());

    render(<UpdateManager autoCheck />);
    await userEvent.click(await screen.findByRole("button", { name: "지금 업데이트" }));

    await waitFor(() => expect(tauriMock.relaunchApp).toHaveBeenCalledOnce());
    expect(screen.getByText(/다시 시작하는 중/)).toBeInTheDocument();
  });

  it("실패하면 차단을 풀고 기술 원문을 감춘다", async () => {
    // prepareUpdate가 "새 작업 차단"을 걸어두므로, 실패 경로에서
    // resumeAfterUpdateCancel을 부르지 않으면 앱이 영구히 잠긴다 — 업로드도
    // 요약도 되지 않는데 화면에는 이유가 없다.
    updaterMock.check.mockResolvedValue(fakeUpdate({ fail: true }));

    render(<UpdateManager autoCheck />);
    await userEvent.click(await screen.findByRole("button", { name: "지금 업데이트" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("업데이트를 완료하지 못했습니다.");
    expect(alert).toHaveTextContent("기존 학습자료는 그대로 보관되어 있습니다.");
    expect(alert.textContent).not.toContain("network died");
    await waitFor(() => expect(systemMock.resumeAfterUpdateCancel).toHaveBeenCalled());
    expect(tauriMock.relaunchApp).not.toHaveBeenCalled();
  });

  it("'나중에'를 눌러도 차단을 푼다", async () => {
    // 여기서 빠뜨리면 사용자가 업데이트를 미룬 것뿐인데 앱이 잠긴 채로 남는다.
    updaterMock.check.mockResolvedValue(fakeUpdate());

    render(<UpdateManager autoCheck />);
    await userEvent.click(await screen.findByRole("button", { name: "나중에" }));

    await waitFor(() => expect(systemMock.resumeAfterUpdateCancel).toHaveBeenCalledOnce());
    expect(screen.queryByText("새 업데이트가 있습니다.")).not.toBeInTheDocument();
  });

  it("자동 확인은 실패해도 조용히 넘어간다", async () => {
    // 오프라인에서 앱을 켤 때마다 오류가 뜨면 안 된다 — 사용자가 요청한 확인이
    // 아니기 때문이다. 수동 확인은 반대로 결과를 말해야 한다(아래 테스트).
    updaterMock.check.mockRejectedValue(new Error("offline"));

    render(<UpdateManager autoCheck />);

    await waitFor(() => expect(updaterMock.check).toHaveBeenCalled());
    // 껍데기 <div>는 남지만 안내는 하나도 뜨지 않는다.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/업데이트/)).not.toBeInTheDocument();
  });

  it("수동 확인은 실패를 화면에 말한다", async () => {
    updaterMock.check.mockRejectedValue(new Error("offline"));

    render(<UpdateManager />);
    await userEvent.click(screen.getByRole("button", { name: "업데이트 확인" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "업데이트를 완료하지 못했습니다.",
    );
  });

  it("최신 버전이면 수동 확인에서만 알린다", async () => {
    updaterMock.check.mockResolvedValue(null);

    render(<UpdateManager />);
    await userEvent.click(screen.getByRole("button", { name: "업데이트 확인" }));

    expect(await screen.findByText("최신 버전을 사용하고 있어요.")).toBeInTheDocument();
  });

  it("자동 확인은 한 번만 실행한다", async () => {
    // 리렌더마다 확인하면 앱이 뜨자마자 업데이트 서버를 반복 호출한다.
    updaterMock.check.mockResolvedValue(null);

    const { rerender } = render(<UpdateManager autoCheck />);
    await waitFor(() => expect(updaterMock.check).toHaveBeenCalledOnce());
    rerender(<UpdateManager autoCheck />);
    rerender(<UpdateManager autoCheck />);

    expect(updaterMock.check).toHaveBeenCalledOnce();
  });
});
