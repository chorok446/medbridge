import { describe, expect, it } from "vitest";
import { hasCitations, tokenizeCitations } from "@/lib/citations";

describe("tokenizeCitations", () => {
  it("splits text around a marker", () => {
    expect(tokenizeCitations("심장은 혈액을 보냅니다[c0].", 1)).toEqual([
      { kind: "text", text: "심장은 혈액을 보냅니다" },
      { kind: "citation", claimIndex: 0 },
      { kind: "text", text: "." },
    ]);
  });

  it("keeps consecutive markers separate", () => {
    const tokens = tokenizeCitations("근거가 둘입니다[c0][c1]", 2);
    expect(tokens.filter((t) => t.kind === "citation")).toHaveLength(2);
  });

  it("treats an out-of-range marker as plain text", () => {
    // 서버가 걸러주지만 프론트도 스스로를 지킨다 — 없는 근거로 이동하면 안 된다.
    expect(tokenizeCitations("x[c9]", 1)).toEqual([{ kind: "text", text: "x[c9]" }]);
  });

  it("leaves a truncated marker alone", () => {
    expect(tokenizeCitations("잘린 마커[c", 1)).toEqual([
      { kind: "text", text: "잘린 마커[c" },
    ]);
  });

  it("does not treat document brackets as citations", () => {
    expect(tokenizeCitations("표 [1]을 보라", 3)).toEqual([
      { kind: "text", text: "표 [1]을 보라" },
    ]);
  });

  it("handles a marker at the very start", () => {
    expect(tokenizeCitations("[c0] 뒤에 글", 1)).toEqual([
      { kind: "citation", claimIndex: 0 },
      { kind: "text", text: " 뒤에 글" },
    ]);
  });

  it("returns an empty list for empty content", () => {
    expect(tokenizeCitations("", 1)).toEqual([]);
  });

  it("reports no citations for plain prose", () => {
    expect(hasCitations(tokenizeCitations("마커 없는 답변", 2))).toBe(false);
  });

  it("reports citations when a valid marker exists", () => {
    expect(hasCitations(tokenizeCitations("답변[c0]", 1))).toBe(true);
  });

  it("reports no citations when every marker is out of range", () => {
    expect(hasCitations(tokenizeCitations("답변[c5]", 1))).toBe(false);
  });
});
