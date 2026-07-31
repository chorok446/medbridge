"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { use } from "react";
import { ErrorBox } from "@/components/error-box";
import { PdfPreview } from "@/components/pdf-preview";
import { StatusBadge } from "@/components/status-badge";
import { ApiError } from "@/lib/api/client";
import { deleteDocument, getDocument, listJobs, retryDocument } from "@/lib/api/documents";
import { STAGE_LABELS, formatBytes, formatDate, isActive } from "@/lib/format";

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "대기",
  running: "실행 중",
  succeeded: "성공",
  failed: "실패",
};

export default function DocumentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const queryClient = useQueryClient();

  const docQuery = useQuery({
    queryKey: ["document", id],
    queryFn: () => getDocument(id),
    refetchInterval: (q) =>
      q.state.data && isActive(q.state.data.processingStatus) ? 2000 : false,
  });

  const jobsQuery = useQuery({
    queryKey: ["document-jobs", id],
    queryFn: () => listJobs(id),
    refetchInterval: () =>
      docQuery.data && isActive(docQuery.data.processingStatus) ? 2000 : false,
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

  if (docQuery.error instanceof ApiError && docQuery.error.status === 401) {
    router.push("/login");
  }

  if (docQuery.isLoading) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-12 text-center text-sm text-slate-500">
        문서 불러오는 중…
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
              className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50"
            >
              재시도
            </button>
          )}
          <button
            type="button"
            disabled={deleteMutation.isPending}
            onClick={() => {
              if (window.confirm("문서를 삭제할까요? 원본 파일과 처리 기록이 함께 삭제됩니다.")) {
                deleteMutation.mutate();
              }
            }}
            className="rounded border border-red-200 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            삭제
          </button>
        </div>
      </header>

      {deleteMutation.isError && (
        <div className="mb-4">
          <ErrorBox
            message={
              deleteMutation.error instanceof Error
                ? deleteMutation.error.message
                : "삭제에 실패했습니다."
            }
            onRetry={() => deleteMutation.mutate()}
          />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
        <PdfPreview documentId={id} />

        <aside className="flex flex-col gap-4">
          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-3 text-sm font-semibold">처리 상태</h2>
            <StatusBadge status={doc.processingStatus} progress={doc.processingProgress} />
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-slate-500">현재 단계</dt>
              <dd>{STAGE_LABELS[doc.processingStage] ?? doc.processingStage}</dd>
              <dt className="text-slate-500">진행률</dt>
              <dd>{doc.processingProgress}%</dd>
              <dt className="text-slate-500">크기</dt>
              <dd>{formatBytes(doc.fileSize)}</dd>
              <dt className="text-slate-500">페이지</dt>
              <dd>{doc.pageCount ?? "—"}</dd>
              <dt className="text-slate-500">업로드</dt>
              <dd>{formatDate(doc.createdAt)}</dd>
            </dl>
            {doc.processingStatus === "failed" && (
              <div className="mt-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700">
                <p className="font-medium">{doc.failureMessage ?? "처리에 실패했습니다."}</p>
                <p className="mt-1">
                  복구 방법: 위의 재시도 버튼을 누르거나, 파일 자체 문제(암호화·손상)라면 수정된
                  파일을 새로 업로드하세요.
                </p>
              </div>
            )}
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-3 text-sm font-semibold">작업 이력</h2>
            {jobsQuery.isLoading && <p className="text-sm text-slate-500">불러오는 중…</p>}
            {jobsQuery.isError && <p className="text-sm text-red-600">이력을 불러오지 못했습니다.</p>}
            {jobsQuery.data && jobsQuery.data.length === 0 && (
              <p className="text-sm text-slate-500">아직 작업 이력이 없습니다.</p>
            )}
            {jobsQuery.data && jobsQuery.data.length > 0 && (
              <ul className="flex flex-col gap-2 text-sm">
                {jobsQuery.data.map((job) => (
                  <li key={job.id} className="rounded border border-slate-100 px-3 py-2">
                    <div className="flex items-center justify-between">
                      <span className="font-medium">파일 검증</span>
                      <span
                        className={
                          job.status === "failed"
                            ? "text-red-600"
                            : job.status === "succeeded"
                              ? "text-green-700"
                              : "text-blue-700"
                        }
                      >
                        {JOB_STATUS_LABELS[job.status] ?? job.status}
                      </span>
                    </div>
                    <p className="text-xs text-slate-500">
                      시도 {job.attemptCount}/{job.maxAttempts} · {formatDate(job.createdAt)}
                    </p>
                    {job.failureMessage && (
                      <p className="mt-1 text-xs text-red-600">{job.failureMessage}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-2 text-sm font-semibold">학습 기능</h2>
            <p className="mb-2 text-xs text-slate-500">
              요약·질의응답·플래시카드는 다음 단계에서 제공됩니다.
            </p>
            <div className="flex flex-wrap gap-2">
              {["요약", "질의응답", "플래시카드"].map((label) => (
                <button
                  key={label}
                  type="button"
                  disabled
                  title="향후 제공 예정"
                  className="cursor-not-allowed rounded border border-slate-200 px-3 py-1.5 text-sm text-slate-400"
                >
                  {label}
                </button>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}
