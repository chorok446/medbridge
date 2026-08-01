/**
 * NDJSON 스트림 파서 — 한 줄 = JSON 이벤트 하나. Sprint 4B Q&A 스트리밍과 4C 모델
 * 다운로드가 공용으로 쓴다. TextDecoder(stream:true)로 UTF-8 멀티바이트가 청크 경계에서
 * 쪼개져도 안전하고, 마지막 미완성 줄은 다음 청크까지 버퍼에 남긴다.
 */
export function createNdjsonParser<T>(onEvent: (event: T) => void) {
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
      onEvent(event as T);
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
 * fetch POST 응답 본문(ReadableStream)을 NDJSON 이벤트로 소비한다. EventSource가 아니라
 * fetch를 쓰는 이유: X-MedBridge-Token 헤더가 필요하고, 취소(AbortController)가 가능해야 한다.
 */
export async function consumeNdjson<T>(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: T) => void,
): Promise<void> {
  const parser = createNdjsonParser<T>(onEvent);
  const reader = body.getReader();
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
