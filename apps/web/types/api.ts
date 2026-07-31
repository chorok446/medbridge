export interface Meta {
  correlationId: string;
}

export interface ApiSuccess<T> {
  data: T;
  meta: Meta;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    retryable: boolean;
    details: unknown;
  };
  meta: Meta;
}

export type ProcessingStatus =
  | "created"
  | "uploading"
  | "uploaded"
  | "queued"
  | "validating"
  | "ready"
  | "extracting"
  | "extracted"
  | "partially_extracted"
  | "ocr_required"
  | "extraction_failed"
  | "failed"
  | "deleting"
  | "deleted";

export interface DocumentSummary {
  id: string;
  title: string;
  originalFilename: string;
  fileSize: number;
  pageCount: number | null;
  documentType: string;
  processingStatus: ProcessingStatus;
  processingStage: string;
  processingProgress: number;
  failureCode: string | null;
  failureMessage: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface DocumentCreated {
  id: string;
  title: string;
  processingStatus: ProcessingStatus;
  processingStage: string;
  processingProgress: number;
  duplicate: boolean;
}

export interface DocumentList {
  items: DocumentSummary[];
  nextCursor: string | null;
}

export interface DocumentJob {
  id: string;
  jobType: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attemptCount: number;
  maxAttempts: number;
  correlationId: string;
  failureCode: string | null;
  failureMessage: string | null;
  startedAt: string | null;
  completedAt: string | null;
  createdAt: string;
}

export interface DownloadUrl {
  url: string;
  expiresInSeconds: number;
}

export interface UserProfile {
  id: string;
  email: string;
  displayName: string;
  studyLevel: number;
  preferredLanguage: string;
  externalAiAllowed: boolean;
}
