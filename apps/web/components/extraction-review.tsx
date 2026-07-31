"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { OcrPanel } from "@/components/ocr-panel";
import { PdfViewer } from "@/components/pdf-viewer";
import {
  cancelExtraction,
  getExtractionStatus,
  getPage,
  getPageBlocks,
  listPages,
  listTables,
  retryExtraction,
} from "@/lib/api/extraction";
import type { DocumentSummary } from "@/types/api";
import type { ExtractionBlock, Rect } from "@/types/extraction";

type PanelTab = "text" | "blocks" | "tables" | "notes";

interface Props {
  doc: DocumentSummary;
  fileUrl: string;
}

/** 추출 결과 검수 화면 — 왼쪽 원문(PDF), 오른쪽 추출 텍스트. 블록 클릭 ↔ 원문 하이라이트. */
export function ExtractionReview({ doc, fileUrl }: Props) {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [tab, setTab] = useState<PanelTab>("text");
  const [includeBands, setIncludeBands] = useState(false);
  const [highlights, setHighlights] = useState<Rect[]>([]);
  const [flashKey, setFlashKey] = useState(0);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);

  const extracting = doc.processingStatus === "extracting";

  const statusQuery = useQuery({
    queryKey: ["extraction-status", doc.id],
    queryFn: () => getExtractionStatus(doc.id),
    refetchInterval: extracting ? 1000 : false,
  });

  const pagesQuery = useQuery({
    queryKey: ["extraction-pages", doc.id],
    queryFn: () => listPages(doc.id),
    enabled: !extracting,
  });

  const pageQuery = useQuery({
    queryKey: ["extraction-page", doc.id, page],
    queryFn: () => getPage(doc.id, page),
    enabled: !extracting,
  });

  const blocksQuery = useQuery({
    queryKey: ["extraction-blocks", doc.id, page, includeBands],
    queryFn: () => getPageBlocks(doc.id, page, includeBands),
    enabled: !extracting,
  });

  const tablesQuery = useQuery({
    queryKey: ["extraction-tables", doc.id],
    queryFn: () => listTables(doc.id),
    enabled: !extracting && tab === "tables",
  });

  const pageCount = doc.pageCount ?? pagesQuery.data?.length ?? 0;
  const pages = useMemo(() => pagesQuery.data ?? [], [pagesQuery.data]);
  const ocrPages = useMemo(() => pages.filter((p) => p.requiresOcr), [pages]);
  const failedPages = useMemo(
    () => pages.filter((p) => p.extractionStatus === "failed"),
    [pages],
  );
  const uncertainPages = useMemo(
    () => pages.filter((p) => p.readingOrderConfidence < 0.7 && !p.requiresOcr),
    [pages],
  );

  function focusBlock(block: ExtractionBlock) {
    setHighlights([{ x0: block.x0, y0: block.y0, x1: block.x1, y1: block.y1 }]);
    setFlashKey((k) => k + 1);
    setSelectedBlockId(block.id);
  }

  function handlePdfClick(x: number, y: number) {
    const hit = (blocksQuery.data ?? []).find(
      (b) => x >= b.x0 && x <= b.x1 && y >= b.y0 && y <= b.y1,
    );
    if (hit) {
      setTab("blocks");
      focusBlock(hit);
    }
  }

  if (extracting) {
    const s = statusQuery.data;
    return (
      <div
        role="status"
        aria-live="polite"
        className="rounded-lg border border-slate-200 bg-white p-8 text-center"
      >
        <p className="text-lg font-medium">문서 내용을 확인하는 중입니다.</p>
        {s && s.pageCount ? (
          <p className="mt-1 text-slate-600">
            페이지 {s.pagesDone} / {s.pageCount}
          </p>
        ) : null}
        <div className="mx-auto mt-3 h-2 max-w-sm overflow-hidden rounded bg-slate-200">
          <div
            className="h-full bg-blue-600 transition-all"
            style={{ width: `${s?.processingProgress ?? 0}%` }}
          />
        </div>
        <button
          type="button"
          onClick={async () => {
            await cancelExtraction(doc.id).catch(() => undefined);
            void queryClient.invalidateQueries({ queryKey: ["document", doc.id] });
          }}
          className="mt-4 rounded border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
        >
          취소
        </button>
      </div>
    );
  }

  if (doc.processingStatus === "extraction_failed") {
    return (
      <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-6 text-center">
        <p className="font-semibold text-red-800">문서 내용을 읽지 못했습니다.</p>
        <p className="mt-1 text-sm text-red-700">
          다시 시도해 보세요. 스캔한 이미지로만 된 문서라면
          <br />
          [안내] 탭의 [이미지 페이지 읽기]로 글자를 읽을 수 있어요.
        </p>
        <button
          type="button"
          onClick={async () => {
            await retryExtraction(doc.id).catch(() => undefined);
            void queryClient.invalidateQueries({ queryKey: ["document", doc.id] });
          }}
          className="mt-3 rounded bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
        >
          다시 시도
        </button>
      </div>
    );
  }

  const panelTabs: { key: PanelTab; label: string }[] = [
    { key: "text", label: "본문" },
    { key: "blocks", label: "구역별 보기" },
    { key: "tables", label: "표" },
    { key: "notes", label: "안내" },
  ];

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
      <div className="h-[640px]">
        <PdfViewer
          fileUrl={fileUrl}
          page={page}
          pageCount={pageCount}
          onPageChange={(p) => {
            setPage(p);
            setHighlights([]);
            setSelectedBlockId(null);
          }}
          highlights={highlights}
          flashKey={flashKey}
          onPagePointerDown={handlePdfClick}
        />
      </div>

      <aside className="flex h-[640px] flex-col rounded-lg border border-slate-200 bg-white">
        <div role="tablist" className="flex border-b border-slate-100 text-sm">
          {panelTabs.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              onClick={() => setTab(t.key)}
              className={`px-3 py-2 ${
                tab === t.key
                  ? "border-b-2 border-blue-600 font-semibold text-blue-700"
                  : "text-slate-500 hover:text-slate-800"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-auto p-3 text-sm">
          {tab === "text" && (
            <div>
              <label className="mb-2 flex items-center gap-2 text-xs text-slate-500">
                <input
                  type="checkbox"
                  checked={showRaw}
                  onChange={(e) => setShowRaw(e.target.checked)}
                />
                정리하지 않은 원문 그대로 보기
              </label>
              {pageQuery.isLoading && <p className="text-slate-500">불러오는 중…</p>}
              {pageQuery.data && (
                <p className="whitespace-pre-wrap leading-relaxed">
                  {showRaw ? pageQuery.data.rawText : pageQuery.data.normalizedText ||
                    "이 페이지에서 읽을 수 있는 글자가 없어요."}
                </p>
              )}
            </div>
          )}

          {tab === "blocks" && (
            <div>
              <label className="mb-2 flex items-center gap-2 text-xs text-slate-500">
                <input
                  type="checkbox"
                  checked={includeBands}
                  onChange={(e) => setIncludeBands(e.target.checked)}
                />
                반복되는 머리글·바닥글도 표시
              </label>
              {blocksQuery.isLoading && <p className="text-slate-500">불러오는 중…</p>}
              <ol className="flex flex-col gap-1.5">
                {(blocksQuery.data ?? [])
                  .filter((b) => b.text.trim())
                  .map((b) => (
                    <li key={b.id}>
                      <button
                        type="button"
                        onClick={() => focusBlock(b)}
                        className={`w-full rounded border px-2.5 py-1.5 text-left leading-snug ${
                          selectedBlockId === b.id
                            ? "border-amber-400 bg-amber-50"
                            : "border-slate-150 border-slate-200 hover:bg-slate-50"
                        }`}
                      >
                        {(b.isHeader || b.isFooter) && (
                          <span className="mr-1 rounded bg-slate-100 px-1 text-[10px] text-slate-500">
                            반복 문구
                          </span>
                        )}
                        {b.isCaption && (
                          <span className="mr-1 rounded bg-blue-50 px-1 text-[10px] text-blue-600">
                            설명 글
                          </span>
                        )}
                        {b.isTable && (
                          <span className="mr-1 rounded bg-green-50 px-1 text-[10px] text-green-700">
                            표
                          </span>
                        )}
                        {b.text.length > 180 ? `${b.text.slice(0, 180)}…` : b.text}
                      </button>
                    </li>
                  ))}
              </ol>
            </div>
          )}

          {tab === "tables" && (
            <div className="flex flex-col gap-3">
              {tablesQuery.isLoading && <p className="text-slate-500">불러오는 중…</p>}
              {(tablesQuery.data ?? []).length === 0 && !tablesQuery.isLoading && (
                <p className="text-slate-500">이 문서에서 표를 찾지 못했어요.</p>
              )}
              {(tablesQuery.data ?? []).map((t) => (
                <div key={t.id} className="rounded border border-slate-200 p-2">
                  <div className="mb-1 flex items-center justify-between">
                    <button
                      type="button"
                      onClick={() => {
                        setPage(t.pageNumber);
                        setHighlights([{ x0: t.x0, y0: t.y0, x1: t.x1, y1: t.y1 }]);
                        setFlashKey((k) => k + 1);
                      }}
                      className="text-blue-700 hover:underline"
                    >
                      {t.pageNumber}쪽의 표 {t.tableIndex + 1}
                    </button>
                    <span className="text-xs text-slate-500">
                      {t.rowCount}행 × {t.columnCount}열
                    </span>
                  </div>
                  {(t.confidence < 0.8 || t.extractionStatus !== "extracted") && (
                    <p className="mb-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">
                      표의 일부 내용을 정확히 읽지 못했을 수 있습니다. 원문과 함께 확인해
                      주세요.
                    </p>
                  )}
                  {t.markdownText && (
                    <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">
                      {t.markdownText}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}

          {tab === "notes" && (
            <div className="flex flex-col gap-3">
              <OcrPanel documentId={doc.id} ocrPageCount={ocrPages.length} />
              {failedPages.length > 0 && (
                <div className="rounded bg-red-50 px-3 py-2 text-red-800">
                  <p className="font-medium">읽지 못한 페이지</p>
                  <p className="mt-0.5 text-xs">
                    {failedPages.map((p) => `${p.pageNumber}쪽`).join(", ")}
                  </p>
                </div>
              )}
              {uncertainPages.length > 0 && (
                <div className="rounded bg-slate-50 px-3 py-2 text-slate-600">
                  <p className="font-medium">읽는 순서가 정확하지 않을 수 있는 페이지</p>
                  <p className="mt-0.5 text-xs">
                    {uncertainPages.map((p) => `${p.pageNumber}쪽`).join(", ")} — 원문과 함께
                    확인해 주세요.
                  </p>
                </div>
              )}
              {ocrPages.length === 0 && failedPages.length === 0 && uncertainPages.length === 0 && (
                <p className="rounded bg-green-50 px-3 py-2 text-green-800">
                  본문을 읽었습니다. 특별한 주의사항이 없어요.
                </p>
              )}
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}
