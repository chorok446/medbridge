"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { prepareUpdate, resumeAfterUpdateCancel } from "@/lib/api/system";
import { isTauri, relaunchApp, saveErrorReport } from "@/lib/tauri";

type Phase =
  | { name: "idle" }
  | { name: "checking" }
  | { name: "none" } // 최신 상태
  | { name: "available"; version: string; notes: string }
  | { name: "downloading"; percent: number }
  | { name: "installing" }
  | { name: "restarting" }
  | { name: "error" };

interface TauriUpdate {
  version: string;
  body?: string | null;
  downloadAndInstall: (
    cb: (event: {
      event: "Started" | "Progress" | "Finished";
      data: { contentLength?: number; chunkLength?: number };
    }) => void,
  ) => Promise<void>;
}

async function checkForUpdate(): Promise<TauriUpdate | null> {
  const { check } = await import("@tauri-apps/plugin-updater");
  return (await check()) as TauriUpdate | null;
}

/**
 * 업데이트 확인·다운로드·설치 GUI.
 * - autoCheck: 앱 실행 후 1회 자동 확인 (레이아웃에서 사용)
 * - 수동 확인 버튼은 설정 화면에서 사용
 * 기술 오류·URL·서명값은 표시하지 않는다.
 */
export function UpdateManager({ autoCheck = false }: { autoCheck?: boolean }) {
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const updateRef = useRef<TauriUpdate | null>(null);
  const checkedOnce = useRef(false);

  const runCheck = useCallback(async (silent: boolean) => {
    if (!isTauri()) return;
    setPhase({ name: "checking" });
    try {
      const update = await checkForUpdate();
      if (update) {
        updateRef.current = update;
        setPhase({
          name: "available",
          version: update.version,
          notes: update.body ?? "",
        });
      } else {
        setPhase(silent ? { name: "idle" } : { name: "none" });
      }
    } catch {
      // 자동 확인 실패는 조용히 넘어간다 (오프라인 등)
      setPhase(silent ? { name: "idle" } : { name: "error" });
    }
  }, []);

  useEffect(() => {
    if (autoCheck && isTauri() && !checkedOnce.current) {
      checkedOnce.current = true;
      void runCheck(true);
    }
  }, [autoCheck, runCheck]);

  async function startUpdate() {
    const update = updateRef.current;
    if (!update) return;
    try {
      await prepareUpdate(); // 새 작업 차단 + checkpoint + DB 백업
      let total = 0;
      let received = 0;
      setPhase({ name: "downloading", percent: 0 });
      await update.downloadAndInstall((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength ?? 0;
        } else if (event.event === "Progress") {
          received += event.data.chunkLength ?? 0;
          if (total > 0) {
            setPhase({
              name: "downloading",
              percent: Math.min(99, Math.round((received / total) * 100)),
            });
          }
        } else if (event.event === "Finished") {
          setPhase({ name: "installing" });
        }
      });
      setPhase({ name: "restarting" });
      await relaunchApp();
    } catch {
      setPhase({ name: "error" });
      void resumeAfterUpdateCancel().catch(() => undefined);
    }
  }

  async function dismiss() {
    setPhase({ name: "idle" });
    void resumeAfterUpdateCancel().catch(() => undefined);
  }

  if (!isTauri()) {
    return autoCheck ? null : (
      <p className="text-sm text-slate-500">업데이트는 데스크톱 앱에서 확인할 수 있어요.</p>
    );
  }

  return (
    <div>
      {!autoCheck && phase.name === "idle" && (
        <button
          type="button"
          onClick={() => runCheck(false)}
          className="rounded border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
        >
          업데이트 확인
        </button>
      )}
      {phase.name === "checking" && (
        <p role="status" className="text-sm text-slate-600">
          업데이트를 확인하는 중…
        </p>
      )}
      {phase.name === "none" && (
        <p role="status" className="text-sm text-green-700">
          최신 버전을 사용하고 있어요.
        </p>
      )}

      {phase.name === "available" && (
        <div
          role="dialog"
          aria-label="업데이트 안내"
          className="rounded-lg border border-blue-200 bg-blue-50 p-4"
        >
          <p className="font-semibold">새 업데이트가 있습니다.</p>
          <p className="mt-1 text-sm">MedBridge {phase.version}</p>
          {phase.notes && (
            <p className="mt-2 whitespace-pre-line text-sm text-slate-700">{phase.notes}</p>
          )}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={dismiss}
              className="rounded border border-slate-300 px-4 py-2 text-sm hover:bg-white"
            >
              나중에
            </button>
            <button
              type="button"
              onClick={startUpdate}
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              지금 업데이트
            </button>
          </div>
        </div>
      )}

      {(phase.name === "downloading" ||
        phase.name === "installing" ||
        phase.name === "restarting") && (
        <div
          role="status"
          aria-live="polite"
          className="rounded-lg border border-slate-200 bg-white p-4 text-sm"
        >
          {phase.name === "downloading" && (
            <>
              <p className="font-medium">업데이트를 다운로드하고 있습니다.</p>
              <p className="mt-1 text-2xl font-bold">{phase.percent}%</p>
              <div className="mt-2 h-2 overflow-hidden rounded bg-slate-200">
                <div
                  className="h-full bg-blue-600 transition-all"
                  style={{ width: `${phase.percent}%` }}
                />
              </div>
            </>
          )}
          {phase.name === "installing" && <p className="font-medium">업데이트를 설치하는 중…</p>}
          {phase.name === "restarting" && (
            <p className="font-medium">MedBridge를 다시 시작하는 중…</p>
          )}
          <p className="mt-2 text-slate-500">MedBridge를 종료하지 마세요.</p>
        </div>
      )}

      {phase.name === "error" && (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm">
          <p className="font-semibold text-red-800">업데이트를 완료하지 못했습니다.</p>
          <p className="mt-1 text-red-700">
            기존 학습자료는 그대로 보관되어 있습니다.
            <br />
            다시 시도하거나 오류 정보를 저장해 주세요.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => runCheck(false)}
              className="rounded border border-red-300 px-4 py-2 text-red-800 hover:bg-red-100"
            >
              다시 시도
            </button>
            <button
              type="button"
              onClick={dismiss}
              className="rounded border border-red-300 px-4 py-2 text-red-800 hover:bg-red-100"
            >
              나중에
            </button>
            <button
              type="button"
              onClick={() => saveErrorReport()}
              className="rounded border border-red-300 px-4 py-2 text-red-800 hover:bg-red-100"
            >
              오류 정보 저장
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
