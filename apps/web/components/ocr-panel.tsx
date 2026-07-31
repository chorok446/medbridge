"use client";

import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { cancelOcr, getOcrStatus, retryOcr, startOcr } from "@/lib/api/ocr";

/**
 * 이미지 페이지 읽기(OCR) 패널 — 텍스트 확인 화면의 안내 탭에서 사용.
 * 기술 용어(엔진·DPI·confidence 숫자)는 표시하지 않는다.
 */
export function OcrPanel({ documentId, ocrPageCount }: { documentId: string; ocrPageCount: number }) {
  const queryClient = useQueryClient();

  const statusQuery = useQuery({
    queryKey: ["ocr-status", documentId],
    queryFn: () => getOcrStatus(documentId),
    refetchInterval: (q) => (q.state.data?.running ? 1000 : false),
  });

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["ocr-status", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["document", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["extraction-status", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["extraction-pages", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["extraction-page", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["extraction-blocks", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["extraction-tables", documentId] });
  };

  // OCR이 끝나는 순간(running true→false) 본문·블록 화면을 새로 고친다
  const running = statusQuery.data?.running ?? false;
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) invalidateAll();
    wasRunning.current = running;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  const startMutation = useMutation({ mutationFn: () => startOcr(documentId), onSettled: invalidateAll });
  const retryMutation = useMutation({ mutationFn: () => retryOcr(documentId), onSettled: invalidateAll });
  const cancelMutation = useMutation({ mutationFn: () => cancelOcr(documentId), onSettled: invalidateAll });

  const s = statusQuery.data;
  if (!s) return null;

  if (!s.available) {
    return (
      <p className="rounded bg-slate-50 px-3 py-2 text-slate-600">
        이 버전에서는 이미지 페이지 읽기를 사용할 수 없어요. 다음 업데이트를 확인해 주세요.
      </p>
    );
  }

  if (s.running) {
    return (
      <div role="status" aria-live="polite" className="rounded bg-blue-50 px-3 py-3">
        <p className="font-medium text-blue-800">이미지로 된 페이지를 읽는 중입니다.</p>
        <p className="mt-0.5 text-blue-700">
          {s.done} / {s.totalTargets}페이지
        </p>
        <div className="mt-2 h-2 overflow-hidden rounded bg-blue-100">
          <div
            className="h-full bg-blue-600 transition-all"
            style={{ width: `${s.totalTargets ? Math.round((s.done / s.totalTargets) * 100) : 0}%` }}
          />
        </div>
        <button
          type="button"
          onClick={() => cancelMutation.mutate()}
          className="mt-2 rounded border border-blue-300 px-3 py-1.5 text-blue-800 hover:bg-blue-100"
        >
          취소
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {s.remainingOcrPages.length > 0 && (
        <div className="rounded bg-amber-50 px-3 py-3 text-amber-800">
          <p>
            이미지로 저장된 페이지가 {s.remainingOcrPages.length}개 있어요. 아래 버튼을 누르면
            글자를 읽어 본문에 추가합니다. 페이지당 몇 초 정도 걸려요.
          </p>
          <button
            type="button"
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending}
            className="mt-2 rounded bg-amber-600 px-4 py-2 font-medium text-white hover:bg-amber-700 disabled:opacity-50"
          >
            이미지 페이지 읽기
          </button>
        </div>
      )}
      {s.lowConfidencePages.length > 0 && (
        <div className="rounded bg-slate-50 px-3 py-2 text-slate-700">
          <p>
            {s.lowConfidencePages.map((p) => `${p}쪽`).join(", ")} — 일부 글자를 정확히 읽지
            못했을 수 있습니다. 원문과 함께 확인해 주세요.
          </p>
          <button
            type="button"
            onClick={() => retryMutation.mutate()}
            disabled={retryMutation.isPending}
            className="mt-1.5 rounded border border-slate-300 px-3 py-1.5 hover:bg-white disabled:opacity-50"
          >
            다시 읽기
          </button>
        </div>
      )}
      {s.failed > 0 && (
        <div className="rounded bg-red-50 px-3 py-2 text-red-800">
          <p>읽지 못한 이미지 페이지가 {s.failed}개 있어요.</p>
          <button
            type="button"
            onClick={() => retryMutation.mutate()}
            disabled={retryMutation.isPending}
            className="mt-1.5 rounded border border-red-300 px-3 py-1.5 hover:bg-red-100 disabled:opacity-50"
          >
            다시 읽기
          </button>
        </div>
      )}
      {s.remainingOcrPages.length === 0 &&
        s.failed === 0 &&
        s.done > 0 &&
        s.lowConfidencePages.length === 0 && (
          <p className="rounded bg-green-50 px-3 py-2 text-green-800">
            이미지 페이지 읽기가 끝났어요. 본문에서 확인할 수 있습니다.
          </p>
        )}
      {ocrPageCount === 0 && s.done === 0 && null}
    </div>
  );
}
