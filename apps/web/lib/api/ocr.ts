import { api } from "@/lib/api/client";

export interface OcrStatus {
  available: boolean;
  running: boolean;
  totalTargets: number;
  done: number;
  failed: number;
  lowConfidencePages: number[];
  remainingOcrPages: number[];
}

export function getOcrStatus(id: string): Promise<OcrStatus> {
  return api<OcrStatus>(`/api/documents/${id}/ocr-status`);
}

export function startOcr(id: string): Promise<{ targetPages: number; started: boolean }> {
  return api(`/api/documents/${id}/ocr`, { method: "POST", body: JSON.stringify({}) });
}

export function retryOcr(id: string): Promise<{ targetPages: number; started: boolean }> {
  return api(`/api/documents/${id}/ocr/retry`, { method: "POST", body: JSON.stringify({}) });
}

export function cancelOcr(id: string): Promise<unknown> {
  return api(`/api/documents/${id}/ocr/cancel`, { method: "POST" });
}
