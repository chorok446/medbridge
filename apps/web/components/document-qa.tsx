"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { CitedText, SourceList } from "@/components/citations";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { PdfViewer } from "@/components/pdf-viewer";
import {
  type CitationSource,
  hasCitations,
  tokenizeCitations,
} from "@/lib/citations";
import { usePdfNavigation } from "@/hooks/use-pdf-navigation";
import { useQaStream } from "@/hooks/use-qa-stream";
import {
  createThread,
  deleteThread,
  getThread,
  listThreads,
  retryAnswer,
} from "@/lib/api/qa";
import type { DocumentSummary } from "@/types/api";
import type { QaMessage } from "@/types/qa";

type Bbox = [number, number, number, number];
type NavigateRef = { pageNumber: number; bbox: Bbox; bboxes?: Bbox[] };

const PHASE_LABEL: Record<string, string> = {
  connecting: "질문을 준비하고 있어요…",
  retrieving: "문서에서 근거를 찾고 있어요…",
  generating: "답변을 작성하고 있어요…",
  finalizing: "출처를 확인하고 마무리하고 있어요…",
  cancelling: "중단하고 있어요…",
};

interface Props {
  doc: DocumentSummary;
  fileUrl: string;
}

const EXAMPLE_QUESTIONS = [
  "이 문서의 핵심 내용은 무엇인가요?",
  "주요 수치나 기준이 있나요?",
  "이 자료에서 강조하는 점은 무엇인가요?",
];

// 답변 깊이. 요약 파이프라인의 learner_level과 같은 3단계다.
const LEVELS = [
  { key: "concise", label: "간단히" },
  { key: "nursing_student", label: "간호학생" },
  { key: "experienced_nurse", label: "경력간호사" },
] as const;

type LearnerLevel = (typeof LEVELS)[number]["key"];
const LEVEL_STORAGE_KEY = "medbridge.qa.learnerLevel";

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
    case "cancelled":
      return "답변을 중단했어요.";
    case "interrupted":
      return "연결이 끊겨 답변을 완료하지 못했어요. 다시 시도해 주세요.";
    case "failed":
      return "답변을 만들지 못했어요.";
    default:
      return null;
  }
}

