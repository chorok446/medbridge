"use client";

import { useEffect, useId, useRef, useState } from "react";

/** 대화상자 안에서 초점을 잡아 둔다 — Tab이 뒷배경으로 새지 않게.
 *
 * 초점이 밖으로 나가면 사용자는 잠긴 줄 알았던 화면을 조작하게 되고, 스크린리더는
 * 대화상자 밖 내용을 계속 읽어 무엇이 열려 있는지 잃는다. focus-trap 라이브러리를
 * 들이지 않는 이유는 이 앱이 다이얼로그를 한 번에 하나만 띄우고, 그 안의 초점
 * 대상이 버튼 두세 개로 끝나기 때문이다.
 */
function useFocusTrap(open: boolean, onEscape: () => void) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const root = ref.current;
    if (!root) return;

    // 닫은 뒤 초점을 원래 자리로 돌려준다. 안 그러면 초점이 <body>로 떨어져,
    // 키보드 사용자는 목록 맨 위부터 다시 Tab을 눌러 내려와야 한다.
    const previous = document.activeElement as HTMLElement | null;

    function focusables(): HTMLElement[] {
      return Array.from(
        root!.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      );
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onEscape();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus?.();
    };
  }, [open, onEscape]);

  return ref;
}

/** 화면을 덮는 배경 + 가운데 종이 한 장. */
function Shell({
  labelId,
  children,
  dialogRef,
}: {
  labelId: string;
  children: React.ReactNode;
  dialogRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={labelId}
        className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-5"
      >
        {children}
      </div>
    </div>
  );
}

interface ConfirmProps {
  open: boolean;
  title: string;
  description?: string;
  confirmLabel: string;
  /** 되돌릴 수 없는 동작이면 true — 확인 버튼이 빨간색이 된다. */
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** 되돌릴 수 없는 동작을 확인받는다 — `window.confirm`을 대신한다.
 *
 * DESIGN.md: "시스템 네이티브 confirm/prompt로 앱의 목소리를 끊지 않는다."
 * OS 대화상자는 앱의 글꼴·색·말투가 하나도 적용되지 않아, 조용한 공부방 화면
 * 한가운데서 갑자기 다른 프로그램이 끼어든 것처럼 보인다. 브라우저·OS마다 모양이
 * 달라 같은 동작이 기기마다 다르게 느껴지는 문제도 있다.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  destructive = true,
  onConfirm,
  onCancel,
}: ConfirmProps) {
  const labelId = useId();
  const ref = useFocusTrap(open, onCancel);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) cancelRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <Shell labelId={labelId} dialogRef={ref}>
      <p id={labelId} className="font-semibold text-slate-900">
        {title}
      </p>
      {description && (
        <p className="mt-1.5 text-sm leading-relaxed text-slate-600">{description}</p>
      )}
      <div className="mt-4 flex justify-end gap-2">
        {/* 취소가 먼저 온다 — 초점이 여기서 시작해야 습관적인 Enter가 삭제를 부르지 않는다. */}
        <button
          ref={cancelRef}
          type="button"
          onClick={onCancel}
          className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
        >
          취소
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className={`rounded-md px-4 py-2 text-sm font-medium text-white focus-visible:outline-2 focus-visible:outline-offset-2 ${
            destructive
              ? "bg-red-600 hover:bg-red-700 focus-visible:outline-red-600"
              : "bg-blue-600 hover:bg-blue-700 focus-visible:outline-blue-600"
          }`}
        >
          {confirmLabel}
        </button>
      </div>
    </Shell>
  );
}

interface PromptProps {
  open: boolean;
  title: string;
  label: string;
  defaultValue: string;
  confirmLabel: string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}

/** 짧은 글 한 줄을 받는다 — `window.prompt`를 대신한다.
 *
 * `window.prompt`는 빈 문자열도 그대로 돌려줘서 호출부가 매번 걸러야 했다. 여기서는
 * 빈 값이면 확인 버튼이 눌리지 않아, "왜 아무 일도 안 일어나지"가 아니라 막혔다는
 * 사실이 화면에 보인다.
 */
export function PromptDialog(props: PromptProps) {
  // 닫혀 있으면 아예 마운트하지 않는다. 열 때마다 새 인스턴스가 되므로 입력값이
  // defaultValue에서 시작하고, 되돌리는 effect가 필요 없다 — effect에서 setState를
  // 부르면 열자마자 한 번 더 렌더되고, 그 사이 한 프레임 동안 지난 이름이 보인다.
  if (!props.open) return null;
  return <PromptBody {...props} />;
}

function PromptBody({
  label,
  title,
  defaultValue,
  confirmLabel,
  onConfirm,
  onCancel,
}: PromptProps) {
  const labelId = useId();
  const inputId = useId();
  const ref = useFocusTrap(true, onCancel);
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState(defaultValue);

  useEffect(() => {
    // 초점 이동은 DOM을 만지는 일이라 effect가 맞는 자리다(상태 변경이 아니다).
    // 이름 전체를 선택해 둔다 — 대개 통째로 새로 쓰지 일부만 고치지 않는다.
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const trimmed = value.trim();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (trimmed) onConfirm(trimmed);
  }

  return (
    <Shell labelId={labelId} dialogRef={ref}>
      <form onSubmit={submit}>
        <p id={labelId} className="font-semibold text-slate-900">
          {title}
        </p>
        <label htmlFor={inputId} className="mt-3 block text-sm text-slate-600">
          {label}
        </label>
        <input
          id={inputId}
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
        />
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
          >
            취소
          </button>
          <button
            type="submit"
            disabled={!trimmed}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:opacity-50"
          >
            {confirmLabel}
          </button>
        </div>
      </form>
    </Shell>
  );
}
