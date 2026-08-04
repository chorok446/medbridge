"use client";

import { useCallback, useRef, useState } from "react";
import { ApiError } from "@/lib/api/client";
import { cancelStream, streamQuestion } from "@/lib/api/qa-stream";
import type { QaMessage, QaStreamPhase, QaStreamSource } from "@/types/qa";

export interface StreamedClaim {
  claimIndex: number;
  text: string;
  sources: QaStreamSource[];
}

export interface QaStreamState {
  phase: QaStreamPhase;
  claims: StreamedClaim[];
  finalMessage: QaMessage | null;
  /** 스트림 시작 거부(501/403 등)의 HTTP status. */
  errorStatus: number | null;
  /** interrupted 이벤트의 코드(REVISION_CHANGED, CONNECTION_LOST, APP_RESTARTED …). */
  interruptCode: string | null;
}

const IDLE: QaStreamState = {
  phase: "idle",
  claims: [],
  finalMessage: null,
  errorStatus: null,
  interruptCode: null,
};

/**
 * 스트리밍 Q&A 상태 머신. 검증된 claim만 서버가 보내므로 도착 순서대로 붙인다.
 * requestId로 경합을 막는다 — 취소 뒤 늦게 온 이전 요청의 이벤트는 무시한다.
 */
export function useQaStream(documentId: string) {
  const [state, setState] = useState<QaStreamState>(IDLE);
  const abortRef = useRef<AbortController | null>(null);
  const activeRef = useRef<{ threadId: string; messageId: string | null } | null>(null);
  // 진행 중인 요청을 세대 번호로 식별 — 늦게 도착한 이전 세대 이벤트는 버린다.
  const genRef = useRef(0);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    activeRef.current = null;
    genRef.current += 1;
    setState(IDLE);
  }, []);

  const ask = useCallback(
    async (threadId: string, question: string, learnerLevel?: string) => {
      abortRef.current?.abort();
      const gen = (genRef.current += 1);
      const controller = new AbortController();
      abortRef.current = controller;
      activeRef.current = { threadId, messageId: null };
      setState({ ...IDLE, phase: "connecting" });

      const fresh = () => gen === genRef.current;
      let sawTerminal = false;

      try {
        await streamQuestion(documentId, threadId, question, {
          signal: controller.signal,
          learnerLevel,
          onEvent: (event) => {
            if (!fresh()) return;
            if (
              event.type === "completed" ||
              event.type === "cancelled" ||
              event.type === "interrupted" ||
              event.type === "error"
            ) {
              sawTerminal = true;
            }
            switch (event.type) {
              case "started":
                if (activeRef.current) activeRef.current.messageId = event.messageId;
                break;
              case "phase":
                setState((s) =>
                  // 취소 중이면 단계 갱신을 무시(취소 표시 유지).
                  s.phase === "cancelling" ? s : { ...s, phase: event.phase },
                );
                break;
              case "claim":
                setState((s) => ({
                  ...s,
                  claims: [
                    ...s.claims,
                    { claimIndex: event.claimIndex, text: event.text, sources: event.sources },
                  ],
                }));
                break;
              case "completed":
                setState((s) => ({ ...s, phase: "completed", finalMessage: event.message }));
                break;
              case "cancelled":
                setState((s) => ({ ...s, phase: "cancelled" }));
                break;
              case "interrupted":
                setState((s) => ({ ...s, phase: "interrupted", interruptCode: event.code }));
                break;
              case "error":
                setState((s) => ({ ...s, phase: "failed" }));
                break;
              // heartbeat: 연결 유지 신호 — 표시 없음.
            }
          },
        });
        // 정상 EOF인데 terminal 이벤트가 없으면(프록시/서버가 이벤트 없이 연결만 닫음)
        // active로 영구히 멈추지 않도록 interrupted로 확정한다.
        if (fresh() && !sawTerminal && !controller.signal.aborted) {
          setState((s) => ({ ...s, phase: "interrupted", interruptCode: "CONNECTION_LOST" }));
        }
      } catch (err) {
        if (!fresh()) return;
        if (controller.signal.aborted) return; // 사용자가 취소 → 상태는 cancel 경로가 관리.
        const status = err instanceof ApiError ? err.status : null;
        setState((s) => ({ ...s, phase: "failed", errorStatus: status }));
      }
    },
    [documentId],
  );

  const cancel = useCallback(async () => {
    const active = activeRef.current;
    // 취소 시점의 연결만 끊는다 — 대기 중 새 질문이 시작되면 그 연결은 건드리지 않는다.
    const controller = abortRef.current;
    setState((s) => ({ ...s, phase: "cancelling" }));
    // started 이벤트 전에 취소하면 messageId가 아직 없다 — 서버는 이미 메시지를 만들었을
    // 수 있으므로 바로 끊지 말고 잠깐 기다려 서버 취소를 전달한다. 안 그러면 서버가
    // CANCELLED 대신 CONNECTION_LOST로 확정해 "연결이 끊겼다"는 오류로 표시된다.
    if (active && !active.messageId) {
      const deadline = Date.now() + 3000;
      while (!active.messageId && Date.now() < deadline && activeRef.current === active) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
    // 서버에 취소를 알린다(멱등). 그다음 로컬 연결을 끊는다.
    if (active?.messageId && activeRef.current === active) {
      try {
        await cancelStream(documentId, active.threadId, active.messageId);
      } catch {
        // 취소 API 실패는 무시 — 연결을 끊는 것만으로도 클라이언트는 종료된다.
      }
    }
    controller?.abort();
    setState((s) => (s.phase === "cancelling" ? { ...s, phase: "cancelled" } : s));
  }, [documentId]);

  const active =
    state.phase !== "idle" &&
    state.phase !== "completed" &&
    state.phase !== "cancelled" &&
    state.phase !== "interrupted" &&
    state.phase !== "failed";

  return { state, active, ask, cancel, reset };
}
