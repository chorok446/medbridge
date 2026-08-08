"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { SourceList } from "@/components/citations";
import { ErrorReportButton } from "@/components/error-report-button";
import { PdfViewer } from "@/components/pdf-viewer";
import { usePdfNavigation } from "@/hooks/use-pdf-navigation";
import { createSummary, getSummaries, getSummaryStatus, retrySummary } from "@/lib/api/summary";
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
    enabled: statusQuery.data?.providerAvailable ?? false,
  });

  const createMutation = useMutation({
    mutationFn: () => createSummary(doc.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["summary-status", doc.id] }),
  });
  const retryMutation = useMutation({
    mutationFn: () => retrySummary(doc.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["summary-status", doc.id] }),
  });

  const navigate = nav.navigate;

  const status = statusQuery.data;
  const pageCount = doc.pageCount ?? 0;
  const artifacts = listQuery.data?.artifacts ?? [];
  const stale = status?.stale ?? false;

  const providerUnavailable = status && !status.providerAvailable;
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

        {providerUnavailable && (
          <div className="rounded bg-slate-50 px-3 py-3 text-sm text-slate-700">
            <p>요약 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.</p>
            <a href="/settings" className="mt-2 inline-block text-blue-700 hover:underline">
              설정으로 이동
            </a>
          </div>
        )}

        {status?.providerAvailable && (
          <>
            {stale && artifacts.length > 0 && (
              <div className="mb-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800">
                <p>문서가 바뀌어 이 요약은 오래된 내용일 수 있어요.</p>
                <button
                  type="button"
                  onClick={() => retryMutation.mutate()}
                  disabled={retryMutation.isPending || isActiveStatus(status.status)}
                  className="mt-1.5 rounded bg-amber-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50"
                >
                  다시 요약하기
                </button>
              </div>
            )}

            {/* 성공했지만 내용이 빠진 요약 — 표시하지 않으면 사용자는 특정 절이 통째로
                사라진 요약을 완결된 요약으로 신뢰하게 된다. */}
            {status.partial && artifacts.length > 0 && (
              <div
                role="status"
                className="mb-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800"
              >
                <p>
                  문서 일부가 AI가 한 번에 볼 수 있는 크기를 넘어, 그 부분은 요약에
                  담기지 못했어요.
                </p>
                <button
                  type="button"
                  onClick={() => retryMutation.mutate()}
                  disabled={retryMutation.isPending || isActiveStatus(status.status)}
                  className="mt-1.5 rounded bg-amber-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50"
                >
                  다시 요약하기
                </button>
              </div>
            )}

            {isActiveStatus(status.status) && (
              <p role="status" aria-live="polite" className="text-sm text-slate-500">
                요약을 만드는 중이에요… ({status.progress}%)
              </p>
            )}

            {/* 표시할 요약이 없고 진행 중도 아닐 때만 생성/실패 안내를 보여준다.
                (이전 성공 요약이 있으면 최신 시도가 실패해도 그 요약을 유지한다) */}
            {artifacts.length === 0 && !isActiveStatus(status.status) && (
              <div className="rounded bg-slate-50 px-3 py-3 text-sm text-slate-700">
                <p>
                  {status.status === "failed"
                    ? summaryFailureGuide(status.failureCategory)
                    : status.status === "cancelled"
                      ? "요약이 취소되었어요."
                      : "아직 요약을 만들지 않았어요."}
                </p>
                <button
                  type="button"
                  onClick={() => createMutation.mutate()}
                  disabled={createMutation.isPending}
                  className="mt-2 rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  요약 만들기
                </button>
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
            {artifacts.length > 0 && (status.status === "failed" || status.status === "cancelled") && (
              <div className="mb-3 rounded bg-slate-50 px-3 py-2 text-xs text-slate-600">
                <p>
                  최근 다시 요약이 {status.status === "failed" ? "실패" : "취소"}되어 이전
                  요약을 보여드려요.
                </p>
                {status.status === "failed" && (
                  <p className="mt-1">
                    {summaryFailureGuide(status.failureCategory)}
                  </p>
                )}
                <div className="mt-1 flex items-center gap-3">
                  {status.canRetry && (
                    <button
                      type="button"
                      onClick={() => retryMutation.mutate()}
                      disabled={retryMutation.isPending}
                      className="text-blue-700 hover:underline disabled:opacity-50"
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
                      <h3 className="mb-1.5 text-sm font-semibold text-slate-800">
                        {group.title}
                      </h3>
                      <ul className="flex flex-col gap-2">
                        {items.map((a, i) => (
                          <li
                            key={`${a.artifactType}-${a.position}-${i}`}
                            className="rounded border border-slate-200 px-3 py-2"
                          >
                            {a.title && (
                              <p className="mb-0.5 text-sm font-medium text-slate-800">
                                {a.title}
                              </p>
                            )}
                            <p className="max-w-[68ch] whitespace-pre-wrap text-[17px] leading-[1.7] text-slate-900">
                              {artifactBody(a)}
                            </p>
                            {/* 질문 탭과 같은 출처 표기를 쓴다 — 근거를 읽는 법이 화면마다
                                다르면 안 된다. 다만 요약 본문에는 인용 마커가 없으므로
                                번호는 붙이지 않는다(number: null): 본문 어디에도 대응하지
                                않는 순번은 사용자를 찾아 헤매게 만든다. */}
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
          </>
        )}
      </aside>
    </div>
  );
}

