import { api } from "@/lib/api/client";
import type {
  ChunkRebuildResult,
  ChunkStatus,
  SearchMode,
  SearchResultItem,
} from "@/types/search";

export function rebuildChunks(id: string): Promise<ChunkRebuildResult> {
  return api(`/api/documents/${id}/chunks/rebuild`, { method: "POST" });
}

export function getChunkStatus(id: string): Promise<ChunkStatus> {
  return api<ChunkStatus>(`/api/documents/${id}/chunks/status`);
}

export function searchDocument(
  id: string,
  params: { query: string; mode: SearchMode; limit?: number },
): Promise<SearchResultItem[]> {
  return api<SearchResultItem[]>(`/api/documents/${id}/search`, {
    method: "POST",
    body: JSON.stringify(params),
  });
}
