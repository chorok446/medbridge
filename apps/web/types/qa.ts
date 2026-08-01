export type QaMessageRole = "user" | "assistant";

export type QaMessageStatus =
  | "pending"
  | "completed"
  | "not_found"
  | "insufficient_evidence"
  | "conflicting_evidence"
  | "failed"
  | "revision_changed";

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
