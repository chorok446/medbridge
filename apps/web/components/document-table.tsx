"use client";

import Link from "next/link";
import { StatusBadge } from "@/components/status-badge";
import { failureGuide, formatBytes, formatDate } from "@/lib/format";
import type { DocumentSummary } from "@/types/api";

interface Props {
  items: DocumentSummary[];
  onRetry: (id: string) => void;
  onRename: (id: string, currentTitle: string) => void;
  onDelete: (id: string, title: string) => void;
  onReport: (id: string) => void;
  busyId?: string | null;
}

const actionButton =
  "rounded border px-3 py-1.5 text-xs hover:bg-slate-50 disabled:opacity-50 border-slate-300";

export function DocumentTable({ items, onRetry, onRename, onDelete, onReport, busyId }: Props) {
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
        <p className="text-lg font-medium">아직 학습자료가 없어요</p>
        <p className="mt-1 text-sm text-slate-500">첫 PDF를 올리면 여기에 표시됩니다.</p>
        <a
          href="#upload-section"
          className="mt-4 inline-block rounded-md bg-blue-600 px-5 py-2.5 font-medium text-white hover:bg-blue-700"
        >
          PDF 추가하기
        </a>
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {items.map((doc) => (
        <li key={doc.id} className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <Link
                href={`/documents/view?id=${doc.id}`}
                className="block truncate text-base font-semibold text-blue-700 hover:underline"
              >
                {doc.title}
              </Link>
              <p className="truncate text-xs text-slate-500">
                {doc.originalFilename} · {formatBytes(doc.fileSize)} ·{" "}
                {formatDate(doc.createdAt)} 추가
              </p>
            </div>
            <StatusBadge status={doc.processingStatus} progress={doc.processingProgress} />
          </div>

          {doc.processingStatus === "failed" && (
            <div className="mt-2 rounded bg-red-50 px-3 py-2 text-sm text-red-800">
              <p className="font-medium">파일을 처리하지 못했습니다.</p>
              <p className="mt-0.5 text-xs">{failureGuide(doc.failureCode)}</p>
            </div>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            <Link href={`/documents/view?id=${doc.id}`} className={actionButton}>
              열기
            </Link>
            {doc.processingStatus === "failed" && (
              <>
                <button
                  type="button"
                  disabled={busyId === doc.id}
                  onClick={() => onRetry(doc.id)}
                  className={actionButton}
                >
                  다시 시도
                </button>
                <button
                  type="button"
                  onClick={() => onReport(doc.id)}
                  className={actionButton}
                >
                  오류 신고
                </button>
              </>
            )}
            <button
              type="button"
              disabled={busyId === doc.id}
              onClick={() => onRename(doc.id, doc.title)}
              className={actionButton}
            >
              이름 변경
            </button>
            <button
              type="button"
              disabled={busyId === doc.id}
              onClick={() => onDelete(doc.id, doc.title)}
              className="rounded border border-red-200 px-3 py-1.5 text-xs text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              삭제
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}
