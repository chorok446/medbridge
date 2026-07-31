export type SearchMode = "keyword" | "vector" | "hybrid";

export interface ChunkRebuildResult {
  jobId: string | null;
  started: boolean;
}

export interface ChunkStatus {
  chunkCount: number;
  lastRebuiltAt: string | null;
  jobStatus: string | null;
  embeddingAvailable: boolean;
}

export interface SearchSourceRef {
  pageNumber: number;
  blockId: string;
  bbox: [number, number, number, number];
  readingOrder: number;
  sourceMethod: "digital" | "ocr";
}

export interface SearchResultItem {
  chunkId: string;
  preview: string;
  sectionTitle: string | null;
  pageStart: number;
  pageEnd: number;
  sourceRefs: SearchSourceRef[];
  matchType: "keyword" | "vector" | "hybrid";
}
