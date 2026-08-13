"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  deleteSummaryApiKey,
  getSummarySettings,
  testSummaryConnection,
  updateSummarySettings,
} from "@/lib/api/settings";
import type { ConnectionTestResult, SummaryModelSettings } from "@/lib/api/settings";

function settingsSnapshotKey(settings: SummaryModelSettings): string {
  return JSON.stringify([
    settings.enabled,
    settings.providerType,
    settings.endpoint,
    settings.modelName,
    settings.isLocal,
    settings.hasApiKey,
  ]);
}

/**
 * 요약 모델 설정 — 사용 여부/공급자/endpoint/모델/API 키/연결 확인/외부 전송 경고.
 * API 키는 저장 후 되돌려 표시하지 않는다(설정됨/미설정만). 포트·env·JSON 설정법은
 * 노출하지 않는다.
 */
export function SummaryModelSection() {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["summary-settings"],
    queryFn: getSummarySettings,
  });

  if (isLoading) {
    return <p className="text-sm text-slate-500">불러오는 중…</p>;
  }
  if (isError || !data) {
    return <ErrorBoxInline onRetry={() => refetch()} />;
  }
  // 폼 상태를 서버 값으로 시드하기 위해 로드 완료 후에만 폼을 마운트한다(useEffect 불필요).
  return (
    <>
      <SummaryModelForm
        key={settingsSnapshotKey(data)}
        initial={data}
        onDraftChanged={() => {
          setNotice(null);
          setTestResult(null);
        }}
        onSettingsChanged={(settings, message) => {
          queryClient.setQueryData(["summary-settings"], settings);
          setNotice(message);
        }}
        onTested={(settings, result) => {
          queryClient.setQueryData(["summary-settings"], settings);
          setNotice("입력한 설정을 저장한 뒤 연결을 확인했습니다.");
          setTestResult(result);
        }}
      />
      {notice && (
        <p role="status" className="mt-3 rounded bg-green-50 px-3 py-2 text-sm text-green-800">
          {notice}
        </p>
      )}
      {testResult && (
        <p
          role={testResult.ok ? "status" : "alert"}
          className={`mt-3 rounded px-3 py-2 text-sm ${
            testResult.ok ? "bg-green-50 text-green-800" : "bg-red-50 text-red-700"
          }`}
        >
          {testResult.message}
        </p>
      )}
    </>
  );
}

