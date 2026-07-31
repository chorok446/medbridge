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

export interface SummaryStatus {
  providerAvailable: boolean;
  status: string | null;
  stale: boolean;
  sourceRevision: number | null;
  currentRevision: number;
  progress: number;
  canRetry: boolean;
}

export interface SummaryStartResult {
  runId: string | null;
  started: boolean;
}
