"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { SourceList } from "@/components/citations";
import { ErrorBox } from "@/components/error-box";
import { ErrorReportButton } from "@/components/error-report-button";
import { PdfViewer } from "@/components/pdf-viewer";
import { usePdfNavigation } from "@/hooks/use-pdf-navigation";
import {
  cancelSummary,
  createSummary,
  getSummaries,
  getSummaryStatus,
  retrySummary,
} from "@/lib/api/summary";
import type { DocumentSummary } from "@/types/api";
import type {
  SummaryArtifact,
  SummaryArtifactType,
  SummaryFailureCategory,
} from "@/types/summary";

interface Props {
  doc: DocumentSummary;
  fileUrl: string;
}

function isActiveStatus(status: string | null | undefined): boolean {
  return status === "queued" || status === "running";
}

// 화면에 보여줄 그룹 순서와 제목 (내부 artifact_type을 사용자 용어로)
const GROUPS: { title: string; types: SummaryArtifactType[] }[] = [
  { title: "전체 개요", types: ["overview"] },
  { title: "섹션별 요약", types: ["section_summary"] },
  { title: "핵심 개념", types: ["key_concept"] },
  { title: "선수지식", types: ["prerequisite"] },
  { title: "주요 수치·대상", types: ["important_number", "target_population"] },
  { title: "학습자 설명", types: ["learner_explanation"] },
  { title: "주의할 내용", types: ["study_caution"] },
];

function artifactBody(a: SummaryArtifact): string {
  const c = a.content;
  return String(
    c.text ?? c.summary ?? c.explanation ?? c.whyNeeded ?? c.context ?? c.value ?? "",
  );
}

function summaryFailureGuide(category: SummaryFailureCategory | null): string {
  if (category === "timeout") {
    return "요약 모델의 응답이 늦어 완료하지 못했어요. 잠시 후 다시 시도해 주세요.";
  }
  if (category === "invalid_response") {
    return "요약 모델의 응답 형식이 올바르지 않아 완료하지 못했어요. 모델 상태를 확인한 뒤 다시 시도해 주세요.";
  }
  if (category === "empty_result") {
    return "요약 모델이 저장할 만한 내용을 만들지 못했어요. 다시 시도해 주세요.";
  }
  if (category === "context_overflow") {
    // 앱이 모델 컨텍스트를 직접 지정하므로 "설정을 늘리라"는 안내는 효과가 없다.
    // 실제로 결과가 달라지는 조치만 안내한다.
    return "문서 한 조각이 AI가 한 번에 볼 수 있는 크기를 넘었어요. 다른 프로그램을 닫아 메모리를 확보한 뒤 다시 시도하거나, 더 작은 모델을 사용해 주세요.";
  }
  return "요약을 만들지 못했어요. 다시 시도해 주세요.";
}

/**
 * 문서 요약 — 왼쪽 원문(PDF), 오른쪽 출처 기반 구조화 요약. 모든 항목에 출처 버튼이
 * 있고 클릭 시 해당 페이지·bbox로 이동한다. 내부 chunk id·모델명·토큰은 노출하지 않는다.
 */
