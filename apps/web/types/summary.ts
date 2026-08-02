export type LearnerLevel = "concise" | "nursing_student" | "experienced_nurse";

export type SummaryArtifactType =
  | "overview"
  | "section_summary"
  | "key_concept"
  | "prerequisite"
  | "important_number"
  | "target_population"
  | "learner_explanation"
  | "study_caution";

export interface SummarySourceRef {
  pageNumber: number;
  blockId: string;
  bbox: [number, number, number, number];
  readingOrder: number;
  sourceMethod: "digital" | "ocr";
}

export interface SummaryArtifact {
  artifactType: SummaryArtifactType;
  title: string | null;
  position: number;
  content: Record<string, unknown>;
  sourceRefs: SummarySourceRef[];
}

export interface SummaryList {
  stale: boolean;
  artifacts: SummaryArtifact[];
}

export type SummaryFailureCategory =
  | "timeout"
  | "invalid_response"
  | "context_overflow"
  | "empty_result"
  // 실패가 아니라 "성공했지만 일부 내용이 빠짐" — status는 succeeded로 온다.
  | "partial_content";

export interface SummaryStatus {
  providerAvailable: boolean;
  status: string | null;
  stale: boolean;
  sourceRevision: number | null;
  currentRevision: number;
  progress: number;
  canRetry: boolean;
  failureCategory: SummaryFailureCategory | null;
  /** 화면에 보이는 요약에 내용이 빠졌는지. 최신 run이 아니라 그 요약을 만든 run 기준이다. */
  partial: boolean;
}

export interface SummaryStartResult {
  runId: string | null;
  started: boolean;
}
