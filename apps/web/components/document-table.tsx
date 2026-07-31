"use client";

import Link from "next/link";
import { StatusBadge } from "@/components/status-badge";
import { STAGE_LABELS, formatBytes, formatDate } from "@/lib/format";
import type { DocumentSummary } from "@/types/api";

interface Props {
  items: DocumentSummary[];
  onRetry: (id: string) => void;
  onDelete: (id: string) => void;
  busyId?: string | null;
}

export function DocumentTable({ items, onRetry, onDelete, busyId }: Props) {
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
        <p className="font-medium">아직 업로드한 문서가 없습니다</p>
        <p className="mt-1 text-sm text-slate-500">
          위에서 첫 PDF를 업로드하면 자동으로 파일 검증이 시작됩니다.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-left text-xs text-slate-500">
            <th className="px-4 py-2 font-medium">제목</th>
            <th className="px-4 py-2 font-medium">크기</th>
            <th className="px-4 py-2 font-medium">생성일</th>
            <th className="px-4 py-2 font-medium">상태</th>
            <th className="px-4 py-2 font-medium">작업</th>
          </tr>
        </thead>
        <tbody>
          {items.map((doc) => (
            <tr key={doc.id} className="border-b border-slate-100 last:border-0">
              <td className="max-w-[280px] px-4 py-2">
                <Link
                  href={`/documents/${doc.id}`}
                  className="block truncate font-medium text-blue-700 hover:underline"
                >
                  {doc.title}
                </Link>
                <span className="block truncate text-xs text-slate-500">
                  {doc.originalFilename}
                </span>
              </td>
              <td className="whitespace-nowrap px-4 py-2">{formatBytes(doc.fileSize)}</td>
              <td className="whitespace-nowrap px-4 py-2">{formatDate(doc.createdAt)}</td>
              <td className="px-4 py-2">
                <StatusBadge status={doc.processingStatus} progress={doc.processingProgress} />
                {doc.processingStatus !== "ready" && (
                  <span className="block text-xs text-slate-500">
                    {STAGE_LABELS[doc.processingStage] ?? doc.processingStage}
                  </span>
                )}
                {doc.processingStatus === "failed" && doc.failureMessage && (
                  <span className="block max-w-[220px] text-xs text-red-600">
                    {doc.failureMessage}
                  </span>
                )}
              </td>
              <td className="whitespace-nowrap px-4 py-2">
                <div className="flex gap-2">
                  {doc.processingStatus === "failed" && (
                    <button
                      type="button"
                      disabled={busyId === doc.id}
                      onClick={() => onRetry(doc.id)}
                      className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                    >
                      재시도
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={busyId === doc.id}
                    onClick={() => onDelete(doc.id)}
                    className="rounded border border-red-200 px-2 py-1 text-xs text-red-700 hover:bg-red-50 disabled:opacity-50"
                  >
                    삭제
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