export function SummaryView({ doc, fileUrl }: Props) {
  const queryClient = useQueryClient();
  const nav = usePdfNavigation();

  const statusQuery = useQuery({
    queryKey: ["summary-status", doc.id],
    queryFn: () => getSummaryStatus(doc.id),
    refetchInterval: (q) => (isActiveStatus(q.state.data?.status) ? 1500 : false),
  });

  // 최신 run 상태와 무관하게 조회한다 — 백엔드는 "최신 성공 run"의 artifact를 돌려주므로,
  // 이후 재시도가 실패해도 이전 성공 요약이 사라지지 않는다.
  const listQuery = useQuery({
    queryKey: ["summaries", doc.id],
    queryFn: () => getSummaries(doc.id),
  });

  // 긴 요약은 완료까지 여러 번 폴링한다. terminal 상태로 바뀐 시점에 artifact 쿼리도
  // 갱신해야, 사용자가 화면을 다시 열지 않아도 방금 완성된 요약이 나타난다.
  const activityRef = useRef({ documentId: doc.id, active: false });
  const awaitingResultRef = useRef<string | null>(null);
  const summaryStatus = statusQuery.data?.status;
  const hasStatusData = statusQuery.data !== undefined;
  useEffect(() => {
    const active = isActiveStatus(summaryStatus);
    if (activityRef.current.documentId !== doc.id) {
      activityRef.current = { documentId: doc.id, active };
      return;
    }
    const justFinished = activityRef.current.active && !active;
    const startedHereAndAlreadyFinished = awaitingResultRef.current === doc.id && !active;
    if ((justFinished || startedHereAndAlreadyFinished) && hasStatusData) {
      awaitingResultRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ["summaries", doc.id] });
    }
    activityRef.current.active = active;
  }, [doc.id, hasStatusData, queryClient, statusQuery.dataUpdatedAt, summaryStatus]);

  const refreshStartedSummary = (documentId: string) => {
    // 매우 빠른 실행은 UI가 queued/running을 한 번도 관측하지 않을 수 있다. 다음 상태
    // 응답이 곧바로 terminal이어도 artifact를 다시 조회하도록 시작 사실을 별도로 기억한다.
    awaitingResultRef.current = documentId;
    void queryClient.invalidateQueries({ queryKey: ["summary-status", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["summaries", documentId] });
  };

  const createMutation = useMutation({
    mutationFn: (documentId: string) => createSummary(documentId),
    onSuccess: (_result, documentId) => refreshStartedSummary(documentId),
  });
  const retryMutation = useMutation({
    mutationFn: (documentId: string) => retrySummary(documentId),
    onSuccess: (_result, documentId) => refreshStartedSummary(documentId),
  });
  const cancelMutation = useMutation({
    mutationFn: (documentId: string) => cancelSummary(documentId),
    onSettled: (_result, _error, documentId) =>
      queryClient.invalidateQueries({ queryKey: ["summary-status", documentId] }),
  });

  const navigate = nav.navigate;

  const status = statusQuery.data;
  const pageCount = doc.pageCount ?? 0;
  const artifacts = listQuery.data?.artifacts ?? [];
  const stale = Boolean(status?.stale || listQuery.data?.stale);

  const providerUnavailable = status !== undefined && !status.providerAvailable;
  const createError = createMutation.error as { status?: number; message?: string } | null;
  const chunksNotReady = createError?.status === 409;
  const consentNeeded = createError?.status === 403;

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_420px]">
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

      <aside className="flex h-[640px] flex-col overflow-auto rounded-lg border border-slate-200 bg-white p-4">
        <p className="mb-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">
          이 내용은 학습 보조용이며 실제 환자의 진단·처방·응급 판단에 사용하지 마세요.
        </p>

        {statusQuery.isPending && (
          <p role="status" className="mb-3 text-sm text-slate-500">
            요약 상태를 확인하는 중…
          </p>
        )}
        {statusQuery.isError && (
          <div className="mb-3">
            <ErrorBox
              message="요약 상태를 확인하지 못했습니다."
              onRetry={() => void statusQuery.refetch()}
            />
          </div>
        )}
        {listQuery.isPending && (
          <p role="status" className="mb-3 text-sm text-slate-500">
            저장된 요약을 불러오는 중…
          </p>
        )}
        {listQuery.isError && (
          <div className="mb-3">
            <ErrorBox
              message="저장된 요약을 불러오지 못했습니다."
              onRetry={() => void listQuery.refetch()}
            />
          </div>
        )}

        {providerUnavailable && (
          <div className="mb-3 rounded bg-slate-50 px-3 py-3 text-sm text-slate-700">
            <p>새 요약을 만들려면 앱 설정에서 요약 모델을 연결해 주세요.</p>
            <a href="/settings" className="mt-2 inline-block text-blue-700 hover:underline">
              설정으로 이동
            </a>
          </div>
        )}

        {stale && artifacts.length > 0 && (
          <div className="mb-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800">
            <p>문서가 바뀌어 이 요약은 오래된 내용일 수 있어요.</p>
            {status?.providerAvailable && (
              <button
                type="button"
                onClick={() => retryMutation.mutate(doc.id)}
                disabled={retryMutation.isPending || isActiveStatus(status.status)}
                className="mt-1.5 rounded bg-amber-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50"
              >
                다시 요약하기
              </button>
            )}
          </div>
        )}

        {/* 성공했지만 내용이 빠진 요약 — 표시하지 않으면 사용자는 특정 절이 통째로
            사라진 요약을 완결된 요약으로 신뢰하게 된다. */}
        {status?.partial && artifacts.length > 0 && (
          <div
            role="status"
            className="mb-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800"
          >
            <p>
              문서 일부가 AI가 한 번에 볼 수 있는 크기를 넘어, 그 부분은 요약에 담기지
              못했어요.
            </p>
            {status.providerAvailable && (
              <button
                type="button"
                onClick={() => retryMutation.mutate(doc.id)}
                disabled={retryMutation.isPending || isActiveStatus(status.status)}
                className="mt-1.5 rounded bg-amber-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50"
              >
                다시 요약하기
              </button>
            )}
          </div>
        )}

        {status && isActiveStatus(status.status) && (
          <div className="mb-3 rounded bg-blue-50 px-3 py-2 text-sm text-blue-900">
            <p role="status" aria-live="polite">
              요약을 만드는 중이에요… ({status.progress}%)
            </p>
            <button
              type="button"
              onClick={() => cancelMutation.mutate(doc.id)}
              disabled={cancelMutation.isPending}
              className="mt-2 rounded-md border border-blue-300 px-3 py-1.5 text-xs font-medium hover:bg-blue-100 disabled:opacity-50"
            >
              {cancelMutation.isPending ? "취소하는 중…" : "요약 취소"}
            </button>
            {cancelMutation.isError && (
              <p role="alert" className="mt-1.5 text-xs text-red-700">
                요약을 취소하지 못했습니다. 잠시 후 다시 시도해 주세요.
              </p>
            )}
          </div>
        )}

        {/* 저장 결과 조회가 끝나기 전에는 "아직 없음"이라고 단정하지 않는다. */}
        {listQuery.isSuccess &&
          artifacts.length === 0 &&
          status &&
          !isActiveStatus(status.status) && (
            <div className="rounded bg-slate-50 px-3 py-3 text-sm text-slate-700">
              <p>
                {status.status === "failed"
                  ? summaryFailureGuide(status.failureCategory)
                  : status.status === "cancelled"
                    ? "요약이 취소되었어요."
                    : "아직 요약을 만들지 않았어요."}
              </p>
              {status.providerAvailable && (
                <button
                  type="button"
                  onClick={() => createMutation.mutate(doc.id)}
                  disabled={createMutation.isPending}
                  className="mt-2 rounded-md bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  요약 만들기
                </button>
              )}
              {chunksNotReady && (
                <p role="alert" className="mt-1.5 text-xs text-amber-700">
                  문서 검색 준비가 끝난 뒤에 요약할 수 있어요. 잠시 후 다시 시도해 주세요.
                </p>
              )}
              {consentNeeded && (
                <p role="alert" className="mt-1.5 text-xs text-amber-700">
                  외부 요약 모델을 쓰려면 앱 설정과 이 문서에서 외부 전송을 먼저 허용해 주세요.
                </p>
              )}
              {status.status === "failed" && (
                <ErrorReportButton variant="inline" className="mt-2 block text-xs" />
              )}
            </div>
          )}

        {/* 최신 시도가 실패했지만 이전 성공 요약이 남아 있는 경우 */}
        {status &&
          artifacts.length > 0 &&
          (status.status === "failed" || status.status === "cancelled") && (
            <div className="mb-3 rounded bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <p>
                최근 다시 요약이 {status.status === "failed" ? "실패" : "취소"}되어 이전
                요약을 보여드려요.
              </p>
              {status.status === "failed" && (
                <p className="mt-1">{summaryFailureGuide(status.failureCategory)}</p>
              )}
              <div className="mt-1 flex items-center gap-3">
                {status.providerAvailable && status.canRetry && (
                  <button
                    type="button"
                    onClick={() => retryMutation.mutate(doc.id)}
                    disabled={retryMutation.isPending}
                    className="rounded-md border border-slate-300 px-3 py-1.5 text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:opacity-50"
                  >
                    다시 시도
                  </button>
                )}
                {status.status === "failed" && <ErrorReportButton variant="inline" />}
              </div>
            </div>
          )}

        {artifacts.length > 0 && (
          <div className="flex flex-col gap-4">
            {GROUPS.map((group) => {
              const items = artifacts.filter((a) => group.types.includes(a.artifactType));
              if (items.length === 0) return null;
              return (
                <section key={group.title}>
                  <h3 className="mb-1.5 text-sm font-semibold text-slate-800">{group.title}</h3>
                  <ul className="flex flex-col gap-2">
                    {items.map((a, i) => (
                      <li
                        key={`${a.artifactType}-${a.position}-${i}`}
                        className="rounded border border-slate-200 px-3 py-2"
                      >
                        {a.title && (
                          <p className="mb-0.5 text-sm font-medium text-slate-800">{a.title}</p>
                        )}
                        <p className="max-w-[68ch] whitespace-pre-wrap text-[1.0625rem] leading-[1.7] text-slate-900">
                          {artifactBody(a)}
                        </p>
                        <SourceList
                          groups={[
                            {
                              number: null,
                              refs: a.sourceRefs.map((r) => ({
                                pageNumber: r.pageNumber,
                                sectionTitle: null,
                                sourceMethod: r.sourceMethod,
                                bbox: r.bbox,
                              })),
                            },
                          ]}
                          onNavigate={navigate}
                        />
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>
        )}
      </aside>
    </div>
  );
}
