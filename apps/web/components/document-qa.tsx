"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PdfViewer } from "@/components/pdf-viewer";
import {
  askQuestion,
  createThread,
  deleteThread,
  getThread,
  listThreads,
  retryAnswer,
} from "@/lib/api/qa";
import type { DocumentSummary } from "@/types/api";
import type { Rect } from "@/types/extraction";
import type { QaClaim, QaMessage } from "@/types/qa";

interface Props {
  doc: DocumentSummary;
  fileUrl: string;
}

const EXAMPLE_QUESTIONS = [
  "이 문서의 핵심 내용은 무엇인가요?",
  "주요 수치나 기준이 있나요?",
  "이 자료에서 강조하는 점은 무엇인가요?",
];

function statusNotice(status: QaMessage["status"]): string | null {
  switch (status) {
    case "not_found":
      return "이 자료에서는 확인할 수 없는 내용이에요.";
    case "insufficient_evidence":
      return "문서에서 충분한 근거를 찾지 못했어요.";
    case "conflicting_evidence":
      return "문서 안에 서로 다른 내용이 있어요. 양쪽 출처를 함께 확인해 주세요.";
    case "revision_changed":
      return "문서 내용이 변경되어 답변을 다시 만들어야 합니다.";
    case "failed":
      return "답변을 만들지 못했어요.";
    default:
      return null;
  }
}

