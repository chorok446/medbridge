"use client";

import { useState } from "react";
import type { Rect } from "@/types/extraction";

export interface PdfNavigationTarget {
  pageNumber: number;
  bbox: [number, number, number, number];
}

/** PDF 이동 + 하이라이트 + 강조 애니메이션 상태를 한 곳에서 관리한다.
 *
 * 요약·질문·검수 화면이 같은 3종 세트(setPage/setHighlights/setFlashKey)를 각자
 * 복제하면서 이동 동작이 화면마다 갈라졌다 — 수정도 다섯 곳에 반복해야 했다.
 */
export function usePdfNavigation() {
  const [page, setPage] = useState(1);
  const [highlights, setHighlights] = useState<Rect[]>([]);
  const [flashKey, setFlashKey] = useState(0);

  /** 출처 클릭 → 해당 페이지로 이동해 bbox를 강조한다. */
  function navigate(ref: PdfNavigationTarget) {
    setPage(ref.pageNumber);
    setHighlights([{ x0: ref.bbox[0], y0: ref.bbox[1], x1: ref.bbox[2], y1: ref.bbox[3] }]);
    setFlashKey((k) => k + 1);
  }

  /** 현재 페이지 안에서 영역만 강조한다(페이지 이동 없음). */
  function highlight(rect: Rect) {
    setHighlights([rect]);
    setFlashKey((k) => k + 1);
  }

  /** PdfViewer의 onPageChange에 연결 — 페이지를 넘기면 이전 하이라이트를 지운다. */
  function changePage(next: number) {
    setPage(next);
    setHighlights([]);
  }

  return { page, highlights, flashKey, navigate, highlight, changePage };
}