export function DocumentQa({ doc, fileUrl }: Props) {
  const queryClient = useQueryClient();
  const nav = usePdfNavigation();
  const [input, setInput] = useState("");
  const [confirmDeleteThread, setConfirmDeleteThread] = useState(false);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  // 마지막 선택을 브라우저에 기억한다 — 서버에 저장하지 않는다. 이 패널은
  // useSearchParams 아래 Suspense 안이라 정적 프리렌더에 들어가지 않으므로
  // 첫 렌더에서 바로 읽어도 하이드레이션이 어긋나지 않는다.
  // 전송 중 잠금. busy(렌더 값)는 createThread를 기다리는 동안 아직 false다.
  const sendingRef = useRef(false);
  const [level, setLevel] = useState<LearnerLevel>(() => {
    if (typeof window === "undefined") return "nursing_student";
    const saved = window.localStorage.getItem(LEVEL_STORAGE_KEY);
    return LEVELS.some((l) => l.key === saved) ? (saved as LearnerLevel) : "nursing_student";
  });
  const pageCount = doc.pageCount ?? 0;
  const stream = useQaStream(doc.id);

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

  const retryMutation = useMutation({
    mutationFn: () => retryAnswer(doc.id, effectiveThreadId as string, level),
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

  const lastFollowups =
    messages.filter((m) => m.role === "assistant").at(-1)?.followups ?? [];
  const providerUnavailable = stream.state.phase === "failed" && stream.state.errorStatus === 501;
  const consentNeeded = stream.state.phase === "failed" && stream.state.errorStatus === 403;
  // 청크가 아직 없는 문서(막 올린 자료)는 서버가 409로 막는다. 안내가 없으면 사용자에겐
  // 버튼이 죽은 것으로 보인다 — PRODUCT.md "오류는 다음 행동과 함께".
  const notReady = stream.state.phase === "failed" && stream.state.errorStatus === 409;
  const streamFailed =
    stream.state.phase === "failed" &&
    !providerUnavailable &&
    !consentNeeded &&
    !notReady;
  const busy = stream.active || retryMutation.isPending;

  const navigate = nav.navigate;

  function chooseLevel(next: LearnerLevel) {
    setLevel(next);
    window.localStorage.setItem(LEVEL_STORAGE_KEY, next);
  }

  async function askQuestion(raw: string) {
    const q = raw.trim();
    if (!q || busy) return;
    // busy는 렌더 시점 값이라 createThread를 기다리는 동안 아직 false다. 예시·후속 질문
    // 버튼을 연타하면 그 틈으로 두 번째 클릭이 들어와 스레드가 둘 생기고, 두 번째 요청이
    // 첫 번째를 abort해 첫 질문은 답을 못 받은 채 남는다. ref로 즉시 잠근다.
    if (sendingRef.current) return;
    sendingRef.current = true;
    try {
      let threadId = effectiveThreadId;
      if (threadId === null) {
        const created = await createThread(doc.id);
        threadId = created.thread.id;
        setSelectedThreadId(threadId);
        void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
      }
      setPendingQuestion(q);
      setInput("");
      await stream.ask(threadId, q, level);
      // 스트림 종료(완료/취소/중단/실패) → DB에 확정된 메시지를 다시 불러온다.
      setPendingQuestion(null);
      void queryClient.invalidateQueries({ queryKey: ["qa-thread", doc.id, threadId] });
      void queryClient.invalidateQueries({ queryKey: ["qa-threads", doc.id] });
    } finally {
      sendingRef.current = false;
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_440px]">
      <div className="h-[640px]">
        <PdfViewer
          fileUrl={fileUrl}
          page={nav.page}
          pageCount={pageCount}
          onPageChange={nav.changePage}
          highlights={nav.highlights}
          flashKey={nav.flashKey}
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
              onClick={() => setConfirmDeleteThread(true)}
              className="rounded border border-red-200 px-2.5 py-1 text-red-700 hover:bg-red-50"
            >
              삭제
            </button>
          )}
        </div>

        <div className="flex-1 overflow-auto p-3">
          {messages.length === 0 && !busy && (
            <div className="flex flex-col items-center gap-4 px-2 py-6 text-center">
              <p className="text-lg font-semibold text-slate-900">
                이 문서에 대해 질문해 보세요.
              </p>
              <ul className="flex w-full flex-col gap-2">
                {EXAMPLE_QUESTIONS.map((q) => (
                  <li key={q}>
                    <button
                      type="button"
                      onClick={() => void askQuestion(q)}
                      className="w-full rounded-lg border border-slate-200 px-4 py-3 text-left text-sm hover:bg-slate-50"
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

          {stream.active && (
            <div className="mt-3 flex flex-col gap-2">
              {pendingQuestion && (
                <p className="self-end rounded-lg bg-blue-600 px-3 py-2 text-sm text-white">
                  {pendingQuestion}
                </p>
              )}
              <div className="rounded-lg bg-slate-50 px-3 py-2 text-sm">
                {stream.state.claims.length > 0 && (
                  <ul className="mb-2 flex flex-col gap-1.5">
                    {stream.state.claims.map((c) => (
                      <li key={c.claimIndex} className="border-t border-slate-200 pt-1.5 first:border-0 first:pt-0">
                        <p className="text-xs leading-snug text-slate-700">{c.text}</p>
                        <SourceBadges sources={c.sources} onNavigate={navigate} />
                      </li>
                    ))}
                  </ul>
                )}
                <div className="flex items-center justify-between gap-2">
                  <p role="status" className="text-sm text-slate-500">
                    {PHASE_LABEL[stream.state.phase] ?? "답변을 찾고 있어요…"}
                  </p>
                  <button
                    type="button"
                    onClick={() => void stream.cancel()}
                    disabled={stream.state.phase === "cancelling"}
                    className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-50"
                  >
                    중단
                  </button>
                </div>
              </div>
            </div>
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
          {notReady && (
            <p role="alert" className="mt-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800">
              아직 질문할 준비가 되지 않았어요. 위쪽 [텍스트 확인] 탭에서 “문서 검색
              준비하기”를 먼저 눌러 주세요.
            </p>
          )}
          {streamFailed && (
            <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-800">
              답변을 만들지 못했어요. 잠시 후 다시 질문해 주세요.
            </p>
          )}

          {!busy && lastFollowups.length > 0 && (
            <ul className="mt-3 flex flex-wrap gap-1.5">
              {lastFollowups.map((q) => (
                <li key={q}>
                  {/* 입력창에 채우지 않고 바로 보낸다 — 한 번 더 누르게 만들 이유가 없다. */}
                  <button
                    type="button"
                    onClick={() => void askQuestion(q)}
                    className="rounded-full border border-slate-200 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
                  >
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {!stream.active &&
            messages.some(
              (m) =>
                m.status === "failed" ||
                m.status === "revision_changed" ||
                m.status === "interrupted",
            ) && (
              <button
                type="button"
                onClick={() => retryMutation.mutate()}
                disabled={busy}
                // Secondary로 둔다. 하단 "보내기"가 이미 파란 버튼이라, 여기도
                // 파랗게 하면 화면에 "다음 할 일"이 둘이 되어 우선순위가 사라진다.
                // 다른 화면의 "다시 시도"도 전부 테두리형이라 모양이 통일된다.
                className="mt-2 rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:opacity-50"
              >
                다시 시도
              </button>
            )}
          {retryMutation.isError && (
            <p role="alert" className="mt-2 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800">
              지금은 다시 시도할 수 없어요. 아래에 질문을 다시 입력해 주세요.
            </p>
          )}
        </div>

        <div className="border-t border-slate-100 p-2">
          {/* 대화가 시작된 뒤에도 어떤 수준이 적용 중인지 보이고 바꿀 수 있어야 한다 */}
          <fieldset className="mb-2 flex justify-center gap-1.5">
            <legend className="sr-only">학습 수준</legend>
            {LEVELS.map((l) => (
              <label
                key={l.key}
                className={`cursor-pointer rounded-full border px-3 py-1 text-xs ${
                  level === l.key
                    ? "border-blue-600 bg-blue-50 text-blue-700"
                    : "border-slate-200 text-slate-600 hover:bg-slate-50"
                }`}
              >
                <input
                  type="radio"
                  name="learner-level"
                  className="sr-only"
                  checked={level === l.key}
                  onChange={() => chooseLevel(l.key)}
                />
                {l.label}
              </label>
            ))}
          </fieldset>
          <p className="mb-1.5 px-1 text-xs text-slate-500">
            업로드한 문서 기준 답변이에요. 실제 진단·처방·응급 판단에 사용하지 마세요.
          </p>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void askQuestion(input);
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

      <ConfirmDialog
        open={confirmDeleteThread}
        title="이 대화를 삭제할까요?"
        description="주고받은 질문과 답변이 모두 지워지며 되돌릴 수 없습니다."
        confirmLabel="삭제"
        onConfirm={() => {
          if (effectiveThreadId) deleteMutation.mutate(effectiveThreadId);
          setConfirmDeleteThread(false);
        }}
        onCancel={() => setConfirmDeleteThread(false)}
      />
    </div>
  );
}

function AssistantMessage({
  message,
  onNavigate,
}: {
  message: QaMessage;
  onNavigate: (ref: NavigateRef) => void;
}) {
  const notice = statusNotice(message.status);
  // 확정 사실로 보여줄 근거 있는 주장만 출처 배지를 표시한다
  const supportedClaims = message.claims.filter(
    (c) => c.verificationStatus === "supported" || c.verificationStatus === "conflicting",
  );
  // 인용 번호 i는 claims[i]에 대응한다 — 서버가 마커를 그 번호로 맞춰 보낸다. 그래서
  // 배열 자리는 그대로 두고, 보여주면 안 되는 자리만 null로 비운다. 걸러내며 당기면
  // 번호가 밀려 사용자가 누른 인용이 다른 근거로 이동한다.
  const sources: (CitationSource[] | null)[] = message.claims.map((c) =>
    // 검증에 실패해 화면에 내보내지 않기로 한 주장의 페이지를 '출처'로 제시하지 않는다.
    // 출처가 아예 없는 주장도 마찬가지 — 없는 근거를 1쪽으로 지어내지 않는다.
    c.verificationStatus === "unsupported" || c.sourceRefs.length === 0
      ? null
      : c.sourceRefs.map((r) => ({
          pageNumber: r.pageNumber,
          sectionTitle: r.sectionTitle ?? null,
          sourceMethod: r.sourceMethod,
          bbox: r.bbox,
        })),
  );
  const tokens = tokenizeCitations(message.content, sources.length);
  const cited = hasCitations(tokens);
  // 본문에 실제로 등장한 인용 번호만 출처 목록에 올린다. claims 배열 전체를 올리면
  // 모델이 일부 주장에만 마커를 단 답변에서(로컬 모델에서 흔하다) 본문에는 [1]만
  // 보이는데 목록에는 1·2·3이 뜬다 — 사용자는 '2'가 가리키는 문장을 답변에서 찾다가
  // 못 찾는다. 번호 자체는 claims 자리를 그대로 써야 인용 pill과 목록이 같은 곳을
  // 가리킨다(걸러내며 당기면 번호가 밀린다).
  const citedIndexes = new Set(
    tokens.flatMap((t) => (t.kind === "citation" ? [t.claimIndex] : [])),
  );

  return (
    <div>
      {notice && <p className="mb-1 text-xs text-slate-500">{notice}</p>}
      {cited ? (
        <>
          <CitedText
            content={message.content}
            sources={sources}
            tokens={tokens}
            onNavigate={(s) => onNavigate({ pageNumber: s.pageNumber, bbox: s.bbox })}
          />
          <SourceList
            groups={sources.flatMap((refs, i) =>
              refs && citedIndexes.has(i) ? [{ number: i + 1, refs }] : [],
            )}
            onNavigate={(s) =>
              onNavigate({ pageNumber: s.pageNumber, bbox: s.bbox, bboxes: s.bboxes })
            }
          />
        </>
      ) : (
        <>
          {/* 마커 없는 옛 메시지 — 기존 렌더를 그대로 둔다. 백필하지 않으므로 이 경로는 남는다. */}
          <p className="max-w-[68ch] whitespace-pre-wrap text-[17px] leading-[1.7] text-slate-900">
            {message.content}
          </p>
          {supportedClaims.length > 0 && (
            <ul className="mt-2 flex flex-col gap-1.5">
              {supportedClaims.map((c, i) => (
                <li key={i} className="border-t border-slate-200 pt-1.5">
                  <p className="text-sm leading-snug text-slate-600">{c.text}</p>
                  <SourceBadges sources={c.sourceRefs} onNavigate={onNavigate} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

type SourceRef = { pageNumber: number; blockId: string; bbox: [number, number, number, number] };

function SourceBadges({
  sources,
  onNavigate,
}: {
  sources: SourceRef[];
  onNavigate: (ref: NavigateRef) => void;
}) {
  // 배지에는 페이지 번호만 보인다 — 출처 목록(SourceList)과 같은 원칙으로, 화면에
  // 똑같이 보이는 배지("3쪽" 여러 개)는 하나로 접는다. 다만 접힌 출처들의 bbox는
  // 전부 실어, 클릭 한 번에 그 페이지의 근거 영역 전부가 하이라이트되게 한다.
  const byPage = new Map<number, Required<NavigateRef>>();
  for (const r of sources) {
    const kept = byPage.get(r.pageNumber);
    if (kept) {
      if (!kept.bboxes.some((b) => b.every((v, i) => v === r.bbox[i]))) {
        kept.bboxes.push(r.bbox);
      }
      continue;
    }
    byPage.set(r.pageNumber, { pageNumber: r.pageNumber, bbox: r.bbox, bboxes: [r.bbox] });
  }
  const refs = [...byPage.values()];
  if (refs.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      <span className="text-xs text-slate-500">출처:</span>
      {refs.map((r) => (
        <button
          key={r.pageNumber}
          type="button"
          onClick={() => onNavigate(r)}
          className="rounded bg-blue-50 px-2 py-0.5 text-xs text-blue-700 hover:bg-blue-100"
        >
          {r.pageNumber}쪽
        </button>
      ))}
    </div>
  );
}