export function DocumentQa({ doc, fileUrl }: Props) {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [highlights, setHighlights] = useState<Rect[]>([]);
  const [flashKey, setFlashKey] = useState(0);
  const [input, setInput] = useState("");
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const pageCount = doc.pageCount ?? 0;

  const threadsQuery = useQuery({
    queryKey: ["qa-threads", doc.id],
    queryFn: () => listThreads(doc.id),
  });
  const threads = threadsQuery.data ?? [];
  const effectiveThreadId = selectedThreadId ?? threads[0]?.id ?? null;

  const detailQuery = useQuery({
    queryKey: ["qa-thread", doc.id, effectiveThreadId],
    queryFn: () => getThread(doc.id, effectiveThreadId as string),
    enabled: effectiveThreadId !== null,
  });
  const messages = detailQuery.data?.messages ?? [];

  const askMutation = useMutation({
    mutationFn: async (question: string) => {
      let threadId = effectiveThreadId;
      if (threadId === null) {
        const created = await createThread(doc.id);
        threadId = created.thread.id;
        setSelectedThreadId(threadId);
      }
      return askQuestion(doc.id, threadId, question);
    },
    onSuccess: (detail) => {
      queryClient.setQueryData(["qa-thread", doc.id, detail.thread.id], detail);
      void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
      setInput("");
    },
  });

  const retryMutation = useMutation({
    mutationFn: () => retryAnswer(doc.id, effectiveThreadId as string),
    onSuccess: (detail) =>
      queryClient.setQueryData(["qa-thread", doc.id, detail.thread.id], detail),
  });

  const newThreadMutation = useMutation({
    mutationFn: () => createThread(doc.id),
    onSuccess: (created) => {
      setSelectedThreadId(created.thread.id);
      void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (threadId: string) => deleteThread(doc.id, threadId),
    onSuccess: () => {
      setSelectedThreadId(null);
      void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
    },
  });

  const askError = askMutation.error as { status?: number } | null;
  const providerUnavailable = askError?.status === 501;
  const consentNeeded = askError?.status === 403;
  const busy = askMutation.isPending || retryMutation.isPending;

  function navigate(ref: { pageNumber: number; bbox: [number, number, number, number] }) {
    setPage(ref.pageNumber);
    setHighlights([{ x0: ref.bbox[0], y0: ref.bbox[1], x1: ref.bbox[2], y1: ref.bbox[3] }]);
    setFlashKey((k) => k + 1);
  }

  function submit() {
    const q = input.trim();
    if (!q || busy) return;
    askMutation.mutate(q);
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_440px]">
      <div className="h-[640px]">
        <PdfViewer
          fileUrl={fileUrl}
          page={page}
          pageCount={pageCount}
          onPageChange={(p) => {
            setPage(p);
            setHighlights([]);
          }}
          highlights={highlights}
          flashKey={flashKey}
        />
      </div>

      <aside className="flex h-[640px] flex-col rounded-lg border border-slate-200 bg-white">
        <div className="flex items-center gap-2 border-b border-slate-100 p-2 text-sm">
          <button
            type="button"
            onClick={() => newThreadMutation.mutate()}
            className="rounded border border-slate-300 px-2.5 py-1 hover:bg-slate-50"
          >
            새 대화
          </button>
          {threads.length > 0 && (
            <select
              value={effectiveThreadId ?? ""}
              onChange={(e) => setSelectedThreadId(e.target.value)}
              aria-label="이전 대화 선택"
              className="min-w-0 flex-1 truncate rounded border border-slate-300 px-2 py-1"
            >
              {threads.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title || "새 대화"}
                </option>
              ))}
            </select>
          )}
          {effectiveThreadId && (
            <button
              type="button"
              onClick={() => {
                if (window.confirm("이 대화를 삭제할까요?")) deleteMutation.mutate(effectiveThreadId);
              }}
              className="rounded border border-red-200 px-2.5 py-1 text-red-700 hover:bg-red-50"
            >
              삭제
            </button>
          )}
        </div>

        <div className="flex-1 overflow-auto p-3">
          {messages.length === 0 && !busy && (
            <div className="text-sm text-slate-600">
              <p className="mb-2 font-medium">이 문서에 대해 질문해 보세요.</p>
              <ul className="flex flex-col gap-1.5">
                {EXAMPLE_QUESTIONS.map((q) => (
                  <li key={q}>
                    <button
                      type="button"
                      onClick={() => setInput(q)}
                      className="w-full rounded border border-slate-200 px-3 py-2 text-left hover:bg-slate-50"
                    >
                      {q}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ol className="flex flex-col gap-3">
            {messages.map((m) =>
              m.role === "user" ? (
                <li key={m.id} className="self-end rounded-lg bg-blue-600 px-3 py-2 text-sm text-white">
                  {m.content}
                </li>
              ) : (
                <li key={m.id} className="rounded-lg bg-slate-50 px-3 py-2 text-sm">
                  <AssistantMessage message={m} onNavigate={navigate} />
                </li>
              ),
            )}
          </ol>

          {busy && (
            <p role="status" className="mt-3 text-sm text-slate-500">
              답변을 찾고 있어요…
            </p>
          )}

          {providerUnavailable && (
            <p role="alert" className="mt-3 rounded bg-slate-100 px-3 py-2 text-sm text-slate-700">
              질문 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.{" "}
              <a href="/settings" className="text-blue-700 hover:underline">
                설정으로 이동
              </a>
            </p>
          )}
          {consentNeeded && (
            <p role="alert" className="mt-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800">
              외부 모델을 쓰려면 앱 설정과 이 문서에서 외부 전송을 먼저 허용해 주세요.
            </p>
          )}

          {(retryMutation.isSuccess || askMutation.isSuccess) &&
            messages.some((m) => m.status === "failed" || m.status === "revision_changed") && (
              <button
                type="button"
                onClick={() => retryMutation.mutate()}
                disabled={busy}
                className="mt-2 rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                다시 시도
              </button>
            )}
        </div>

        <div className="border-t border-slate-100 p-2">
          <p className="mb-1.5 px-1 text-[11px] text-slate-400">
            업로드한 문서 기준 답변이에요. 실제 진단·처방·응급 판단에 사용하지 마세요.
          </p>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="이 문서에 대해 질문하기"
              aria-label="질문 입력"
              className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={busy || !input.trim()}
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              보내기
            </button>
          </form>
        </div>
      </aside>
    </div>
  );
}

function AssistantMessage({
  message,
  onNavigate,
}: {
  message: QaMessage;
  onNavigate: (ref: { pageNumber: number; bbox: [number, number, number, number] }) => void;
}) {
  const notice = statusNotice(message.status);
  // 확정 사실로 보여줄 근거 있는 주장만 출처 배지를 표시한다
  const supportedClaims = message.claims.filter(
    (c) => c.verificationStatus === "supported" || c.verificationStatus === "conflicting",
  );
  return (
    <div>
      {notice && <p className="mb-1 text-xs text-slate-500">{notice}</p>}
      <p className="whitespace-pre-wrap leading-snug text-slate-800">{message.content}</p>
      {supportedClaims.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1.5">
          {supportedClaims.map((c, i) => (
            <li key={i} className="border-t border-slate-200 pt-1.5">
              <p className="text-xs leading-snug text-slate-600">{c.text}</p>
              <ClaimSources claim={c} onNavigate={onNavigate} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ClaimSources({
  claim,
  onNavigate,
}: {
  claim: QaClaim;
  onNavigate: (ref: { pageNumber: number; bbox: [number, number, number, number] }) => void;
}) {
  const seen = new Set<string>();
  const refs = claim.sourceRefs.filter((r) => {
    const key = `${r.pageNumber}-${r.blockId}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (refs.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      <span className="text-[11px] text-slate-400">출처:</span>
      {refs.map((r) => (
        <button
          key={`${r.pageNumber}-${r.blockId}`}
          type="button"
          onClick={() => onNavigate({ pageNumber: r.pageNumber, bbox: r.bbox })}
          className="rounded bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700 hover:bg-blue-100"
        >
          {r.pageNumber}쪽
        </button>
      ))}
    </div>
  );
}
