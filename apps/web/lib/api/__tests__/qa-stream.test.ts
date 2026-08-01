import { describe, expect, it } from "vitest";
import { createNdjsonParser } from "@/lib/api/qa-stream";
import type { QaStreamEvent } from "@/types/qa";

const enc = new TextEncoder();

function collect(chunks: Uint8Array[]): QaStreamEvent[] {
  const events: QaStreamEvent[] = [];
  const parser = createNdjsonParser<QaStreamEvent>((e) => events.push(e));
  for (const c of chunks) parser.push(c);
  parser.end();
  return events;
}

describe("createNdjsonParser", () => {
  it("한 청크의 여러 이벤트를 분리한다", () => {
    const events = collect([
      enc.encode('{"type":"started","requestId":"r","messageId":"m"}\n{"type":"phase","phase":"generating"}\n'),
    ]);
    expect(events.map((e) => e.type)).toEqual(["started", "phase"]);
  });

  it("여러 청크에 걸쳐 쪼개진 한 줄을 이어붙인다", () => {
    const events = collect([
      enc.encode('{"type":"claim","seq":1,"claimIndex":0,"tex'),
      enc.encode('t":"심장은 뛴다","sources":[]}\n'),
    ]);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "claim", text: "심장은 뛴다" });
  });

  it("UTF-8 멀티바이트가 청크 경계에서 쪼개져도 안전하다", () => {
    const prefix = '{"type":"claim","seq":1,"claimIndex":0,"text":"';
    const full = enc.encode(`${prefix}심장","sources":[]}\n`);
    // '심'(3바이트)의 첫 바이트 뒤에서 잘라 멀티바이트 경계를 쪼갠다.
    const cut = enc.encode(prefix).length + 1;
    const events = collect([full.slice(0, cut), full.slice(cut)]);
    expect(events[0]).toMatchObject({ text: "심장" });
  });

  it("개행 없이 끝난 마지막 줄도 end에서 처리한다", () => {
    const events = collect([enc.encode('{"type":"completed","message":{"id":"m"}}')]);
    expect(events[0].type).toBe("completed");
  });

  it("깨진 JSON 줄은 조용히 버린다", () => {
    const events = collect([enc.encode('깨진 줄\n{"type":"heartbeat","seq":1}\n')]);
    expect(events.map((e) => e.type)).toEqual(["heartbeat"]);
  });
});
