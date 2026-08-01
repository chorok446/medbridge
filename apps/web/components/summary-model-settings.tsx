"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  getSummarySettings,
  testSummaryConnection,
  updateSummarySettings,
} from "@/lib/api/settings";
import type { SummaryModelSettings } from "@/lib/api/settings";

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
  const [saved, setSaved] = useState(false);
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
        onSaved={() => {
          setSaved(true);
          setTimeout(() => setSaved(false), 3000);
        }}
      />
      {saved && (
        <p role="status" className="mt-3 rounded bg-green-50 px-3 py-2 text-sm text-green-800">
          저장되었습니다.
        </p>
      )}
    </>
  );
}

function SummaryModelForm({
  initial,
  onSaved,
}: {
  initial: SummaryModelSettings;
  onSaved: () => void;
}) {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = useState(initial.enabled);
  const [endpoint, setEndpoint] = useState(initial.endpoint ?? "");
  const [modelName, setModelName] = useState(initial.modelName ?? "");
  const [isLocal, setIsLocal] = useState(initial.isLocal);
  const [apiKey, setApiKey] = useState("");
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateSummarySettings({
        enabled,
        providerType: "openai_compatible",
        endpoint,
        modelName,
        isLocal,
        // 입력한 경우에만 키를 전송 — 빈 값은 keyring에서 삭제한다
        ...(apiKey ? { apiKey } : {}),
      }),
    onSuccess: () => {
      onSaved();
      setApiKey("");
      void queryClient.invalidateQueries({ queryKey: ["summary-settings"] });
    },
  });

  const testMutation = useMutation({
    mutationFn: testSummaryConnection,
    onSuccess: (r) => setTestResult(r),
  });

  const external = enabled && !isLocal;

  return (
    <form
      className="flex flex-col gap-3 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        saveMutation.mutate();
      }}
    >
      <p className="text-xs text-slate-500">
        요약 기능을 쓰려면 요약 모델을 연결하세요. 연결하지 않아도 문서 열람과 검색은 그대로
        쓸 수 있어요.
      </p>

      <label className="flex items-center gap-2">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        요약 모델 사용
      </label>

      <p className="text-xs text-slate-500">연결 방식: OpenAI 호환 모델 서비스</p>

      <label className="flex items-center gap-2">
        <input type="checkbox" checked={isLocal} onChange={(e) => setIsLocal(e.target.checked)} />내
        컴퓨터/사내에서 실행되는 로컬 모델이에요
      </label>

      <label className="flex flex-col gap-1">
        서비스 주소
        <input
          value={endpoint}
          onChange={(e) => setEndpoint(e.target.value)}
          placeholder={isLocal ? "예: http://localhost:11434/v1" : "예: https://api.example.com/v1"}
          className="rounded border border-slate-300 px-3 py-2"
        />
        <span className="text-xs text-slate-400">
          {isLocal
            ? "로컬 모델은 내 컴퓨터 주소(localhost 또는 127.0.0.1)만 쓸 수 있어요."
            : "외부 모델 주소는 https:// 로 시작해야 안전하게 연결돼요."}
        </span>
      </label>

      <label className="flex flex-col gap-1">
        모델 이름
        <input
          value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          className="rounded border border-slate-300 px-3 py-2"
        />
      </label>

      <label className="flex flex-col gap-1">
        API 키 {initial.hasApiKey && <span className="text-xs text-green-700">(설정됨)</span>}
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={initial.hasApiKey ? "변경하려면 새 키 입력" : "API 키 입력"}
          className="rounded border border-slate-300 px-3 py-2"
        />
        <span className="text-xs text-slate-400">
          키는 이 기기의 안전한 저장소에만 보관되고 화면에 다시 표시되지 않습니다.
        </span>
      </label>

      {external && (
        <p role="note" className="rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">
          문서의 일부 내용이 선택한 모델 서비스로 전송될 수 있습니다. 환자 식별 정보가 포함된
          문서는 외부로 보내지 않도록 주의하세요.
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={saveMutation.isPending}
          className="rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          저장
        </button>
        <button
          type="button"
          onClick={() => testMutation.mutate()}
          disabled={testMutation.isPending}
          className="rounded border border-slate-300 px-4 py-2 hover:bg-slate-50 disabled:opacity-50"
        >
          연결 확인
        </button>
      </div>

      {saveMutation.isError && (
        <p role="alert" className="rounded bg-red-50 px-3 py-2 text-red-700">
          저장하지 못했습니다. 입력값을 확인해 주세요.
        </p>
      )}
      {testResult && (
        <p
          role="status"
          className={`rounded px-3 py-2 ${
            testResult.ok ? "bg-green-50 text-green-800" : "bg-red-50 text-red-700"
          }`}
        >
          {testResult.message}
        </p>
      )}
    </form>
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
