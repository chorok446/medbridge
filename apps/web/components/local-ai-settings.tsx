"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useModelDownload } from "@/hooks/use-model-download";
import { ApiError } from "@/lib/api/client";
import {
  activateLocalModel,
  getLocalAiStatus,
  getLocalModels,
  testLocalModel,
  type LocalModel,
} from "@/lib/api/local-ai";
import { openExternalUrl } from "@/lib/tauri";

const OLLAMA_INSTALL_URL = "https://ollama.com/download/windows";
const GIB = 1024 ** 3;

function formatSize(bytes: number): string {
  return `약 ${(bytes / GIB).toFixed(1)}GB`;
}

const RAM_NOTE: Record<string, string | null> = {
  recommended: null,
  selectable: "이 PC에서 쓸 수 있지만 다소 무거울 수 있어요.",
  warn: "이 PC의 메모리로는 느리거나 실행이 어려울 수 있어요.",
  unknown: null,
};

/** 설정 화면의 "로컬 AI" 섹션 — 감지·설치 안내·모델 선택·다운로드·연결·기본 설정. */
export function LocalAiSection() {
  const statusQuery = useQuery({
    queryKey: ["local-ai-status"],
    queryFn: getLocalAiStatus,
    // 무한 polling 금지 — 화면 진입과 "다시 확인"에서만 조회한다.
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    retry: false,
  });

  if (statusQuery.isLoading) {
    return (
      <p role="status" className="text-sm text-slate-500">
        로컬 AI 상태를 확인하는 중이에요…
      </p>
    );
  }

  const status = statusQuery.data?.status ?? "error";
  const recheck = () => statusQuery.refetch();

  if (status === "not_running") {
    return <NotRunning onRecheck={recheck} />;
  }
  if (status === "incompatible") {
    return <Incompatible onRecheck={recheck} />;
  }
  if (status === "error" || statusQuery.isError) {
    return <StatusError onRecheck={recheck} />;
  }
  return <ReadyPanel onRecheck={recheck} />;
}

function NotRunning({ onRecheck }: { onRecheck: () => void }) {
  return (
    <div aria-live="polite" className="flex flex-col gap-3 text-sm">
      <p className="text-slate-700">
        로컬 AI를 사용하려면 먼저 <b>로컬 AI 실행 프로그램</b>이 필요합니다. 아래에서 설치
        안내를 열어 설치한 뒤 “다시 확인”을 눌러 주세요.
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void openExternalUrl(OLLAMA_INSTALL_URL)}
          className="rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700"
        >
          설치 안내 열기
        </button>
        <button
          type="button"
          onClick={onRecheck}
          className="rounded border border-slate-300 px-4 py-2 hover:bg-slate-50"
        >
          다시 확인
        </button>
      </div>
      <p className="text-xs text-slate-400">
        설치 파일은 공식 페이지에서만 받으세요. MedBridge가 대신 내려받지 않습니다.
      </p>
    </div>
  );
}

function Incompatible({ onRecheck }: { onRecheck: () => void }) {
  return (
    <div aria-live="polite" className="flex flex-col gap-3 text-sm">
      <p className="text-slate-700">
        설치된 로컬 AI 실행 프로그램이 오래된 버전이에요. 최신 버전으로 업데이트한 뒤 “다시
        확인”을 눌러 주세요.
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void openExternalUrl(OLLAMA_INSTALL_URL)}
          className="rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700"
        >
          설치 안내 열기
        </button>
        <button
          type="button"
          onClick={onRecheck}
          className="rounded border border-slate-300 px-4 py-2 hover:bg-slate-50"
        >
          다시 확인
        </button>
      </div>
    </div>
  );
}

function StatusError({ onRecheck }: { onRecheck: () => void }) {
  return (
    <div role="alert" className="flex flex-col gap-3 text-sm">
      <p className="text-slate-700">
        로컬 AI 상태를 확인하지 못했어요. 실행 프로그램이 켜져 있는지 확인한 뒤 다시 시도해
        주세요.
      </p>
      <button
        type="button"
        onClick={onRecheck}
        className="w-fit rounded border border-slate-300 px-4 py-2 hover:bg-slate-50"
      >
        다시 시도
      </button>
    </div>
  );
}