function SummaryModelForm({
  initial,
  onDraftChanged,
  onSettingsChanged,
  onTested,
}: {
  initial: SummaryModelSettings;
  onDraftChanged: () => void;
  onSettingsChanged: (settings: SummaryModelSettings, message: string) => void;
  onTested: (settings: SummaryModelSettings, result: ConnectionTestResult) => void;
}) {
  const [enabled, setEnabled] = useState(initial.enabled);
  const [endpoint, setEndpoint] = useState(initial.endpoint ?? "");
  const [modelName, setModelName] = useState(initial.modelName ?? "");
  const [isLocal, setIsLocal] = useState(initial.isLocal);
  const [apiKey, setApiKey] = useState("");
  const [confirmDeleteKey, setConfirmDeleteKey] = useState(false);

  function draftSettings() {
    return {
      enabled,
      providerType: "openai_compatible",
      endpoint,
      modelName,
      isLocal,
      // 빈 칸은 이미 저장된 키를 유지한다. 삭제는 별도 확인 동작으로만 수행한다.
      ...(apiKey ? { apiKey } : {}),
    };
  }

  const saveMutation = useMutation({
    mutationFn: () => updateSummarySettings(draftSettings()),
    onSuccess: (settings) => {
      setApiKey("");
      onSettingsChanged(settings, "저장되었습니다.");
    },
  });

  const testMutation = useMutation({
    mutationFn: async () => {
      // 연결 확인 API는 저장된 설정을 검사한다. 그래서 현재 폼 draft를 먼저 저장해
      // 화면에 보이는 주소·모델·키와 실제 검사 대상이 달라지는 일을 막는다.
      const settings = await updateSummarySettings(draftSettings());
      try {
        const result = await testSummaryConnection();
        return { settings, result };
      } catch {
        return {
          settings,
          result: {
            ok: false,
            message: "설정은 저장했지만 연결 확인 요청을 완료하지 못했습니다. 다시 시도해 주세요.",
          },
        };
      }
    },
    onSuccess: ({ settings, result }) => {
      setApiKey("");
      onTested(settings, result);
    },
  });

  const deleteKeyMutation = useMutation({
    mutationFn: deleteSummaryApiKey,
    onSuccess: (settings) => {
      setApiKey("");
      onSettingsChanged(settings, "저장된 API 키를 삭제했습니다.");
    },
  });

  const external = enabled && !isLocal;
  const actionPending =
    saveMutation.isPending || testMutation.isPending || deleteKeyMutation.isPending;

  return (
    <>
      <form
        className="flex flex-col gap-3 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          onDraftChanged();
          saveMutation.mutate();
        }}
      >
        <p className="text-xs text-slate-500">
          요약 기능을 쓰려면 요약 모델을 연결하세요. 연결하지 않아도 문서 열람과 검색은
          그대로 쓸 수 있어요.
        </p>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={enabled}
            disabled={actionPending}
            onChange={(e) => {
              setEnabled(e.target.checked);
              onDraftChanged();
            }}
          />
          요약 모델 사용
        </label>

        <p className="text-xs text-slate-500">연결 방식: OpenAI 호환 모델 서비스</p>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={isLocal}
            disabled={actionPending}
            onChange={(e) => {
              setIsLocal(e.target.checked);
              onDraftChanged();
            }}
          />
          내 컴퓨터/사내에서 실행되는 로컬 모델이에요
        </label>

        <label className="flex flex-col gap-1">
          서비스 주소
          <input
            value={endpoint}
            disabled={actionPending}
            onChange={(e) => {
              setEndpoint(e.target.value);
              onDraftChanged();
            }}
            placeholder={
              isLocal ? "예: http://localhost:11434/v1" : "예: https://api.example.com/v1"
            }
            className="rounded border border-slate-300 px-3 py-2"
          />
          <span className="text-xs text-slate-500">
            {isLocal
              ? "로컬 모델은 내 컴퓨터 주소(localhost 또는 127.0.0.1)만 쓸 수 있어요."
              : "외부 모델 주소는 https:// 로 시작해야 안전하게 연결돼요."}
          </span>
        </label>

        <label className="flex flex-col gap-1">
          모델 이름
          <input
            value={modelName}
            disabled={actionPending}
            onChange={(e) => {
              setModelName(e.target.value);
              onDraftChanged();
            }}
            className="rounded border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="flex flex-col gap-1">
          API 키 {initial.hasApiKey && <span className="text-xs text-green-700">(설정됨)</span>}
          <input
            type="password"
            value={apiKey}
            disabled={actionPending}
            onChange={(e) => {
              setApiKey(e.target.value);
              onDraftChanged();
            }}
            placeholder={initial.hasApiKey ? "변경하려면 새 키 입력" : "API 키 입력"}
            className="rounded border border-slate-300 px-3 py-2"
          />
          <span className="text-xs text-slate-500">
            키는 이 기기의 안전한 저장소에만 보관되고 화면에 다시 표시되지 않습니다.
          </span>
        </label>

        {external && (
          <p role="note" className="rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">
            문서의 일부 내용이 선택한 모델 서비스로 전송될 수 있습니다. 환자 식별 정보가
            포함된 문서는 외부로 보내지 않도록 주의하세요.
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <button
            type="submit"
            disabled={actionPending}
            className="rounded-md bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            저장
          </button>
          <button
            type="button"
            onClick={() => {
              onDraftChanged();
              testMutation.mutate();
            }}
            disabled={actionPending}
            className="rounded border border-slate-300 px-4 py-2 hover:bg-slate-50 disabled:opacity-50"
          >
            연결 확인
          </button>
          {initial.hasApiKey && (
            <button
              type="button"
              onClick={() => setConfirmDeleteKey(true)}
              disabled={actionPending}
              className="rounded border border-red-200 px-4 py-2 text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              저장된 API 키 삭제
            </button>
          )}
        </div>

        <p className="text-xs text-slate-500">
          연결 확인은 현재 입력값을 먼저 저장한 뒤 같은 설정으로 검사합니다.
        </p>

        {saveMutation.isError && (
          <p role="alert" className="rounded bg-red-50 px-3 py-2 text-red-700">
            저장하지 못했습니다. 입력값을 확인해 주세요.
          </p>
        )}
        {testMutation.isError && (
          <p role="alert" className="rounded bg-red-50 px-3 py-2 text-red-700">
            설정을 저장하지 못해 연결을 확인하지 못했습니다. 입력값을 확인해 주세요.
          </p>
        )}
        {deleteKeyMutation.isError && (
          <p role="alert" className="rounded bg-red-50 px-3 py-2 text-red-700">
            저장된 API 키를 삭제하지 못했습니다. 다시 시도해 주세요.
          </p>
        )}
      </form>

      <ConfirmDialog
        open={confirmDeleteKey}
        title="저장된 API 키를 삭제할까요?"
        description="삭제하면 새 키를 저장하기 전까지 이 모델 서비스에 연결할 수 없습니다."
        confirmLabel="API 키 삭제"
        onConfirm={() => {
          setConfirmDeleteKey(false);
          onDraftChanged();
          deleteKeyMutation.mutate();
        }}
        onCancel={() => setConfirmDeleteKey(false)}
      />
    </>
  );
}

function ErrorBoxInline({ onRetry }: { onRetry: () => void }) {
  return (
    <div role="alert" className="rounded bg-red-50 px-3 py-2 text-sm text-red-700">
      <p>요약 모델 설정을 불러오지 못했습니다.</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-1.5 rounded border border-red-300 px-3 py-1.5 text-xs hover:bg-red-100"
      >
        다시 시도
      </button>
    </div>
  );
}
