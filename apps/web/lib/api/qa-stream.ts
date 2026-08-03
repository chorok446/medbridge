import { getApiConfig } from "@/lib/api/base";
import { apiErrorFromResponse } from "@/lib/api/client";
import { consumeNdjson } from "@/lib/api/ndjson";
import type { QaStreamEvent } from "@/types/qa";

// NDJSON 파서는 lib/api/ndjson.ts에 공용화되어 있다(4C 모델 다운로드와 공용).
export { createNdjsonParser } from "@/lib/api/ndjson";

/**
 * 스트림 질문 시작 — NDJSON 이벤트를 onEvent로 흘려준다.
 * EventSource가 아니라 fetch POST를 쓰는 이유: X-MedBridge-Token 헤더가 필요.
 */
export async function streamQuestion(
  documentId: string,
  threadId: string,
  question: string,
  opts: {
    signal: AbortSignal;
    onEvent: (event: QaStreamEvent) => void;
    /** 답변 깊이. 서버가 저장하지 않으므로 요청마다 함께 보낸다. */
    learnerLevel?: string;
  },
): Promise<void> {
  const { base, token } = await getApiConfig();
  const res = await fetch(
    `${base}/api/documents/${documentId}/qa/threads/${threadId}/messages/stream`,
    {
      method: "POST",
      signal: opts.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "X-MedBridge-Token": token } : {}),
      },
      body: JSON.stringify(
        opts.learnerLevel ? { question, learnerLevel: opts.learnerLevel } : { question },
      ),
    },
  );

  if (!res.ok || !res.body) {
    // 스트림 시작 전 거부(501/403/409 등)는 JSON 에러 본문으로 온다.
    const body: unknown = await res.json().catch(() => null);
    throw apiErrorFromResponse(res, body);
  }

  await consumeNdjson<QaStreamEvent>(res.body, opts.onEvent);
}

export interface QaMessageStatusOut {
  id: string;
  status: string;
  errorCode: string | null;
}

export async function cancelStream(
  documentId: string,
  threadId: string,
  messageId: string,
): Promise<QaMessageStatusOut> {
  const { api } = await import("@/lib/api/client");
  return api<QaMessageStatusOut>(
    `/api/documents/${documentId}/qa/threads/${threadId}/messages/${messageId}/cancel`,
    { method: "POST" },
  );
}