function ReadyPanel({ onRecheck }: { onRecheck: () => void }) {
  const queryClient = useQueryClient();
  const modelsQuery = useQuery({
    queryKey: ["local-ai-models"],
    queryFn: getLocalModels,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    retry: false,
  });
  const download = useModelDownload();
  const [selected, setSelected] = useState<string | null>(null);
  const [testMessage, setTestMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirmOverwrite, setConfirmOverwrite] = useState<string | null>(null);

  const testMutation = useMutation({
    mutationFn: (model: string) => testLocalModel(model),
    onSuccess: (r) => setTestMessage({ ok: r.ok, text: r.message }),
    onError: () =>
      setTestMessage({ ok: false, text: "연결을 확인하지 못했어요. 다시 시도해 주세요." }),
  });

  const activateMutation = useMutation({
    mutationFn: ({ model, overwrite }: { model: string; overwrite: boolean }) =>
      activateLocalModel(model, overwrite),
    onSuccess: () => {
      setConfirmOverwrite(null);
      setTestMessage({ ok: true, text: "이 모델을 기본 AI로 설정했어요." });
      void queryClient.invalidateQueries({ queryKey: ["summary-settings"] });
    },
    onError: (err, variables) => {
      if (err instanceof ApiError && err.status === 409) {
        // 외부 AI가 이미 설정됨 → 확인 후 덮어쓰기
        setConfirmOverwrite(variables.model);
      } else {
        setTestMessage({ ok: false, text: "설정을 저장하지 못했어요. 다시 시도해 주세요." });
      }
    },
  });

  if (modelsQuery.isLoading) {
    return (
      <p role="status" className="text-sm text-slate-500">
        모델 목록을 불러오는 중이에요…
      </p>
    );
  }
  if (modelsQuery.isError || !modelsQuery.data) {
    return <StatusError onRecheck={() => modelsQuery.refetch()} />;
  }

  const { models } = modelsQuery.data;
  const installed = models.filter((m) => m.installed);
  const downloading = download.active;
  const activeModel = selected ?? installed[0]?.model ?? modelsQuery.data.defaultModel;

  async function onDownloadDone(model: string) {
    // 다운로드 종료 → 실제 설치 상태 재확인. 실제로 설치됐을 때만 자동 연결 확인.
    const fresh = await modelsQuery.refetch();
    if (fresh.data?.models.some((m) => m.model === model && m.installed)) {
      testMutation.mutate(model);
    }
  }

  return (
    <div className="flex flex-col gap-4 text-sm">
      {/* 다운로드 진행 중 */}
      {downloading && (
        <DownloadProgress
          label={modelLabel(models, download.state.model)}
          stepLabel={download.state.stepLabel}
          percent={download.state.percent}
          onCancel={download.cancel}
        />
      )}

      {!downloading && (
        <>
          {installed.length > 0 && (
            <ReadyWithModels
              installed={installed}
              activeModel={activeModel}
              onSelect={setSelected}
              onTest={(m) => testMutation.mutate(m)}
              onActivate={(m) => activateMutation.mutate({ model: m, overwrite: false })}
              testing={testMutation.isPending}
              activating={activateMutation.isPending}
            />
          )}

          <ModelChooser
            models={models}
            defaultModel={modelsQuery.data.defaultModel}
            onDownload={(m) => {
              setTestMessage(null);
              void download.start(m).then(() => onDownloadDone(m));
            }}
          />
        </>
      )}

      {download.state.phase === "failed" && (
        <p role="alert" className="rounded bg-red-50 px-3 py-2 text-red-700">
          {download.state.errorMessage} 저장 공간과 실행 상태를 확인해 주세요.{" "}
          <button
            type="button"
            onClick={() => download.state.model && void download.start(download.state.model)}
            className="font-medium underline"
          >
            다시 시도
          </button>
        </p>
      )}

      {testMessage && (
        <p
          role="status"
          aria-live="polite"
          className={
            testMessage.ok
              ? "rounded bg-green-50 px-3 py-2 text-green-800"
              : "rounded bg-amber-50 px-3 py-2 text-amber-800"
          }
        >
          {testMessage.text}
        </p>
      )}

      {confirmOverwrite && (
        <div role="alertdialog" className="rounded border border-amber-200 bg-amber-50 p-3">
          <p className="mb-2 text-amber-900">
            이미 외부 AI가 설정되어 있어요. 로컬 AI로 바꿀까요?
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() =>
                activateMutation.mutate({ model: confirmOverwrite, overwrite: true })
              }
              className="rounded bg-blue-600 px-3 py-1.5 font-medium text-white hover:bg-blue-700"
            >
              로컬 AI로 변경
            </button>
            <button
              type="button"
              onClick={() => setConfirmOverwrite(null)}
              className="rounded border border-slate-300 px-3 py-1.5 hover:bg-white"
            >
              취소
            </button>
          </div>
        </div>
      )}

      <button
        type="button"
        onClick={onRecheck}
        className="w-fit text-xs text-slate-400 underline hover:text-slate-600"
      >
        상태 다시 확인
      </button>
    </div>
  );
}

