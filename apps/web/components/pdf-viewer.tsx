"use client";

import { useEffect, useRef, useState } from "react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import type { Rect } from "@/types/extraction";

/**
 * PDF.js 캔버스 뷰어 + 좌표 하이라이트.
 *
 * 저장 좌표는 PyMuPDF 페이지 공간(pt, 좌상단 원점, y 아래로, 페이지 자체 회전 적용 후)이다.
 * PDF.js 기본 viewport(rotation = 페이지 회전)도 같은 공간을 스케일만 다르게 쓰므로
 * 기본 회전에서는 screen = 좌표 × scale 이고, 사용자가 추가 회전하면
 * 사각형을 페이지 크기 기준으로 회전 변환한 뒤 스케일한다.
 * (사용자 화면에는 좌표 숫자를 표시하지 않는다)
 */

interface Props {
  fileUrl: string;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
  highlights: Rect[];
  flashKey?: number; // 바뀔 때마다 하이라이트 강조 애니메이션 재생
  onPagePointerDown?: (x: number, y: number) => void; // PDF 공간 좌표 (pt)
}

interface RenderedPage {
  width: number; // viewport 크기 (px)
  height: number;
  baseWidth: number; // 기본 회전 기준 페이지 크기 (pt)
  baseHeight: number;
}

function rotateRect(rect: Rect, rotate: 0 | 90 | 180 | 270, w: number, h: number): Rect {
  const { x0, y0, x1, y1 } = rect;
  switch (rotate) {
    case 90:
      return { x0: h - y1, y0: x0, x1: h - y0, y1: x1 };
    case 180:
      return { x0: w - x1, y0: h - y1, x1: w - x0, y1: h - y0 };
    case 270:
      return { x0: y0, y0: w - x1, x1: y1, y1: w - x0 };
    default:
      return rect;
  }
}

