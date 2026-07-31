import { getApiConfig } from "@/lib/api/base";
import type { ApiErrorBody, ApiSuccess } from "@/types/api";

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly status: number;

  constructor(status: number, code: string, message: string, retryable: boolean) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const { base, token } = await getApiConfig();
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      ...(init?.body && typeof init.body === "string"
        ? { "Content-Type": "application/json" }
        : {}),
      ...(token ? { "X-MedBridge-Token": token } : {}),
      ...init?.headers,
    },
  });
  if (res.status === 204) {
    return undefined as T;
  }
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body as ApiErrorBody | null;
    throw new ApiError(
      res.status,
      err?.error.code ?? "INTERNAL_ERROR",
      err?.error.message ?? "문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
      err?.error.retryable ?? false,
    );
  }
  return (body as ApiSuccess<T>).data;
}
