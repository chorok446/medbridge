import { getApiConfig } from "@/lib/api/base";
import { api } from "@/lib/api/client";
import type {
  DocumentCreated,
  DocumentJob,
  DocumentList,
  DocumentSummary,
} from "@/types/api";

export function listDocuments(cursor?: string): Promise<DocumentList> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  const qs = params.toString();
  return api<DocumentList>(`/api/documents${qs ? `?${qs}` : ""}`);
}

export function getDocument(id: string): Promise<DocumentSummary> {
  return api<DocumentSummary>(`/api/documents/${id}`);
}

export function deleteDocument(id: string): Promise<{ id: string; processingStatus: string }> {
  return api(`/api/documents/${id}`, { method: "DELETE" });
}

export function retryDocument(id: string): Promise<DocumentSummary> {
  return api<DocumentSummary>(`/api/documents/${id}/retry`, { method: "POST" });
}

export function renameDocument(id: string, title: string): Promise<DocumentSummary> {
  return api<DocumentSummary>(`/api/documents/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export function listJobs(id: string): Promise<DocumentJob[]> {
  return api<DocumentJob[]>(`/api/documents/${id}/jobs`);
}

export function reportError(documentId: string | null, description: string) {
  return api<{ received: boolean }>(`/api/reports`, {
    method: "POST",
    body: JSON.stringify({ documentId, description }),
  });
}

/** PDF 미리보기 iframe용 URL (iframe은 헤더를 못 보내므로 토큰은 query로) */
export async function documentFileUrl(id: string): Promise<string> {
  const { base, token } = await getApiConfig();
  return `${base}/api/documents/${id}/file${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}

export interface UploadHandle {
  promise: Promise<DocumentCreated>;
  abort: () => void;
}

/** fetch는 업로드 진행률을 제공하지 않아 XHR을 사용한다. */
export function uploadDocument(
  file: File,
  title: string | undefined,
  onProgress: (percent: number) => void,
): UploadHandle {
  const xhr = new XMLHttpRequest();
  let aborted = false;
  const promise = new Promise<DocumentCreated>((resolve, reject) => {
    getApiConfig().then(({ base, token }) => {
      // 설정 해석 중(xhr.open 전)의 취소 — xhr.abort()는 이 시점엔 abort 이벤트를 못 낸다.
      if (aborted) {
        reject(new Error("업로드를 취소했습니다."));
        return;
      }
      const form = new FormData();
      form.append("file", file);
      if (title) form.append("title", title);

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      });
      xhr.addEventListener("load", () => {
        let parsed: unknown = null;
        try {
          parsed = JSON.parse(xhr.responseText) as unknown;
        } catch {
          parsed = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve((parsed as { data: DocumentCreated }).data);
        } else {
          const err = parsed as { error?: { message?: string } } | null;
          reject(
            new Error(
              err?.error?.message ?? "파일을 올리지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ),
          );
        }
      });
      xhr.addEventListener("error", () =>
        reject(new Error("연결에 문제가 생겨 파일을 올리지 못했습니다. 다시 시도해 주세요.")),
      );
      xhr.addEventListener("abort", () => reject(new Error("업로드를 취소했습니다.")));
      xhr.open("POST", `${base}/api/documents`);
      if (token) xhr.setRequestHeader("X-MedBridge-Token", token);
      xhr.send(form);
      // 설정 해석 실패를 삼키면 promise가 영원히 pending — 업로드 UI 전체가 먹통이 된다.
    }, reject);
  });
  return {
    promise,
    abort: () => {
      aborted = true;
      xhr.abort();
    },
  };
}
