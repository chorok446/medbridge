import { getApiConfig } from "@/lib/api/base";
import { api, apiErrorFromResponse } from "@/lib/api/client";
import { consumeNdjson } from "@/lib/api/ndjson";

export type LocalAiStatus = "ready" | "not_running" | "incompatible" | "error";

export interface LocalAiStatusResult {
  status: LocalAiStatus;
}

export type RamAdvice = "recommended" | "selectable" | "warn" | "unknown";

export interface LocalModel {
  model: string; // 내부명 — 상세 보기 전용
  tier: "light" | "balanced" | "quality" | "advanced";
  label: string;
  description: string;
  approxBytes: number;
  installed: boolean;
  recommended: boolean;
  ramAdvice: RamAdvice;
  diskOk: boolean;
  requiredBytes: number;
}

export interface LocalModelsResult {
  models: LocalModel[];
  defaultModel: string;
  totalRamBytes: number | null;
  freeDiskBytes: number | null;
}

export interface LocalTestResult {
  ok: boolean;
  message: string;
}

export interface LocalActivateResult {
  enabled: boolean;
  providerType: string;
  modelName: string | null;
  isLocal: boolean;
}

/** 다운로드 진행 이벤트(NDJSON). blob/digest/manifest 등 내부 용어는 포함하지 않는다. */
export type PullEvent =
  | { type: "progress"; phase: string; total?: number; completed?: number; percent?: number }
  | { type: "completed" }
  | { type: "error"; message: string };

export function getLocalAiStatus(): Promise<LocalAiStatusResult> {
  return api<LocalAiStatusResult>("/api/local-ai/status");
}

export function getLocalModels(): Promise<LocalModelsResult> {
  return api<LocalModelsResult>("/api/local-ai/models");
}

export function testLocalModel(model: string): Promise<LocalTestResult> {
  return api<LocalTestResult>("/api/local-ai/test", {
    method: "POST",
    body: JSON.stringify({ model }),
  });
}

export function activateLocalModel(
  model: string,
  overwriteExternal = false,
): Promise<LocalActivateResult> {
  return api<LocalActivateResult>("/api/local-ai/activate", {
    method: "POST",
    body: JSON.stringify({ model, overwriteExternal }),
  });
}

/**
 * 모델 다운로드 시작 — NDJSON 진행 이벤트를 onEvent로 흘려준다. 취소는 AbortController.
 * fetch를 쓰는 이유: 토큰 헤더 + 취소가 필요(EventSource 불가).
 */
export async function streamModelPull(
  model: string,
  opts: { signal: AbortSignal; onEvent: (event: PullEvent) => void },
): Promise<void> {
  const { base, token } = await getApiConfig();
  const res = await fetch(`${base}/api/local-ai/models/pull`, {
    method: "POST",
    signal: opts.signal,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-MedBridge-Token": token } : {}),
    },
    body: JSON.stringify({ model }),
  });
  if (!res.ok || !res.body) {
    const body: unknown = await res.json().catch(() => null);
    throw apiErrorFromResponse(res, body);
  }
  await consumeNdjson<PullEvent>(res.body, opts.onEvent);
}
