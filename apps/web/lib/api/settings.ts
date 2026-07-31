import { api } from "@/lib/api/client";

export interface SummaryModelSettings {
  enabled: boolean;
  providerType: string;
  endpoint: string | null;
  modelName: string | null;
  isLocal: boolean;
  hasApiKey: boolean;
}

export interface SummaryModelSettingsUpdate {
  enabled?: boolean;
  providerType?: string;
  endpoint?: string;
  modelName?: string;
  isLocal?: boolean;
  apiKey?: string;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
}

export function getSummarySettings(): Promise<SummaryModelSettings> {
  return api<SummaryModelSettings>("/api/settings/summary");
}

export function updateSummarySettings(
  patch: SummaryModelSettingsUpdate,
): Promise<SummaryModelSettings> {
  return api<SummaryModelSettings>("/api/settings/summary", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

export function testSummaryConnection(): Promise<ConnectionTestResult> {
  return api<ConnectionTestResult>("/api/settings/summary/test", { method: "POST" });
}

export function deleteSummaryApiKey(): Promise<SummaryModelSettings> {
  return api<SummaryModelSettings>("/api/settings/summary/key", { method: "DELETE" });
}
