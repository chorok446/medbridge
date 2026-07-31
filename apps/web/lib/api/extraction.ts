import { api } from "@/lib/api/client";
import type { DocumentSummary } from "@/types/api";
import type {
  ExtractionBlock,
  ExtractionStatus,
  ExtractionTable,
  PageDetail,
  PageSummary,
} from "@/types/extraction";

export function getExtractionStatus(id: string): Promise<ExtractionStatus> {
  return api<ExtractionStatus>(`/api/documents/${id}/extraction-status`);
}

export function listPages(id: string): Promise<PageSummary[]> {
  return api<PageSummary[]>(`/api/documents/${id}/pages`);
}

export function getPage(id: string, pageNumber: number): Promise<PageDetail> {
  return api<PageDetail>(`/api/documents/${id}/pages/${pageNumber}`);
}

export function getPageBlocks(
  id: string,
  pageNumber: number,
  includeBands: boolean,
): Promise<ExtractionBlock[]> {
  return api<ExtractionBlock[]>(
    `/api/documents/${id}/pages/${pageNumber}/blocks?include_bands=${includeBands}`,
  );
}

export function listTables(id: string): Promise<ExtractionTable[]> {
  return api<ExtractionTable[]>(`/api/documents/${id}/tables`);
}

export function retryExtraction(id: string): Promise<DocumentSummary> {
  return api<DocumentSummary>(`/api/documents/${id}/extract/retry`, { method: "POST" });
}

export function cancelExtraction(id: string): Promise<DocumentSummary> {
  return api<DocumentSummary>(`/api/documents/${id}/extract/cancel`, { method: "POST" });
}
