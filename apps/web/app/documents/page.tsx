"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { DocumentTable } from "@/components/document-table";
import { ErrorBox } from "@/components/error-box";
import { UploadDropzone } from "@/components/upload-dropzone";
import {
  deleteDocument,
  listDocuments,
  renameDocument,
  reportError,
  retryDocument,
} from "@/lib/api/documents";
import { isActive } from "@/lib/format";

export default function DocumentsPage() {
  const queryClient = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
    // 처리 중 문서가 있으면 2초 간격 폴링
    refetchInterval: (q) =>
      q.state.data?.items.some((d) => isActive(d.processingStatus)) ? 2000 : false,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["documents"] });

  const retryMutation = useMutation({
    mutationFn: retryDocument,
    onMutate: (id: string) => {
      setBusyId(id);
      setActionError(null);
    },
    onError: (err) =>
      setActionError(err instanceof Error ? err.message : "다시 시도하지 못했습니다."),
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
      setActionError(err instanceof Error ? err.message : "삭제하지 못했습니다."),
    onSettled: () => {
      setBusyId(null);
      invalidate();
    },
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameDocument(id, title),
    onError: (err) =>
      setActionError(err instanceof Error ? err.message : "이름을 바꾸지 못했습니다."),
    onSettled: () => invalidate(),
  });

  const reportMutation = useMutation({
    mutationFn: (id: string) => reportError(id, "문서 처리 실패 신고"),
    onSuccess: () =>
      setNotice(
        "오류 내용을 기록했어요. [설정]에서 [오류 정보 저장]을 누르면 개발자에게 전달할 파일이 만들어집니다.",
      ),
    onError: () => setActionError("오류 신고를 저장하지 못했습니다. 잠시 후 다시 시도해 주세요."),
  });

  function handleDelete(id: string, title: string) {
    // 확인 모달: 기본(Esc/취소)은 취소 행동. 삭제 대상 이름을 명시한다.
    if (window.confirm(`'${title}'을(를) 삭제할까요?\n파일과 학습 기록이 함께 삭제되며 되돌릴 수 없습니다.`)) {
      deleteMutation.mutate(id);
    }
  }

  function handleRename(id: string, currentTitle: string) {
    const title = window.prompt("새 이름을 입력해 주세요.", currentTitle);
    if (title && title.trim() && title.trim() !== currentTitle) {
      renameMutation.mutate({ id, title: title.trim() });
    }
  }

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-8">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">내 학습자료</h1>
        <p className="text-sm text-slate-500">
          PDF를 올리면 자동으로 확인한 뒤 학습 준비를 마칩니다.
        </p>
      </header>

      <UploadDropzone onUploaded={() => invalidate()} />

      <div className="mt-6">
        {isLoading && (
          <div className="rounded-lg border border-slate-200 bg-white px-6 py-12 text-center text-sm text-slate-500">
            학습자료를 불러오는 중…
          </div>
        )}
        {isError && (
          <ErrorBox message="학습자료 목록을 불러오지 못했습니다." onRetry={() => refetch()} />
        )}
        {actionError && <ErrorBox message={actionError} />}
        {notice && (
          <p role="status" className="mb-3 rounded bg-green-50 px-3 py-2 text-sm text-green-800">
            {notice}
          </p>
        )}
        {data && (
          <DocumentTable
            items={data.items}
            busyId={busyId}
            onRetry={(id) => retryMutation.mutate(id)}
            onRename={handleRename}
            onDelete={(id, title) => handleDelete(id, title)}
            onReport={(id) => reportMutation.mutate(id)}
          />
        )}
      </div>
    </div>
  );
}
