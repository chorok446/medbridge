import { getApiConfig } from "@/lib/api/base";
import { ApiError } from "@/lib/api/client";
import { consumeNdjson } from "@/lib/api/ndjson";
import type { ApiErrorBody } from "@/types/api";
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
  opts: { signal: AbortSignal; onEvent: (event: QaStreamEvent) => void },
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
      body: JSON.stringify({ question }),
    },
  );

  if (!res.ok || !res.body) {
    // 스트림 시작 전 거부(501/403/409 등)는 JSON 에러 본문으로 온다.
    const body = (await res.json().catch(() => null)) as ApiErrorBody | null;
    // body.error가 없을 수도 있다(FastAPI detail 응답·잘린 본문) → 옵셔널 체이닝으로 방어.
    throw new ApiError(
      res.status,
      body?.error?.code ?? "INTERNAL_ERROR",
      body?.error?.message ?? "문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
      body?.error?.retryable ?? false,
    );
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
