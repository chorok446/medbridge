"use client";

import { ErrorReportButton } from "@/components/error-report-button";

export function ErrorBox({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm">
      <p className="text-red-800">{message}</p>
      <div className="mt-2 flex items-center gap-3">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded border border-red-300 px-3 py-1 text-red-800 hover:bg-red-100"
          >
            다시 시도
          </button>
        )}
        {/* 오류가 뜬 자리에서 바로 진단 파일을 저장할 수 있어야 한다 — 설정까지
            찾아 들어가야 하면 대개 그냥 포기하고 원인이 영영 안 남는다. */}
        <ErrorReportButton variant="inline" />
      </div>
    </div>
  );
}
