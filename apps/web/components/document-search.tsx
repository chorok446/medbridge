"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { getChunkStatus, rebuildChunks, searchDocument } from "@/lib/api/search";
import type { SearchMode, SearchResultItem } from "@/types/search";

interface Props {
  documentId: string;
  onNavigate: (pageNumber: number, bbox: [number, number, number, number]) => void;
}

function isActiveJobStatus(status: string | null | undefined): boolean {
  return status === "running" || status === "queued";
}

/**
 * 문서 검색 — 청크 기반 키워드/의미 검색. 기술 용어(포트·모델명·원시 점수)는
 * 어디에도 노출하지 않는다.
 */
export function DocumentSearch({ documentId, onNavigate }: Props) {
  const queryClient = useQueryClient();
  const [inputValue, setInputValue] = useState("");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [submitted, setSubmitted] = useState<{ query: string; mode: SearchMode } | null>(null);
  const [blankQueryNotice, setBlankQueryNotice] = useState(false);

  const statusQuery = useQuery({
    queryKey: ["chunk-status", documentId],
    queryFn: () => getChunkStatus(documentId),
    refetchInterval: (q) => (isActiveJobStatus(q.state.data?.jobStatus) ? 1000 : false),
  });

  const rebuildMutation = useMutation({
    mutationFn: () => rebuildChunks(documentId),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["chunk-status", documentId] }),
  });

  const searchQuery = useQuery({
    queryKey: ["document-search", documentId, submitted?.query, submitted?.mode],
    queryFn: () => searchDocument(documentId, { query: submitted!.query, mode: submitted!.mode }),
    enabled: submitted !== null,
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = inputValue.trim();
    if (!trimmed) {
      setBlankQueryNotice(true);
      return;
    }
    setBlankQueryNotice(false);
    setSubmitted({ query: trimmed, mode });
  }

  const chunkCount = statusQuery.data?.chunkCount ?? 0;
  const embeddingAvailable = statusQuery.data?.embeddingAvailable ?? false;
  const results: SearchResultItem[] = searchQuery.data ?? [];
  const usedKeywordOnlyFallback = submitted?.mode === "hybrid" && !embeddingAvailable;

  return (
    <div className="flex flex-col gap-3">
      {statusQuery.isError && (
        <div role="alert" className="rounded bg-red-50 px-3 py-2 text-sm text-red-800">
          <p>검색 준비 상태를 확인하지 못했습니다.</p>
          <button
            type="button"
            onClick={() => statusQuery.refetch()}
            className="mt-1.5 rounded border border-red-300 px-3 py-1.5 text-xs hover:bg-red-100"
          >
            다시 시도
          </button>
        </div>
      )}

      {!statusQuery.isLoading && !statusQuery.isError && chunkCount === 0 && (
        <div className="rounded bg-slate-50 px-3 py-3 text-slate-700">
          <p>이 문서는 아직 검색 준비가 되지 않았어요.</p>
          <button
            type="button"
            onClick={() => rebuildMutation.mutate()}
            disabled={rebuildMutation.isPending || isActiveJobStatus(statusQuery.data?.jobStatus)}
            className="mt-2 rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            문서 검색 준비하기
          </button>
          {isActiveJobStatus(statusQuery.data?.jobStatus) && (
            <p role="status" aria-live="polite" className="mt-1.5 text-xs text-slate-500">
              준비하는 중이에요…
            </p>
          )}
          {rebuildMutation.isError && (
            <p role="alert" className="mt-1.5 text-xs text-red-700">
              준비를 시작하지 못했습니다. 다시 시도해 주세요.
            </p>
          )}
        </div>
      )}

      <form onSubmit={handleSubmit} className="flex flex-col gap-2">
        <div className="flex gap-2">
          <input
            type="search"
            value={inputValue}
            onChange={(e) => {
              setInputValue(e.target.value);
              if (blankQueryNotice) setBlankQueryNotice(false);
            }}
            placeholder="문서에서 찾을 단어나 문장을 입력하세요"
            aria-label="문서 검색어"
            className="flex-1 rounded border border-slate-300 px-3 py-2"
          />
          <button
            type="submit"
            className="rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700"
          >
            검색
          </button>
        </div>
        <div className="flex gap-3 text-xs text-slate-600">
          <label className="flex items-center gap-1.5">
            <input
              type="radio"
              name="search-mode"
              checked={mode === "keyword"}
              onChange={() => setMode("keyword")}
            />
            단어 검색
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="radio"
              name="search-mode"
              checked={mode === "hybrid"}
              onChange={() => setMode("hybrid")}
            />
            의미 검색 포함
          </label>
        </div>
        {blankQueryNotice && (
          <p role="alert" className="text-xs text-red-700">
            검색어를 입력해 주세요.
          </p>
        )}
      </form>

      {usedKeywordOnlyFallback && (
        <p className="rounded bg-slate-50 px-3 py-2 text-xs text-slate-600">
          의미 검색을 사용할 수 없어 단어 검색 결과만 표시합니다.
        </p>
      )}

      {submitted && searchQuery.isLoading && (
        <p role="status" className="text-sm text-slate-500">
          검색하는 중…
        </p>
      )}

      {submitted && searchQuery.isError && (
        <div role="alert" className="rounded bg-red-50 px-3 py-2 text-sm text-red-800">
          <p>검색하지 못했습니다.</p>
          <button
            type="button"
            onClick={() => searchQuery.refetch()}
            className="mt-1.5 rounded border border-red-300 px-3 py-1.5 text-xs hover:bg-red-100"
          >
            다시 시도
          </button>
        </div>
      )}

      {submitted && searchQuery.isSuccess && results.length === 0 && (
        <p className="rounded bg-slate-50 px-3 py-2 text-sm text-slate-600">
          검색 결과가 없어요. 다른 단어로 찾아보세요.
        </p>
      )}

      {results.length > 0 && (
        <ol className="flex flex-col gap-2">
          {results.map((r) => (
            <li key={r.chunkId}>
              <button
                type="button"
                onClick={() => onNavigate(r.sourceRefs[0].pageNumber, r.sourceRefs[0].bbox)}
                className="w-full rounded border border-slate-200 px-3 py-2 text-left hover:bg-slate-50"
              >
                <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
                  <span>
                    {r.sectionTitle ? `${r.sectionTitle} · ` : ""}
                    {r.pageStart === r.pageEnd
                      ? `${r.pageStart}쪽`
                      : `${r.pageStart}~${r.pageEnd}쪽`}
                  </span>
                </div>
                <p className="leading-snug text-slate-800">{r.preview}</p>
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
