"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { DocumentTable } from "@/components/document-table";
import { ErrorBox } from "@/components/error-box";
import { UploadDropzone } from "@/components/upload-dropzone";
import { ApiError } from "@/lib/api/client";
import { deleteDocument, listDocuments, retryDocument } from "@/lib/api/documents";
import { isActive } from "@/lib/format";

export default function DocumentsPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
    // 처리 중 문서가 있으면 2초 간격 폴링
    refetchInterval: (q) =>
      q.state.data?.items.some((d) => isActive(d.processingStatus)) ? 2000 : false,
  });

  if (error instanceof ApiError && error.status === 401) {
    router.push("/login");
  }

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["documents"] });

  const retryMutation = useMutation({
    mutationFn: retryDocument,
    onMutate: (id: string) => {
      setBusyId(id);
      setActionError(null);
    },
    onError: (err) =>
      setActionError(err instanceof Error ? err.message : "재시도에 실패했습니다."),
    onSettled: () => {
      setBusyId(null);
      invalidate();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteDocument,
    onMutate: (id: string) => {
      setBusyId(id);
      setActionError(null);
    },
    onError: (err) =>
      setActionError(err instanceof Error ? err.message : "삭제에 실패했습니다."),
    onSettled: () => {
      setBusyId(null);
      invalidate();
    },
  });

  function handleDelete(id: string) {
    if (window.confirm("문서를 삭제할까요? 원본 파일과 처리 기록이 함께 삭제됩니다.")) {
      deleteMutation.mutate(id);
    }
  }

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-8">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">내 문서</h1>
        <p className="text-sm text-slate-500">
          PDF를 업로드하면 파일 검증 후 학습 준비가 완료됩니다.
        </p>
      </header>

      <UploadDropzone onUploaded={() => invalidate()} />

      <div className="mt-6">
        {isLoading && (
          <div className="rounded-lg border border-slate-200 bg-white px-6 py-12 text-center text-sm text-slate-500">
            문서 목록 불러오는 중…
          </div>
        )}
        {isError && (
          <ErrorBox message="문서 목록을 불러오지 못했습니다." onRetry={() => refetch()} />
        )}
        {actionError && <ErrorBox message={actionError} />}
        {data && (
          <DocumentTable
            items={data.items}
            busyId={busyId}
            onRetry={(id) => retryMutation.mutate(id)}
            onDelete={handleDelete}
          />
        )}
      </div>
    </div>
  );
}
