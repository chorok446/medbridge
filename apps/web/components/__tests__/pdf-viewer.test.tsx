import { describe, expect, it } from "vitest";
import { rotateRect } from "@/components/pdf-viewer";
import type { Rect } from "@/types/extraction";

// A4 세로 (pt). 가로·세로가 달라야 회전에서 축이 바뀌는 실수를 잡을 수 있다 —
// 정사각형 페이지로 시험하면 w와 h를 바꿔 써도 통과한다.
const W = 595;
const H = 842;

/** 왼쪽 위 구석의 작은 사각형 — 회전 후 어디로 가는지가 눈으로 확인된다. */
const CORNER: Rect = { x0: 10, y0: 20, x1: 60, y1: 40 };

describe("rotateRect", () => {
  it("회전이 없으면 그대로 둔다", () => {
    expect(rotateRect(CORNER, 0, W, H)).toEqual(CORNER);
  });

  it("90도는 좌상단을 우상단으로 보낸다", () => {
    // 시계방향 90도: 새 x는 (h - 옛 y), 새 y는 옛 x.
    expect(rotateRect(CORNER, 90, W, H)).toEqual({
      x0: H - 40,
      y0: 10,
      x1: H - 20,
      y1: 60,
    });
  });

  it("180도는 반대편 구석으로 보낸다", () => {
    expect(rotateRect(CORNER, 180, W, H)).toEqual({
      x0: W - 60,
      y0: H - 40,
      x1: W - 10,
      y1: H - 20,
    });
  });

  it("270도는 좌상단을 좌하단으로 보낸다", () => {
    expect(rotateRect(CORNER, 270, W, H)).toEqual({
      x0: 20,
      y0: W - 60,
      x1: 40,
      y1: W - 10,
    });
  });

  it("돌린 만큼 되돌리면 원래 자리로 온다", () => {
    // 화면 클릭을 PDF 좌표로 되돌릴 때 (360 - userRotate)로 역변환하는데,
    // 이 왕복이 맞지 않으면 사용자가 원문을 눌러도 엉뚱한 블록이 잡힌다.
    // 90·270 회전은 페이지 가로·세로가 뒤바뀌므로 역변환의 w·h도 바뀐다.
    const cases: { rotate: 90 | 180 | 270; backW: number; backH: number }[] = [
      { rotate: 90, backW: H, backH: W },
      { rotate: 180, backW: W, backH: H },
      { rotate: 270, backW: H, backH: W },
    ];
    for (const { rotate, backW, backH } of cases) {
      const forward = rotateRect(CORNER, rotate, W, H);
      const inverse = ((360 - rotate) % 360) as 0 | 90 | 180 | 270;
      expect(rotateRect(forward, inverse, backW, backH)).toEqual(CORNER);
    }
  });

  it("회전해도 사각형 크기는 보존된다 (90·270은 가로세로가 바뀐다)", () => {
    const w = CORNER.x1 - CORNER.x0;
    const h = CORNER.y1 - CORNER.y0;

    const r180 = rotateRect(CORNER, 180, W, H);
    expect(r180.x1 - r180.x0).toBe(w);
    expect(r180.y1 - r180.y0).toBe(h);

    const r90 = rotateRect(CORNER, 90, W, H);
    expect(r90.x1 - r90.x0).toBe(h);
    expect(r90.y1 - r90.y0).toBe(w);
  });

  it("회전해도 페이지 안에 머문다", () => {
    // 좌표가 음수가 되거나 페이지를 넘으면 하이라이트가 화면 밖으로 나가 사라진다.
    for (const rotate of [90, 180, 270] as const) {
      const out = rotateRect(CORNER, rotate, W, H);
      const [limitX, limitY] = rotate === 180 ? [W, H] : [H, W];
      expect(out.x0).toBeGreaterThanOrEqual(0);
      expect(out.y0).toBeGreaterThanOrEqual(0);
      expect(out.x1).toBeLessThanOrEqual(limitX);
      expect(out.y1).toBeLessThanOrEqual(limitY);
      // 뒤집힌 사각형(x1 < x0)은 화면에서 폭 0으로 그려져 보이지 않는다.
      expect(out.x1).toBeGreaterThan(out.x0);
      expect(out.y1).toBeGreaterThan(out.y0);
    }
  });
});
