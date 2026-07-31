import { api } from "@/lib/api/client";
import type {
  DocumentCreated,
  DocumentJob,
  DocumentList,
  DocumentSummary,
  DownloadUrl,
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

export function listJobs(id: string): Promise<DocumentJob[]> {
  return api<DocumentJob[]>(`/api/documents/${id}/jobs`);
}

export function getDownloadUrl(id: string): Promise<DownloadUrl> {
  return api<DownloadUrl>(`/api/documents/${id}/download-url`);
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
  const promise = new Promise<DocumentCreated>((resolve, reject) => {
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
        const err = parsed as
          | { error?: { code?: string; message?: string; retryable?: boolean } }
          | null;
        reject(
          new Error(err?.error?.message ?? "업로드에 실패했습니다. 잠시 후 다시 시도해 주세요."),
        );
      }
    });
    xhr.addEventListener("error", () =>
      reject(new Error("네트워크 오류로 업로드에 실패했습니다.")),
    );
    xhr.addEventListener("abort", () => reject(new Error("업로드를 취소했습니다.")));
    xhr.open("POST", "/api/documents");
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}
