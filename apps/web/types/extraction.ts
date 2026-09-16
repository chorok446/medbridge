export interface ExtractionStatus {
  processingStatus: string;
  processingProgress: number;
  pageCount: number | null;
  pagesDone: number;
  pagesOcrRequired: number;
  pagesFailed: number;
}

export interface PageSummary {
  pageNumber: number;
  width: number;
  height: number;
  rotation: number;
  extractionStatus: "pending" | "extracted" | "ocr_required" | "failed";
  scanVerdict: "digital" | "mixed" | "scanned" | "unknown";
  requiresOcr: boolean;
  readingOrderConfidence: number;
  textCharacterCount: number;
  tableCount: number;
}

export interface PageDetail extends PageSummary {
  rawText: string;
  normalizedText: string;
}

export interface ExtractionBlock {
  id: string;
  blockIndex: number;
  blockType: "text" | "image" | "vector" | "table" | "caption" | "unknown";
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  text: string;
  readingOrder: number;
  isHeader: boolean;
  isFooter: boolean;
  isTable: boolean;
  isCaption: boolean;
}

export interface ExtractionTable {
  id: string;
  pageNumber: number;
  tableIndex: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  rowCount: number;
  columnCount: number;
  /** 행 우선 셀 격자(첫 행이 머리글, 빈 칸은 null). 화면은 이걸로 진짜 표를 그린다. */
  cells: (string | null)[][];
  markdownText: string;
  confidence: number;
  extractionStatus: string;
}

export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}
