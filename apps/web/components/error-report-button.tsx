"use client";

import { useState } from "react";
import { saveErrorReport, useIsTauri } from "@/lib/tauri";

/**
 * 오류 정보(진단 zip) 저장 버튼.
 *
 * 오류가 뜬 자리에서 바로 누를 수 있어야 한다. 설정 화면까지 찾아 들어가야 하면
 * 사용자는 대개 그냥 포기하고, 지원 요청에는 "안 돼요"만 남아 원인을 좁힐 수 없다.
 *
 * - `block`: 설정 화면처럼 독립된 영역에 놓을 때
 * - `inline`: 오류 안내 안에 곁들일 때(테두리 없는 텍스트 버튼)
 */
export function ErrorReportButton({
  variant = "block",
  className = "",
}: {
  variant?: "block" | "inline";
  className?: string;
}) {
  const desktop = useIsTauri();
  // 성공만 상태로 두면 실패가 "아무 일도 안 일어남"과 같은 모양이 된다. 사용자는
  // 버튼이 죽은 줄 알고 계속 누르고, 정작 지원 요청에 필요한 파일은 없다.
  const [result, setResult] = useState<"idle" | "saved" | "failed">("idle");

  // 브라우저 개발 모드에는 저장할 진단 파일이 없다. 오류 안내 안에서는 아무것도
  // 보여주지 않는 편이 낫다(눌러도 안 되는 버튼을 두지 않는다).
  if (!desktop) {
    return variant === "inline" ? null : (
      <p className={`text-sm text-slate-500 ${className}`}>데스크톱 앱에서 사용할 수 있어요.</p>
    );
  }

  async function save() {
    // 예외와 "false를 돌려준 실패"를 같게 다룬다 — 사용자에게는 둘 다 "파일이
    // 만들어지지 않았다"로 똑같고, 원인은 화면에 내보이지 않는다(경로·디스크 오류).
    const ok = await saveErrorReport().catch(() => false);
    setResult(ok ? "saved" : "failed");
  }

  if (variant === "inline") {
    return (
      <span className={className}>
        <button
          type="button"
          onClick={save}
          className="text-blue-700 underline-offset-2 hover:underline"
        >
          오류 정보 저장
        </button>
        {result === "saved" && (
          <span role="status" className="ml-2 text-green-700">
            저장했어요
          </span>
        )}
        {result === "failed" && (
          <span role="alert" className="ml-2 text-red-700">
            저장하지 못했어요
          </span>
        )}
      </span>
    );
  }

  return (
    <div className={className}>
      <button
        type="button"
        onClick={save}
        className="rounded border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
      >
        오류 정보 저장
      </button>
      {result === "saved" && (
        <p role="status" className="mt-2 text-sm text-green-700">
          오류 정보를 저장했습니다.
        </p>
      )}
      {result === "failed" && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          오류 정보를 저장하지 못했습니다. 저장 공간이 넉넉한지 확인한 뒤 다시 시도해 주세요.
        </p>
      )}
    </div>
  );
}
