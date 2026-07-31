"use client";

import { useCallback, useId, useRef, useState } from "react";
import { uploadDocument, type UploadHandle } from "@/lib/api/documents";
import { formatBytes } from "@/lib/format";
import type { DocumentCreated } from "@/types/api";

const MAX_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? "50");

interface Props {
  onUploaded: (doc: DocumentCreated) => void;
}

export function UploadDropzone({ onUploaded }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const handleRef = useRef<UploadHandle | null>(null);
  const checkboxId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [privacyConfirmed, setPrivacyConfirmed] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const showError = useCallback((message: string) => {
    setError(message);
    // 오류 발생 위치로 포커스 이동 (스크린리더 안내)
    setTimeout(() => errorRef.current?.focus(), 0);
  }, []);

  const selectFile = useCallback(
    (f: File | null) => {
      setError(null);
      if (!f) return;
      if (!f.name.toLowerCase().endsWith(".pdf") && f.type !== "application/pdf") {
        showError("PDF 파일만 업로드할 수 있습니다. 선택한 파일을 확인해 주세요.");
        return;
      }
      if (f.size > MAX_MB * 1024 * 1024) {
        showError(`파일이 최대 크기(${MAX_MB}MB)를 넘습니다. 더 작은 PDF를 선택해 주세요.`);
        return;
      }
      setFile(f);
    },
    [showError],
  );

  async function startUpload() {
    if (!file || !privacyConfirmed) return;
    setError(null);
    setProgress(0);
    const handle = uploadDocument(file, undefined, setProgress);
    handleRef.current = handle;
    try {
      const doc = await handle.promise;
      setFile(null);
      setPrivacyConfirmed(false);
      setProgress(null);
      onUploaded(doc);
    } catch (err) {
      setProgress(null);
      showError(err instanceof Error ? err.message : "파일을 올리지 못했습니다.");
    } finally {
      handleRef.current = null;
    }
  }

  const uploading = progress !== null;

  return (
    <section
      id="upload-section"
      aria-label="PDF 추가"
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (!uploading) selectFile(e.dataTransfer.files[0] ?? null);
        }}
        className={`flex flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed px-4 py-10 text-center transition-colors ${
          dragOver ? "border-blue-500 bg-blue-50" : "border-slate-300"
        }`}
      >
        <p className="text-lg font-medium">PDF를 여기로 끌어다 놓으세요</p>
        <button
          type="button"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
          className="rounded-md bg-blue-600 px-6 py-3 text-base font-semibold text-white hover:bg-blue-700 focus:outline-2 focus:outline-offset-2 focus:outline-blue-600 disabled:opacity-50"
        >
          PDF 파일 선택
        </button>
        <p className="text-sm text-slate-500">최대 {MAX_MB}MB까지 올릴 수 있어요</p>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        className="sr-only"
        aria-label="PDF 파일 선택"
        onChange={(e) => selectFile(e.target.files?.[0] ?? null)}
      />

      <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
        <div className="rounded bg-green-50 px-3 py-2 text-green-800">
          <p className="font-semibold">이런 자료를 올릴 수 있어요</p>
          <p>강의자료 · 의학 논문 · 교과서 발췌 · 익명 처리된 학습용 사례</p>
        </div>
        <div className="rounded bg-amber-50 px-3 py-2 text-amber-800">
          <p className="font-semibold">이런 자료는 올리면 안 돼요</p>
          <p>실제 환자 이름·등록번호가 있는 기록 · 진료 차트 원본 · 검사 결과지 원본</p>
        </div>
      </div>

      {file && !uploading && (
        <div className="mt-3 rounded border border-slate-200 px-3 py-3 text-sm">
          <p className="mb-2 truncate">
            <span className="font-medium">{file.name}</span>{" "}
            <span className="text-slate-500">({formatBytes(file.size)})</span>
          </p>
          <label htmlFor={checkboxId} className="flex cursor-pointer items-start gap-2">
            <input
              id={checkboxId}
              type="checkbox"
              checked={privacyConfirmed}
              onChange={(e) => setPrivacyConfirmed(e.target.checked)}
              className="mt-0.5 h-5 w-5"
            />
            <span>
              이 파일에 실제 환자를 알아볼 수 있는 정보(이름·등록번호 등)가 없는 것을
              확인했어요.
            </span>
          </label>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => {
                setFile(null);
                setPrivacyConfirmed(false);
              }}
              className="rounded border border-slate-300 px-4 py-2 hover:bg-slate-50"
            >
              선택 취소
            </button>
            <button
              type="button"
              onClick={startUpload}
              disabled={!privacyConfirmed}
              className="rounded bg-blue-600 px-5 py-2 font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              업로드
            </button>
          </div>
        </div>
      )}

      {uploading && (
        <div className="mt-3 text-sm">
          <div className="mb-1 flex items-center justify-between">
            <span className="truncate">{file?.name} — 파일을 올리는 중…</span>
            <button
              type="button"
              onClick={() => handleRef.current?.abort()}
              className="rounded border border-slate-300 px-3 py-1.5 hover:bg-slate-50"
            >
              취소
            </button>
          </div>
          <div
            role="progressbar"
            aria-valuenow={progress ?? 0}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="업로드 진행률"
            className="h-2.5 overflow-hidden rounded bg-slate-200"
          >
            <div className="h-full bg-blue-600 transition-all" style={{ width: `${progress}%` }} />
          </div>
          <p aria-live="polite" className="mt-1 text-slate-600">
            {progress}% 완료
          </p>
        </div>
      )}

      {error && (
        <p
          ref={errorRef}
          role="alert"
          tabIndex={-1}
          className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700"
        >
          {error}
        </p>
      )}
    </section>
  );
}
