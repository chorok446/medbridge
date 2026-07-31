import { STATUS_LABELS, isActive } from "@/lib/format";
import type { ProcessingStatus } from "@/types/api";

const STYLES: Record<string, string> = {
  ready: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  deleted: "bg-slate-100 text-slate-500",
  deleting: "bg-slate-100 text-slate-500",
};

export function StatusBadge({
  status,
  progress,
}: {
  status: ProcessingStatus;
  progress?: number;
}) {
  const style = STYLES[status] ?? "bg-blue-100 text-blue-800";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${style}`}
    >
      {isActive(status) && (
        <span
          aria-hidden
          className="inline-block h-2 w-2 animate-pulse rounded-full bg-current"
        />
      )}
      {STATUS_LABELS[status]}
      {isActive(status) && progress !== undefined && progress > 0 && ` ${progress}%`}
    </span>
  );
}
