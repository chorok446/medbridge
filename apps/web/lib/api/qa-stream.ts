import { getApiConfig } from "@/lib/api/base";
import { ApiError } from "@/lib/api/client";
import type { ApiErrorBody } from "@/types/api";
import type { QaStreamEvent } from "@/types/qa";

/**
 * NDJSON 한 줄씩 분해하는 파서. TextDecoder(stream:true)로 UTF-8 멀티바이트가
 * 청크 경계에서 쪼개져도 안전하고, 마지막 미완성 줄은 다음 청크까지 버퍼에 남긴다.
 */
export function createNdjsonParser(onEvent: (event: QaStreamEvent) => void) {
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  function flushLines(final: boolean) {
    let nl = buffer.indexOf("\n");
    while (nl !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line) emit(line);
      nl = buffer.indexOf("\n");
    }
    // final일 때만 개행 없이 끝난 마지막 줄을 처리한다(그 전엔 미완성으로 간주).
    if (final) {
      const rest = buffer.trim();
      buffer = "";
      if (rest) emit(rest);
    }
  }

  function emit(line: string) {
    let event: unknown;
    try {
      event = JSON.parse(line);
    } catch {
      return; // 깨진 줄은 조용히 버린다 — 기술 오류를 UI에 흘리지 않는다.
    }
    if (event && typeof event === "object" && "type" in event) {
      onEvent(event as QaStreamEvent);
    }
  }

  return {
    push(chunk: Uint8Array) {
      buffer += decoder.decode(chunk, { stream: true });
      flushLines(false);
    },
    end() {
      buffer += decoder.decode();
      flushLines(true);
    },
  };
}

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
    throw new ApiError(
      res.status,
      body?.error.code ?? "INTERNAL_ERROR",
      body?.error.message ?? "문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
      body?.error.retryable ?? false,
    );
  }

  const parser = createNdjsonParser(opts.onEvent);
  const reader = res.body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) parser.push(value);
    }
    parser.end();
  } finally {
    reader.releaseLock();
  }
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
