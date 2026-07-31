"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { ErrorBox } from "@/components/error-box";
import { ExtractionReview } from "@/components/extraction-review";
import { PdfPreview } from "@/components/pdf-preview";
import { StatusBadge } from "@/components/status-badge";
import { SummaryView } from "@/components/summary-view";
import {
  deleteDocument,
  documentFileUrl,
  getDocument,
  listJobs,
  reportError,
  retryDocument,
} from "@/lib/api/documents";
import { failureGuide, formatBytes, formatDate, hasExtraction, isActive } from "@/lib/format";

function DocumentDetail() {
  const searchParams = useSearchParams();
  const id = searchParams.get("id") ?? "";
  const router = useRouter();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  const [mainTab, setMainTab] = useState<"preview" | "extraction" | "summary">("preview");

  const fileUrlQuery = useQuery({
    queryKey: ["file-url", id],
    queryFn: () => documentFileUrl(id),
    staleTime: Infinity,
  });

  const docQuery = useQuery({
    queryKey: ["document", id],
    queryFn: () => getDocument(id),
    refetchInterval: (q) =>
      q.state.data && isActive(q.state.data.processingStatus) ? 2000 : false,
  });

  const jobsQuery = useQuery({
    queryKey: ["document-jobs", id],
    queryFn: () => listJobs(id),
    enabled: docQuery.isSuccess,
  });

  const retryMutation = useMutation({
    mutationFn: () => retryDocument(id),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["document", id] });
      queryClient.invalidateQueries({ queryKey: ["document-jobs", id] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteDocument(id),
    onSuccess: () => router.push("/documents"),
  });

  const reportMutation = useMutation({
    mutationFn: () => reportError(id, "문서 상세 화면에서 오류 신고"),
    onSuccess: () => setNotice("오류가 접수되었습니다. 확인 후 개선하겠습니다."),
  });

  if (docQuery.isLoading) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-12 text-center text-sm text-slate-500">
        문서를 불러오는 중…
      </div>
    );
  }
  if (docQuery.isError || !docQuery.data) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-8">
        <ErrorBox message="문서를 불러오지 못했습니다." onRetry={() => docQuery.refetch()} />
      </div>
    );
  }

  const doc = docQuery.data;
  const processing = isActive(doc.processingStatus);

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-2xl font-bold">{doc.title}</h1>
          <p className="text-sm text-slate-500">{doc.originalFilename}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          {doc.processingStatus === "failed" && (
            <button
              type="button"
              disabled={retryMutation.isPending}
              onClick={() => retryMutation.mutate()}
              className="rounded border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            >
              다시 시도
            </button>
          )}
          <button
            type="button"
            disabled={deleteMutation.isPending}
            onClick={() => {
              if (
                window.confirm("이 학습자료를 삭제할까요? 파일과 학습 기록이 함께 삭제됩니다.")
              ) {
                deleteMutation.mutate();
              }
            }}
            className="rounded border border-red-200 px-4 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            삭제
          </button>
        </div>
      </header>

      {notice && (
        <p role="status" className="mb-4 rounded bg-green-50 px-3 py-2 text-sm text-green-800">
          {notice}
        </p>
      )}
      {deleteMutation.isError && (
        <div className="mb-4">
          <ErrorBox
            message="삭제하지 못했습니다. 잠시 후 다시 시도해 주세요."
            onRetry={() => deleteMutation.mutate()}
          />
        </div>
      )}

      {doc.processingStatus === "failed" && (
        <div role="alert" className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4">
          <p className="font-semibold text-red-800">파일을 처리하지 못했습니다.</p>
          <p className="mt-1 text-sm text-red-700">{failureGuide(doc.failureCode)}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Link
              href="/documents"
              className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-800 hover:bg-red-100"
            >
              다른 파일 선택
            </Link>
            <button
              type="button"
              disabled={retryMutation.isPending}
              onClick={() => retryMutation.mutate()}
              className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-800 hover:bg-red-100 disabled:opacity-50"
            >
              다시 시도
            </button>
            <button
              type="button"
              onClick={() => reportMutation.mutate()}
              className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-800 hover:bg-red-100"
            >
              오류 신고
            </button>
          </div>
        </div>
      )}

      {(hasExtraction(doc.processingStatus) ||
        doc.processingStatus === "extracting" ||
        doc.processingStatus === "extraction_failed") && (
        <div role="tablist" className="mb-4 flex gap-1 border-b border-slate-200 text-sm">
          {(
            [
              { key: "preview", label: "문서 보기" },
              { key: "extraction", label: "텍스트 확인" },
              { key: "summary", label: "요약" },
            ] as const
          ).map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={mainTab === t.key}
              onClick={() => setMainTab(t.key)}
              className={`rounded-t px-4 py-2 ${
                mainTab === t.key
                  ? "border border-b-0 border-slate-200 bg-white font-semibold text-blue-700"
                  : "text-slate-500 hover:text-slate-800"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}

      {mainTab === "extraction" && fileUrlQuery.data ? (
        <ExtractionReview doc={doc} fileUrl={fileUrlQuery.data} />
      ) : mainTab === "summary" && fileUrlQuery.data ? (
        <SummaryView doc={doc} fileUrl={fileUrlQuery.data} />
      ) : (
      <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
        <PdfPreview documentId={id} />

        <aside className="flex flex-col gap-4">
          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-3 text-sm font-semibold">처리 상태</h2>
            <StatusBadge status={doc.processingStatus} progress={doc.processingProgress} />
            {processing && (
              <p aria-live="polite" className="mt-2 text-sm text-slate-600">
                파일을 확인하고 있어요. 조금만 기다려 주세요. ({doc.processingProgress}%)
              </p>
            )}
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-slate-500">크기</dt>
              <dd>{formatBytes(doc.fileSize)}</dd>
              <dt className="text-slate-500">페이지</dt>
              <dd>{doc.pageCount ?? "—"}</dd>
              <dt className="text-slate-500">추가한 날짜</dt>
              <dd>{formatDate(doc.createdAt)}</dd>
            </dl>
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-2 text-sm font-semibold">학습 기능</h2>
            <p className="mb-2 text-xs text-slate-500">
              위쪽 [요약] 탭에서 문서 요약을 볼 수 있어요. 질문·복습 카드는 다음 업데이트에서
              열려요.
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setMainTab("summary")}
                className="rounded border border-blue-200 px-3 py-1.5 text-sm text-blue-700 hover:bg-blue-50"
              >
                요약 보기
              </button>
              {["질문하기", "복습 카드"].map((label) => (
                <button
                  key={label}
                  type="button"
                  disabled
                  title="다음 업데이트에서 제공됩니다"
                  className="cursor-not-allowed rounded border border-slate-200 px-3 py-1.5 text-sm text-slate-400"
                >
                  {label}
                </button>
              ))}
            </div>
          </section>

          {/* 기술 정보는 기본 화면에서 숨긴다 (관리자·개발 확인용) */}
          <details className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
            <summary className="cursor-pointer font-semibold text-slate-500">
              자세한 처리 기록
            </summary>
            {jobsQuery.data && jobsQuery.data.length > 0 ? (
              <ul className="mt-2 flex flex-col gap-1 text-xs text-slate-500">
                {jobsQuery.data.map((job) => (
                  <li key={job.id}>
                    파일 확인 — {job.status === "succeeded" ? "성공" : job.status === "failed" ? "실패" : "진행 중"} ·{" "}
                    {formatDate(job.createdAt)}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-xs text-slate-500">기록이 없습니다.</p>
            )}
          </details>
        </aside>
      </div>
      )}
    </div>
  );
}


export default function DocumentDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-5xl px-4 py-12 text-center text-sm text-slate-500">
          문서를 불러오는 중…
        </div>
      }
    >
      <DocumentDetail />
    </Suspense>
  );
}
