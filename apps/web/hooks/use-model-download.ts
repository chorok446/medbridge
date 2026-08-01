"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { streamModelPull } from "@/lib/api/local-ai";

export type DownloadPhase = "idle" | "downloading" | "completed" | "cancelling" | "cancelled" | "failed";

export interface DownloadState {
  phase: DownloadPhase;
  model: string | null;
  stepLabel: string; // 사용자 친화 현재 단계
  percent: number | null; // 0–100, 총량 미상이면 null
  errorMessage: string | null;
}

const IDLE: DownloadState = {
  phase: "idle",
  model: null,
  stepLabel: "",
  percent: null,
  errorMessage: null,
};

/**
 * 모델 다운로드 상태 머신. 중복 시작 차단, 취소(fetch 종료), unmount 후 setState 차단.
 * 앱/컴포넌트가 사라져도 안전하고, 다시 실행하면 실제 설치 상태를 /models로 재확인한다.
 */
export function useModelDownload() {
  const [state, setState] = useState<DownloadState>(IDLE);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  // 진행 중 요청 세대 — 취소·재시작 뒤 늦게 온 이전 이벤트를 버린다.
  const genRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort(); // unmount 시 연결 정리
    };
  }, []);

  const safeSet = useCallback((updater: (s: DownloadState) => DownloadState, gen: number) => {
    if (mountedRef.current && gen === genRef.current) setState(updater);
  }, []);

  const start = useCallback(
    async (model: string) => {
      if (abortRef.current) return; // 이미 진행 중 → 중복 시작 차단
      const gen = (genRef.current += 1);
      const controller = new AbortController();
      abortRef.current = controller;
      setState({ ...IDLE, phase: "downloading", model, stepLabel: "준비 중" });
      try {
        await streamModelPull(model, {
          signal: controller.signal,
          onEvent: (event) => {
            if (event.type === "progress") {
              safeSet(
                (s) => ({
                  ...s,
                  phase: s.phase === "cancelling" ? "cancelling" : "downloading",
                  stepLabel: event.phase,
                  percent: typeof event.percent === "number" ? event.percent : s.percent,
                }),
                gen,
              );
            } else if (event.type === "completed") {
              safeSet((s) => ({ ...s, phase: "completed", percent: 100 }), gen);
            } else if (event.type === "error") {
              safeSet((s) => ({ ...s, phase: "failed", errorMessage: event.message }), gen);
            }
          },
        });
        // terminal 이벤트 없이 스트림이 끝나면(연결만 닫힘) 실패로 처리 — 멈춤 방지.
        safeSet(
          (s) => (s.phase === "downloading" ? { ...s, phase: "failed",
            errorMessage: "다운로드가 중단되었습니다. 다시 시도해 주세요." } : s),
          gen,
        );
      } catch {
        if (controller.signal.aborted) return; // 사용자가 취소 → cancel 경로가 상태 관리
        safeSet(
          (s) => ({ ...s, phase: "failed",
            errorMessage: "다운로드에 실패했습니다. 다시 시도해 주세요." }),
          gen,
        );
      } finally {
        if (gen === genRef.current) abortRef.current = null;
      }
    },
    [safeSet],
  );

  const cancel = useCallback(() => {
    if (!abortRef.current) return;
    setState((s) => ({ ...s, phase: "cancelling" }));
    abortRef.current.abort();
    abortRef.current = null;
    genRef.current += 1; // 이후 이벤트 무시
    setState((s) => ({ ...s, phase: "cancelled" }));
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    genRef.current += 1;
    setState(IDLE);
  }, []);

  const active = state.phase === "downloading" || state.phase === "cancelling";
  return { state, active, start, cancel, reset };
}
