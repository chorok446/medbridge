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
  /** 청크가 0개인 이유. 이유를 모르면 통하지 않는 안내를 반복하게 된다. */
  failureCode: string | null;
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
