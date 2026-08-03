import { api } from "@/lib/api/client";
import type { QaThread, QaThreadDetail } from "@/types/qa";

export function listThreads(documentId: string): Promise<QaThread[]> {
  return api<QaThread[]>(`/api/documents/${documentId}/qa/threads`);
}

export function createThread(documentId: string): Promise<{ thread: QaThread }> {
  return api<{ thread: QaThread }>(`/api/documents/${documentId}/qa/threads`, { method: "POST" });
}

export function getThread(documentId: string, threadId: string): Promise<QaThreadDetail> {
  return api<QaThreadDetail>(`/api/documents/${documentId}/qa/threads/${threadId}`);
}

export function updateThread(
  documentId: string,
  threadId: string,
  patch: { title?: string; archived?: boolean },
): Promise<QaThread> {
  return api<QaThread>(`/api/documents/${documentId}/qa/threads/${threadId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteThread(documentId: string, threadId: string): Promise<QaThread> {
  return api<QaThread>(`/api/documents/${documentId}/qa/threads/${threadId}`, { method: "DELETE" });
}

export function askQuestion(
  documentId: string,
  threadId: string,
  question: string,
  learnerLevel?: string,
): Promise<QaThreadDetail> {
  return api<QaThreadDetail>(`/api/documents/${documentId}/qa/threads/${threadId}/messages`, {
    method: "POST",
    body: JSON.stringify(learnerLevel ? { question, learnerLevel } : { question }),
  });
}

export function retryAnswer(
  documentId: string,
  threadId: string,
  learnerLevel?: string,
): Promise<QaThreadDetail> {
  // 수준은 서버에 저장되지 않는다 — 재시도 때도 화면이 현재 선택값을 다시 보낸다.
  return api<QaThreadDetail>(`/api/documents/${documentId}/qa/threads/${threadId}/retry`, {
    method: "POST",
    body: JSON.stringify(learnerLevel ? { learnerLevel } : {}),
  });
}