export function PdfViewer({
  fileUrl,
  page,
  pageCount,
  onPageChange,
  highlights,
  flashKey = 0,
  onPagePointerDown,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [scale, setScale] = useState(1.2);
  const [userRotate, setUserRotate] = useState<0 | 90 | 180 | 270>(0);
  const [rendered, setRendered] = useState<RenderedPage | null>(null);
  const [error, setError] = useState(false);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);

  // 문서 로드는 fileUrl에만 묶는다 — 페이지·배율·회전 변경마다 수백 MB짜리 문서를
  // 다시 열면 이전 문서가 해제되지 않아 웹뷰 메모리가 페이지 넘김마다 누적된다.
  useEffect(() => {
    let cancelled = false;
    let task: PDFDocumentLoadingTask | null = null;
    async function load() {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();
        if (cancelled) return;
        task = pdfjs.getDocument({ url: fileUrl });
        const d = await task.promise;
        if (!cancelled) setDoc(d);
      } catch {
        if (!cancelled) setError(true);
      }
    }
    void load();
    return () => {
      cancelled = true;
      setDoc(null);
      // 진행 중 로드 중단 + 문서·워커 해제까지 한 번에 담당한다.
      void task?.destroy().catch(() => undefined);
    };
  }, [fileUrl]);

  useEffect(() => {
    if (!doc) return;
    let cancelled = false;
    let renderTask: RenderTask | null = null;
    async function render() {
      try {
        if (!doc) return;
        const pdfPage = await doc.getPage(page);
        // 기본 회전(페이지 자체 회전) + 사용자 추가 회전
        const viewport = pdfPage.getViewport({
          scale,
          rotation: (pdfPage.rotate + userRotate) % 360,
        });
        const base = pdfPage.getViewport({ scale: 1, rotation: pdfPage.rotate });
        const canvas = canvasRef.current;
        if (!canvas || cancelled) return;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        renderTask = pdfPage.render({ canvas, canvasContext: ctx, viewport });
        await renderTask.promise;
        if (!cancelled) {
          setRendered({
            width: viewport.width,
            height: viewport.height,
            baseWidth: base.width,
            baseHeight: base.height,
          });
          setError(false);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    }
    void render();
    return () => {
      cancelled = true;
      // 같은 캔버스에 렌더가 겹치면 pdf.js가 예외를 던진다 — 이전 렌더를 명시적으로 끊는다.
      renderTask?.cancel();
    };
  }, [doc, page, scale, userRotate]);

  const toScreen = (rect: Rect): Rect | null => {
    if (!rendered) return null;
    const rotated = rotateRect(rect, userRotate, rendered.baseWidth, rendered.baseHeight);
    return {
      x0: rotated.x0 * scale,
      y0: rotated.y0 * scale,
      x1: rotated.x1 * scale,
      y1: rotated.y1 * scale,
    };
  };

  return (
    <div className="flex h-full flex-col rounded-lg border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-3 py-2 text-sm">
        <button
          type="button"
          onClick={() => onPageChange(Math.max(1, page - 1))}
          disabled={page <= 1}
          className="rounded border border-slate-300 px-2.5 py-1 disabled:opacity-40"
        >
          이전
        </button>
        <span aria-live="polite">
          {page} / {pageCount || "?"}
        </span>
        <button
          type="button"
          onClick={() => onPageChange(Math.min(pageCount || page + 1, page + 1))}
          disabled={pageCount > 0 && page >= pageCount}
          className="rounded border border-slate-300 px-2.5 py-1 disabled:opacity-40"
        >
          다음
        </button>
        <span className="mx-1 text-slate-300">|</span>
        <button
          type="button"
          aria-label="축소"
          onClick={() => setScale((s) => Math.max(0.5, Math.round((s - 0.2) * 10) / 10))}
          className="rounded border border-slate-300 px-2.5 py-1"
        >
          −
        </button>
        <span>{Math.round(scale * 100)}%</span>
        <button
          type="button"
          aria-label="확대"
          onClick={() => setScale((s) => Math.min(3, Math.round((s + 0.2) * 10) / 10))}
          className="rounded border border-slate-300 px-2.5 py-1"
        >
          +
        </button>
        <button
          type="button"
          onClick={() => setUserRotate((r) => ((r + 90) % 360) as 0 | 90 | 180 | 270)}
          className="rounded border border-slate-300 px-2.5 py-1"
        >
          회전
        </button>
      </div>

      <div className="relative flex-1 overflow-auto bg-slate-100 p-3">
        {error ? (
          <p className="p-6 text-center text-sm text-red-700">
            문서 화면을 표시하지 못했습니다. 다시 시도해 주세요.
          </p>
        ) : (
          <div
            // 그림자 대신 1px 테두리로 종이의 가장자리를 낸다. DESIGN.md는 깊이를
            // (1) 바탕 위 흰 종이의 명도 차, (2) 1px 테두리 두 가지로만 낸다고
            // 못박았고 "box-shadow 값은 코드베이스에 존재하지 않는다"고 적혀 있다.
            // 회색 바탕(bg-slate-100) 위 흰 캔버스라 (1)은 이미 성립한다.
            className="relative mx-auto w-fit border border-slate-300"
            onPointerDown={(e) => {
              if (!rendered || !onPagePointerDown) return;
              const el = e.currentTarget.getBoundingClientRect();
              const sx = e.clientX - el.left;
              const sy = e.clientY - el.top;
              // 화면 → PDF 공간 역변환 (사용자 회전 역적용)
              const inv = rotateRect(
                { x0: sx / scale, y0: sy / scale, x1: sx / scale, y1: sy / scale },
                ((360 - userRotate) % 360) as 0 | 90 | 180 | 270,
                userRotate % 180 === 0 ? rendered.baseWidth : rendered.baseHeight,
                userRotate % 180 === 0 ? rendered.baseHeight : rendered.baseWidth,
              );
              onPagePointerDown(inv.x0, inv.y0);
            }}
          >
            <canvas ref={canvasRef} className="block" />
            {rendered &&
              highlights.map((h, i) => {
                const s = toScreen(h);
                if (!s) return null;
                return (
                  <div
                    key={`${flashKey}-${i}`}
                    data-testid="pdf-highlight"
                    // 깜빡임을 끄면 테두리와 틴트는 남는다 — 위치를 알리는 일은
                    // 움직임이 아니라 색이 하므로, 동작을 줄여도 정보는 잃지 않는다.
                    className="pointer-events-none absolute animate-pulse rounded-sm border-2 border-amber-500 bg-amber-300/30 motion-reduce:animate-none"
                    style={{
                      left: s.x0,
                      top: s.y0,
                      width: Math.max(2, s.x1 - s.x0),
                      height: Math.max(2, s.y1 - s.y0),
                      animationIterationCount: 3,
                    }}
                  />
                );
              })}
          </div>
        )}
      </div>
    </div>
  );
}
