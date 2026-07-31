"use client";

import { useEffect, useState } from "react";
import { isTauri, relaunchApp, saveErrorReport, sidecarStatus } from "@/lib/tauri";

/**
 * 앱 준비 게이트 — sidecar와 데이터베이스가 준비되기 전에는 메인 화면을 보여주지 않는다.
 * 기술 정보(포트·경로·오류 원문)는 표시하지 않는다.
 */
export function StartupGate({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<"starting" | "ready" | "failed">(
    isTauri() ? "starting" : "ready",
  );

  useEffect(() => {
    if (!isTauri() || status !== "starting") return;
    let cancelled = false;
    const timer = setInterval(async () => {
      const s = await sidecarStatus().catch(() => "starting" as const);
      if (!cancelled && s !== "starting") {
        setStatus(s);
      }
    }, 700);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [status]);

  if (status === "ready") return <>{children}</>;

  if (status === "failed") {
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
        className="h-8 w-8 animate-spin rounded-full border-4 border-blue-200 border-t-blue-600"
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
