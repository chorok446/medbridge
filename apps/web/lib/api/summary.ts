import { api } from "@/lib/api/client";
import type {
  LearnerLevel,
  SummaryList,
  SummaryStartResult,
  SummaryStatus,
} from "@/types/summary";

interface CreateOptions {
  learnerLevel?: LearnerLevel;
  language?: string;
  includeSections?: boolean;
  includePrerequisites?: boolean;
}

export function createSummary(id: string, opts: CreateOptions = {}): Promise<SummaryStartResult> {
  return api<SummaryStartResult>(`/api/documents/${id}/summaries`, {
    method: "POST",
    body: JSON.stringify({ learnerLevel: "nursing_student", language: "ko", ...opts }),
  });
}

export function retrySummary(id: string, opts: CreateOptions = {}): Promise<SummaryStartResult> {
  return api<SummaryStartResult>(`/api/documents/${id}/summaries/retry`, {
    method: "POST",
    body: JSON.stringify({ learnerLevel: "nursing_student", language: "ko", ...opts }),
  });
}

export function getSummaryStatus(id: string): Promise<SummaryStatus> {
  return api<SummaryStatus>(`/api/documents/${id}/summaries/status`);
}

export function getSummaries(id: string): Promise<SummaryList> {
  return api<SummaryList>(`/api/documents/${id}/summaries`);
}

export function cancelSummary(id: string): Promise<SummaryStartResult> {
  return api<SummaryStartResult>(`/api/documents/${id}/summaries/cancel`, { method: "POST" });
}

export function deleteSummaries(id: string): Promise<SummaryStartResult> {
  return api<SummaryStartResult>(`/api/documents/${id}/summaries`, { method: "DELETE" });
}