function DownloadProgress({
  label,
  stepLabel,
  percent,
  onCancel,
}: {
  label: string;
  stepLabel: string;
  percent: number | null;
  onCancel: () => void;
}) {
  return (
    <div aria-live="polite" className="rounded-lg border border-slate-200 p-3">
      <p className="mb-2 font-medium text-slate-800">{label} 내려받는 중…</p>
      <div
        role="progressbar"
        aria-label="다운로드 진행률"
        aria-valuenow={percent ?? undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        className="h-2.5 w-full overflow-hidden rounded-full bg-slate-100"
      >
        <div
          className="h-full rounded-full bg-blue-600 transition-all"
          style={{ width: percent === null ? "40%" : `${percent}%` }}
        />
      </div>
      <div className="mt-2 flex items-center justify-between">
        <span className="text-xs text-slate-500">
          {stepLabel}
          {percent !== null && ` · ${Math.round(percent)}%`}
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-slate-300 px-2.5 py-1 text-xs hover:bg-slate-50"
        >
          중단
        </button>
      </div>
      <p className="mt-2 text-xs text-slate-400">
        앱을 종료하면 다음 실행에서 설치 상태를 다시 확인해요.
      </p>
    </div>
  );
}

function ReadyWithModels({
  installed,
  activeModel,
  onSelect,
  onTest,
  onActivate,
  testing,
  activating,
}: {
  installed: LocalModel[];
  activeModel: string;
  onSelect: (model: string) => void;
  onTest: (model: string) => void;
  onActivate: (model: string) => void;
  testing: boolean;
  activating: boolean;
}) {
  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <p className="mb-2 font-medium text-slate-800">사용할 수 있는 모델</p>
      <fieldset className="flex flex-col gap-2">
        <legend className="sr-only">설치된 모델 선택</legend>
        {installed.map((m) => (
          <label key={m.model} className="flex items-center gap-2">
            <input
              type="radio"
              name="installed-model"
              value={m.model}
              checked={activeModel === m.model}
              onChange={() => onSelect(m.model)}
              className="h-4 w-4"
            />
            <span>
              {m.label} <span className="text-xs text-slate-400">({formatSize(m.approxBytes)})</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onTest(activeModel)}
          disabled={testing}
          className="rounded border border-slate-300 px-3 py-1.5 hover:bg-slate-50 disabled:opacity-50"
        >
          {testing ? "확인 중…" : "연결 확인"}
        </button>
        <button
          type="button"
          onClick={() => onActivate(activeModel)}
          disabled={activating}
          className="rounded bg-blue-600 px-3 py-1.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          기본 모델로 사용
        </button>
      </div>
    </div>
  );
}

function ModelChooser({
  models,
  defaultModel,
  onDownload,
}: {
  models: LocalModel[];
  defaultModel: string;
  onDownload: (model: string) => void;
}) {
  const notInstalled = models.filter((m) => !m.installed);
  if (notInstalled.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <p className="font-medium text-slate-800">모델 내려받기</p>
      <ul className="flex flex-col gap-2">
        {notInstalled.map((m) => {
          const ramNote = RAM_NOTE[m.ramAdvice];
          const isDefault = m.model === defaultModel;
          return (
            <li
              key={m.model}
              className="flex flex-col gap-1.5 rounded-lg border border-slate-200 p-3"
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-slate-800">
                  {m.label}
                  {isDefault && (
                    <span className="ml-2 rounded bg-blue-50 px-1.5 py-0.5 text-[11px] text-blue-700">
                      추천
                    </span>
                  )}
                </span>
                <span className="text-xs text-slate-500">{formatSize(m.approxBytes)}</span>
              </div>
              <p className="text-xs text-slate-500">{m.description}</p>
              {ramNote && <p className="text-xs text-amber-700">⚠ {ramNote}</p>}
              {!m.diskOk && (
                <p className="text-xs text-red-700">
                  ⚠ 저장 공간이 부족할 수 있어요. 공간을 확보한 뒤 다시 시도해 주세요.
                </p>
              )}
              <button
                type="button"
                onClick={() => onDownload(m.model)}
                disabled={!m.diskOk}
                className={
                  isDefault
                    ? "mt-1 w-fit rounded bg-blue-600 px-3 py-1.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                    : "mt-1 w-fit rounded border border-slate-300 px-3 py-1.5 hover:bg-slate-50 disabled:opacity-50"
                }
              >
                내려받기
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function modelLabel(models: LocalModel[], model: string | null): string {
  return models.find((m) => m.model === model)?.label ?? "모델";
}
