"use client";

import { useEffect, useState } from "react";
import { relaunchApp, saveErrorReport, sidecarStatus, useIsTauri } from "@/lib/tauri";

/**
 * 앱 준비 게이트 — sidecar와 데이터베이스가 준비되기 전에는 메인 화면을 보여주지 않는다.
 * 기술 정보(포트·경로·오류 원문)는 표시하지 않는다.
 */
export function StartupGate({ children }: { children: React.ReactNode }) {
  // useIsTauri()는 하이드레이션 안전(정적 export 빌드와 첫 렌더가 항상 일치)하다.
  // 브라우저 개발 모드에서는 desktop이 계속 false이므로 아래에서 children을
  // 즉시 렌더한다 — 별도의 "starting → ready" 전환이 필요 없다.
  const desktop = useIsTauri();
  const [sidecarState, setSidecarState] = useState<"starting" | "ready" | "failed">("starting");

  useEffect(() => {
    if (!desktop || sidecarState !== "starting") return;
    let cancelled = false;
    const timer = setInterval(async () => {
      const s = await sidecarStatus().catch(() => "starting" as const);
      if (!cancelled && s !== "starting") {
        setSidecarState(s);
      }
    }, 700);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [desktop, sidecarState]);

  if (!desktop || sidecarState === "ready") return <>{children}</>;

  if (sidecarState === "failed") {
    return (
      <div className="flex min-h-[70vh] flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="text-xl font-bold">MedBridge를 시작하지 못했습니다.</h1>
        <p className="text-sm text-slate-600">
          학습자료는 삭제되지 않았습니다.
          <br />
          다시 시작하거나 오류 정보를 저장할 수 있습니다.
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => relaunchApp()}
            className="rounded bg-blue-600 px-5 py-2.5 font-medium text-white hover:bg-blue-700"
          >
            다시 시작
          </button>
          <button
            type="button"
            onClick={() => saveErrorReport()}
            className="rounded border border-slate-300 px-5 py-2.5 hover:bg-slate-50"
          >
            오류 정보 저장
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-h-[70vh] flex-col items-center justify-center gap-3 px-6 text-center"
    >
      <span
        aria-hidden
        // 회전을 멈춰도 정보를 잃지 않는다 — 진행 중이라는 사실은 옆의 문장이
        // 말하고, 이 원은 aria-hidden이라 애초에 보조기술에는 없는 요소다.
        className="h-8 w-8 animate-spin rounded-full border-4 border-blue-200 border-t-blue-600 motion-reduce:animate-none"
      />
      <h1 className="text-lg font-semibold">MedBridge를 준비하고 있습니다.</h1>
      <p className="text-sm text-slate-600">
        학습자료를 확인하는 중입니다.
        <br />
        잠시만 기다려 주세요.
      </p>
    </div>
  );
}
