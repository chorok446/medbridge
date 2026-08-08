"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { DocumentSearch } from "@/components/document-search";
import { ExtractedTable } from "@/components/extracted-table";
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
import { getOcrStatus, startPageOcr } from "@/lib/api/ocr";
import { usePdfNavigation } from "@/hooks/use-pdf-navigation";
import type { DocumentSummary } from "@/types/api";
import type { ExtractionBlock } from "@/types/extraction";

type PanelTab = "text" | "blocks" | "tables" | "search" | "notes";

interface Props {
  doc: DocumentSummary;
  fileUrl: string;
}

/** 추출 결과 검수 화면 — 왼쪽 원문(PDF), 오른쪽 추출 텍스트. 블록 클릭 ↔ 원문 하이라이트. */
export function ExtractionReview({ doc, fileUrl }: Props) {
  const queryClient = useQueryClient();
  const nav = usePdfNavigation();
  const { page } = nav;
  const [tab, setTab] = useState<PanelTab>("text");
  const [includeBands, setIncludeBands] = useState(false);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  // 의학 교재의 표는 열이 대여섯 개씩 된다. 380px 칸에서는 아무리 잘 그려도 가로로
  // 밀어가며 읽어야 해서, 표를 보는 동안만 패널을 넓히는 스위치를 준다. 원문 대조가
  // 표 읽기의 핵심이라 PDF는 좁아질지언정 화면에서 치우지 않는다.
  const [wideTables, setWideTables] = useState(false);

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

  // "안내" 탭(OcrPanel)을 보고 있지 않아도 OCR 완료를 감지해 새로고침한다 —
  // 탭은 조건부 렌더링이라 다른 탭에서 수동 OCR을 실행하면 OcrPanel이 마운트돼
  // 있지 않은 채로 끝날 수 있다. 쿼리 키가 같아 OcrPanel과 폴링을 공유한다.
  const ocrStatusQuery = useQuery({
    queryKey: ["ocr-status", doc.id],
    queryFn: () => getOcrStatus(doc.id),
    enabled: !extracting,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : false),
  });
  const ocrRunning = ocrStatusQuery.data?.running ?? false;
  const ocrWasRunning = useRef(false);
  useEffect(() => {
    // 일시적 조회 실패를 "방금 완료됨"으로 오인하지 않는다 — 성공한 응답을
    // 받을 때까지 이전 상태를 그대로 유지한다.
    if (ocrStatusQuery.isError) return;
    if (ocrWasRunning.current && !ocrRunning) {
      void queryClient.invalidateQueries({ queryKey: ["document", doc.id] });
      void queryClient.invalidateQueries({ queryKey: ["extraction-status", doc.id] });
      void queryClient.invalidateQueries({ queryKey: ["extraction-pages", doc.id] });
      void queryClient.invalidateQueries({ queryKey: ["extraction-page", doc.id] });
      void queryClient.invalidateQueries({ queryKey: ["extraction-blocks", doc.id] });
    }
    ocrWasRunning.current = ocrRunning;
  }, [ocrRunning, ocrStatusQuery.isError, doc.id, queryClient]);

  const pageOcrMutation = useMutation({
    mutationFn: (pageNumber: number) => startPageOcr(doc.id, pageNumber),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["ocr-status", doc.id] });
      void queryClient.invalidateQueries({ queryKey: ["extraction-pages", doc.id] });
    },
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
    nav.highlight({ x0: block.x0, y0: block.y0, x1: block.x1, y1: block.y1 });
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
    { key: "search", label: "검색" },
    { key: "notes", label: "안내" },
  ];

  return (
    <div
      className={`grid gap-4 ${
        wideTables && tab === "tables"
          ? "lg:grid-cols-[minmax(0,1fr)_720px]"
          : "lg:grid-cols-[minmax(0,1fr)_380px]"
      }`}
    >
      <div className="h-[640px]">
        <PdfViewer
          fileUrl={fileUrl}
          page={page}
          pageCount={pageCount}
          onPageChange={(p) => {
            nav.changePage(p);
            setSelectedBlockId(null);
          }}
          highlights={nav.highlights}
          flashKey={nav.flashKey}
          onPagePointerDown={handlePdfClick}
        />
      </div>

      <aside className="flex h-[640px] flex-col rounded-lg border border-slate-200 bg-white">
        <div role="tablist" className="flex border-b border-slate-100 text-sm">
          {panelTabs.map((t) => (
            <button
              key={t.key}
              role="tab"
              // 탭과 내용을 id로 이어야 스크린리더가 "3번째 탭, 패널과 연결됨"이라는
              // 표준 안내를 낼 수 있다. 연결이 없으면 탭 아래 내용이 무엇에 속하는지
              // 알 수 없어, 탭을 옮겨도 내용이 바뀐 줄 모른다.
              id={`extraction-tab-${t.key}`}
              aria-controls={`extraction-panel-${t.key}`}
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

        <div
          role="tabpanel"
          id={`extraction-panel-${tab}`}
          aria-labelledby={`extraction-tab-${tab}`}
          className="flex-1 overflow-auto p-3 text-sm"
        >
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
              {pageQuery.data && showRaw && (
                <p className="whitespace-pre-wrap leading-relaxed">
                  {pageQuery.data.rawText || "이 페이지에서 읽을 수 있는 글자가 없어요."}
                </p>
              )}
              {pageQuery.data && !showRaw && pageQuery.data.normalizedText && (
                <p className="whitespace-pre-wrap leading-relaxed">
                  {pageQuery.data.normalizedText}
                </p>
              )}
              {pageQuery.data && !showRaw && !pageQuery.data.normalizedText && (
                <div className="rounded bg-amber-50 px-3 py-3 text-amber-800">
                  <p>이 페이지에서 읽을 수 있는 글자를 찾지 못했어요.</p>
                  <button
                    type="button"
                    onClick={() => pageOcrMutation.mutate(page)}
                    disabled={pageOcrMutation.isPending || ocrRunning}
                    className="mt-2 rounded bg-amber-700 px-4 py-2 font-medium text-white hover:bg-amber-800 disabled:opacity-50"
                  >
                    현재 페이지 이미지로 읽기
                  </button>
                  {pageOcrMutation.isError && (
                    <p role="alert" className="mt-2 text-red-700">
                      이미지 페이지 읽기를 시작하지 못했습니다. 다시 시도해 주세요.
                    </p>
                  )}
                </div>
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
                        // 선택 상태를 배경색으로만 알리면 스크린리더 사용자는 어느
                        // 구역이 지금 원문에서 하이라이트되고 있는지 알 수 없다.
                        // 원문 대조가 이 화면의 핵심이라 거기서 막힌다(WCAG 1.4.1).
                        aria-pressed={selectedBlockId === b.id}
                        onClick={() => focusBlock(b)}
                        className={`w-full rounded border px-2.5 py-1.5 text-left leading-snug ${
                          selectedBlockId === b.id
                            ? "border-amber-400 bg-amber-50"
                            : "border-slate-200 hover:bg-slate-50"
                        }`}
                      >
                        {(b.isHeader || b.isFooter) && (
                          <span className="mr-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                            반복 문구
                          </span>
                        )}
                        {b.isCaption && (
                          <span className="mr-1 rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700">
                            설명 글
                          </span>
                        )}
                        {b.isTable && (
                          <span className="mr-1 rounded bg-green-50 px-1.5 py-0.5 text-xs text-green-800">
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
            <div className="flex flex-col gap-4">
              {tablesQuery.isLoading && <p className="text-slate-500">불러오는 중…</p>}
              {(tablesQuery.data ?? []).length === 0 && !tablesQuery.isLoading && (
                <p className="text-slate-500">이 문서에서 표를 찾지 못했어요.</p>
              )}
              {/* 규칙 기반 추출의 한계는 모든 표에 똑같이 해당한다. 표마다 반복해서
                  붙이면 매번 뜨는 경고가 되어 아무도 읽지 않고 표만 밀어낸다. */}
              {(tablesQuery.data ?? []).length > 0 && (
                <p className="rounded bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
                  표는 자동으로 읽어낸 것이라 칸이 밀리거나 빠질 수 있어요. 중요한 수치는
                  표 제목을 눌러 원문과 함께 확인해 주세요.
                </p>
              )}
              {(tablesQuery.data ?? []).length > 0 && (
                <button
                  type="button"
                  onClick={() => setWideTables((v) => !v)}
                  aria-pressed={wideTables}
                  className="hidden self-start rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 lg:inline-block"
                >
                  {/* 라벨은 상태와 무관하게 고정한다. 눌린 상태에서 "원문 넓게 보기"로
                      바꾸면 스크린리더가 "원문 넓게 보기, 눌림"으로 읽어, 라벨은 다음
                      동작을 aria-pressed는 현재 상태를 말해 서로 반대로 들린다. */}
                  표 넓게 보기
                </button>
              )}
              {(tablesQuery.data ?? []).map((t) => (
                <ExtractedTable
                  key={t.id}
                  table={t}
                  onLocate={() => {
                    nav.navigate({ pageNumber: t.pageNumber, bbox: [t.x0, t.y0, t.x1, t.y1] });
                    // 다른 이동 경로와 동일하게 이전 블록 선택을 지운다 — 화면마다
                    // 잔여 선택 상태가 다르면 같은 동작이 다르게 보인다.
                    setSelectedBlockId(null);
                  }}
                />
              ))}
            </div>
          )}

          {tab === "search" && (
            <DocumentSearch
              documentId={doc.id}
              onNavigate={(pageNumber, bbox) => {
                nav.navigate({ pageNumber, bbox });
                setSelectedBlockId(null);
              }}
            />
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
