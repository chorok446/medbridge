import { getApiConfig } from "@/lib/api/base";
import type { ApiSuccess } from "@/types/api";

export interface ApiErrorContext {
  details?: unknown;
  correlationId?: string | null;
}

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly status: number;
  readonly details: unknown;
  readonly correlationId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    retryable: boolean,
    context: ApiErrorContext = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = context.details ?? null;
    this.correlationId = context.correlationId ?? null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parse an error envelope without trusting its runtime shape. */
export function apiErrorFromResponse(response: Response, body: unknown): ApiError {
  const envelope = isRecord(body) ? body : null;
  const error = envelope && isRecord(envelope.error) ? envelope.error : null;
  const meta = envelope && isRecord(envelope.meta) ? envelope.meta : null;
  const correlationId =
    (typeof meta?.correlationId === "string" ? meta.correlationId : null) ??
    response.headers.get("X-Correlation-ID");

  return new ApiError(
    response.status,
    typeof error?.code === "string" ? error.code : "INTERNAL_ERROR",
    typeof error?.message === "string"
      ? error.message
      : "문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
    typeof error?.retryable === "boolean" ? error.retryable : false,
    { details: error?.details, correlationId },
  );
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
    throw apiErrorFromResponse(res, body);
  }
  return (body as ApiSuccess<T>).data;
}
