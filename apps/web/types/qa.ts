export type QaMessageRole = "user" | "assistant";

export type QaMessageStatus =
  | "pending"
  | "streaming"
  | "finalizing"
  | "completed"
  | "not_found"
  | "insufficient_evidence"
  | "conflicting_evidence"
  | "failed"
  | "revision_changed"
  | "cancelled"
  | "interrupted";

export type QaClaimVerification = "supported" | "unsupported" | "conflicting";

export interface QaSourceRef {
  pageNumber: number;
  blockId: string;
  bbox: [number, number, number, number];
  readingOrder: number;
  sourceMethod: "digital" | "ocr";
  sectionTitle?: string | null;
}

export interface QaClaim {
  text: string;
  verificationStatus: QaClaimVerification;
  sourceRefs: QaSourceRef[];
}

export interface QaMessage {
  id: string;
  role: QaMessageRole;
  content: string;
  status: QaMessageStatus;
  sequenceNumber: number;
  retrievalMode: string | null;
  claims: QaClaim[];
}

export interface QaThread {
  id: string;
  title: string | null;
  archived: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface QaThreadDetail {
  thread: QaThread;
  messages: QaMessage[];
}

// --- 스트리밍(4B) ---

/** claim 이벤트의 출처 — 내부 chunk id·DB 구조 없이 UI 이동에 필요한 필드만. */
export interface QaStreamSource {
  pageNumber: number;
  bbox: [number, number, number, number];
  blockId: string;
  sectionTitle?: string | null;
  sourceMethod: "digital" | "ocr";
}

export type QaStreamEvent =
  | { type: "started"; requestId: string; messageId: string }
  | { type: "phase"; phase: "retrieving" | "generating" | "finalizing" }
  | { type: "claim"; seq: number; claimIndex: number; text: string; sources: QaStreamSource[] }
  | { type: "completed"; message: QaMessage }
  | { type: "cancelled"; messageId: string }
  | { type: "interrupted"; code: string; retryable: boolean }
  | { type: "error"; code: string; message: string; retryable: boolean }
  | { type: "heartbeat"; seq: number };

/** UI 상태 머신 단계. */
export type QaStreamPhase =
  | "idle"
  | "connecting"
  | "retrieving"
  | "generating"
  | "finalizing"
  | "completed"
  | "cancelling"
  | "cancelled"
  | "interrupted"
  | "failed";
