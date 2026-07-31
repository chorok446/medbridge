"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ErrorBox } from "@/components/error-box";
import { UpdateManager } from "@/components/update-manager";
import { getProfile, updateProfile } from "@/lib/api/profile";
import { isTauri, saveErrorReport } from "@/lib/tauri";

function ErrorReportButton() {
  const [saved, setSaved] = useState<string | null>(null);
  if (!isTauri()) {
    return <p className="text-sm text-slate-500">데스크톱 앱에서 사용할 수 있어요.</p>;
  }
  return (
    <div>
      <button
        type="button"
        onClick={async () => {
          const ok = await saveErrorReport().catch(() => false);
          setSaved(ok ? "오류 정보를 저장했습니다." : null);
        }}
        className="rounded border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
      >
        오류 정보 저장
      </button>
      {saved && (
        <p role="status" className="mt-2 text-sm text-green-700">
          {saved}
        </p>
      )}
    </div>
  );
}

const LEVELS = [
  { value: 0, label: "의학 입문" },
  { value: 1, label: "기초의학" },
  { value: 2, label: "임상실습" },
  { value: 3, label: "전공자" },
];

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const [saved, setSaved] = useState(false);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["profile"],
    queryFn: getProfile,
  });

  const mutation = useMutation({
    mutationFn: updateProfile,
    onSuccess: () => {
      setSaved(true);
      queryClient.invalidateQueries({ queryKey: ["profile"] });
      setTimeout(() => setSaved(false), 3000);
    },
  });

  if (isLoading) {
    return (
      <div className="mx-auto max-w-lg px-4 py-12 text-center text-sm text-slate-500">
        설정을 불러오는 중…
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="mx-auto max-w-lg px-4 py-8">
        <ErrorBox message="설정을 불러오지 못했습니다." onRetry={() => refetch()} />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-lg px-4 py-8">
      <h1 className="mb-6 text-2xl font-bold">앱 설정</h1>

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-semibold">내 정보</h2>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            const form = new FormData(e.currentTarget);
            mutation.mutate({
              displayName: String(form.get("displayName") ?? "").trim() || data.displayName,
              studyLevel: Number(form.get("studyLevel")),
            });
          }}
        >
          <label className="flex flex-col gap-1 text-sm">
            표시 이름
            <input
              name="displayName"
              defaultValue={data.displayName}
              maxLength={100}
              className="rounded border border-slate-300 px-3 py-2.5"
            />
          </label>
          <fieldset className="flex flex-col gap-2 text-sm">
            <legend className="mb-1">학습 수준 — 설명의 난이도가 달라져요</legend>
            {LEVELS.map((level) => (
              <label key={level.value} className="flex items-center gap-2">
                <input
                  type="radio"
                  name="studyLevel"
                  value={level.value}
                  defaultChecked={data.studyLevel === level.value}
                  className="h-4 w-4"
                />
                {level.label}
              </label>
            ))}
          </fieldset>
          {mutation.isError && (
            <p role="alert" className="rounded bg-red-50 px-3 py-2 text-sm text-red-700">
              설정을 저장하지 못했습니다. 다시 시도해 주세요.
            </p>
          )}
          {saved && (
            <p role="status" className="rounded bg-green-50 px-3 py-2 text-sm text-green-800">
              저장되었습니다.
            </p>
          )}
          <button
            type="submit"
            disabled={mutation.isPending}
            className="rounded bg-blue-600 px-4 py-2.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            저장
          </button>
        </form>
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-2 font-semibold">업데이트</h2>
        <UpdateManager />
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-2 font-semibold">문제 해결</h2>
        <p className="mb-3 text-sm text-slate-500">
          문제가 반복되면 오류 정보를 파일로 저장해 개발자에게 전달할 수 있어요. 학습자료
          내용은 포함되지 않습니다.
        </p>
        <ErrorReportButton />
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-2 font-semibold">백업·복원</h2>
        <p className="mb-3 text-sm text-slate-500">
          학습자료를 안전하게 보관하고 되돌리는 기능은 다음 업데이트에서 제공됩니다.
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            disabled
            title="다음 업데이트에서 제공됩니다"
            className="cursor-not-allowed rounded border border-slate-200 px-4 py-2 text-sm text-slate-400"
          >
            백업 만들기
          </button>
          <button
            type="button"
            disabled
            title="다음 업데이트에서 제공됩니다"
            className="cursor-not-allowed rounded border border-slate-200 px-4 py-2 text-sm text-slate-400"
          >
            백업에서 복원
          </button>
        </div>
      </section>
    </div>
  );
}
