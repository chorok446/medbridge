"use client";

import { useQuery } from "@tanstack/react-query";
import { documentFileUrl } from "@/lib/api/documents";
import { ErrorBox } from "@/components/error-box";

/**
 * 분석 완료 전에도 원문을 보여주는 미리보기.
 * ponytail: 웹뷰 내장 PDF 뷰어(iframe) 사용 — 페이지 좌표 이동·하이라이트가 필요한
 * Sprint 2+에서 PDF.js 커스텀 뷰어로 교체한다.
 */
export function PdfPreview({ documentId }: { documentId: string }) {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["file-url", documentId],
    queryFn: () => documentFileUrl(documentId),
    staleTime: Infinity,
  });

  if (isLoading) {
    return (
      <div className="flex h-[600px] items-center justify-center rounded-lg border border-slate-200 bg-white text-sm text-slate-500">
        문서를 불러오는 중…
      </div>
    );
  }
  if (isError || !data) {
    return <ErrorBox message="문서를 불러오지 못했습니다." onRetry={() => refetch()} />;
  }
  return (
    <iframe
      src={data}
      title="PDF 미리보기"
      className="h-[600px] w-full rounded-lg border border-slate-200 bg-white"
    />
  );
}
