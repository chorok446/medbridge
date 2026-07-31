"use client";

import { useCallback, useRef, useState } from "react";
import { uploadDocument, type UploadHandle } from "@/lib/api/documents";
import { formatBytes } from "@/lib/format";
import type { DocumentCreated } from "@/types/api";

const MAX_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? "50");

interface Props {
  onUploaded: (doc: DocumentCreated) => void;
}

export function UploadDropzone({ onUploaded }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const handleRef = useRef<UploadHandle | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const selectFile = useCallback((f: File | null) => {
    setError(null);
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".pdf") && f.type !== "application/pdf") {
      setError("PDF 파일만 업로드할 수 있습니다.");
      return;
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      setError(`파일이 최대 크기(${MAX_MB}MB)를 초과했습니다.`);
      return;
    }
    setFile(f);
  }, []);

  async function startUpload() {
    if (!file) return;
    setError(null);
    setProgress(0);
    const handle = uploadDocument(file, undefined, setProgress);
    handleRef.current = handle;
    try {
      const doc = await handle.promise;
      setFile(null);
      setProgress(null);
      onUploaded(doc);
    } catch (err) {
      setProgress(null);
      setError(err instanceof Error ? err.message : "업로드에 실패했습니다.");
    } finally {
      handleRef.current = null;
    }
  }

  function cancelUpload() {
    handleRef.current?.abort();
  }

  const uploading = progress !== null;

  return (
    <section aria-label="PDF 업로드" className="rounded-lg border border-slate-200 bg-white p-4">
      <div
        role="button"
        tabIndex={0}
        aria-label="PDF 파일을 끌어다 놓거나 클릭해서 선택"
        onClick={() => !uploading && inputRef.current?.click()}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && !uploading) inputRef.current?.click();
        }}
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
        className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-md border-2 border-dashed px-4 py-8 text-center transition-colors ${
          dragOver ? "border-blue-500 bg-blue-50" : "border-slate-300 hover:border-slate-400"
        }`}
      >
        <p className="font-medium">PDF를 여기로 끌어다 놓거나 클릭해서 선택하세요</p>
        <p className="text-sm text-slate-500">PDF만 지원 · 최대 {MAX_MB}MB</p>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        className="hidden"
        aria-hidden
        onChange={(e) => selectFile(e.target.files?.[0] ?? null)}
      />

      <p className="mt-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">
        ⚠️ 환자 이름·등록번호 등 의료 개인정보가 포함된 파일은 업로드 후 개인정보 검토를 거치기
        전까지 외부 AI로 전송되지 않습니다. 가능하면 익명화된 자료를 사용하세요.
      </p>

      {file && !uploading && (
        <div className="mt-3 flex items-center justify-between gap-2 rounded border border-slate-200 px-3 py-2 text-sm">
          <span className="truncate">
            {file.name} <span className="text-slate-500">({formatBytes(file.size)})</span>
          </span>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={() => setFile(null)}
              className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-50"
            >
              선택 취소
            </button>
            <button
              type="button"
              onClick={startUpload}
              className="rounded bg-blue-600 px-3 py-1 font-medium text-white hover:bg-blue-700"
            >
              업로드
            </button>
          </div>
        </div>
      )}

      {uploading && (
        <div className="mt-3 text-sm">
          <div className="mb-1 flex items-center justify-between">
            <span className="truncate">{file?.name} 업로드 중…</span>
            <button
              type="button"
              onClick={cancelUpload}
              className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-50"
            >
              취소
            </button>
          </div>
          <div
            role="progressbar"
            aria-valuenow={progress ?? 0}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-2 overflow-hidden rounded bg-slate-200"
          >
            <div className="h-full bg-blue-600 transition-all" style={{ width: `${progress}%` }} />
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
    </section>
  );
}
