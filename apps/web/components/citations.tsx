"use client";

import { type CitationSource, tokenizeCitations } from "@/lib/citations";

/** 인용 번호 i가 가리키는 출처들. null이면 그 번호는 화면에 내보내지 않는다 —
 *  검증에 실패했거나 출처가 아예 없는 주장이다. 배열 자리는 비워둔 채로 유지한다:
 *  걸러내며 당기면 번호가 밀려 사용자가 누른 인용이 다른 근거로 이동한다. */
export type CitationSlots = (CitationSource[] | null)[];

/** 출처 목록의 한 묶음. number가 null이면 본문에 대응하는 인용 번호가 없다는 뜻이다. */
export interface SourceGroup {
  number: number | null;
  refs: CitationSource[];
}

/** 답변 본문 — 마커 자리에 눌러서 원문으로 갈 수 있는 번호를 넣는다.
 *
 * 문장과 근거가 붙어 있어야 한다. 본문을 통째로 보여준 뒤 근거를 따로 나열하면
 * 사용자가 "이 문장의 근거가 어느 것인지"를 눈으로 다시 맞춰야 한다.
 */
export function CitedText({
  content,
  sources,
  onNavigate,
}: {
  content: string;
  sources: CitationSlots;
  onNavigate: (source: CitationSource) => void;
}) {
  const tokens = tokenizeCitations(content, sources.length);
  return (
    <p className="max-w-[68ch] whitespace-pre-wrap text-[17px] leading-[1.7] text-slate-900">
      {tokens.map((t, i) => {
        if (t.kind === "text") return <span key={i}>{t.text}</span>;
        const refs = sources[t.claimIndex];
        // 보여줄 근거가 없는 번호는 아예 렌더하지 않는다. 눌러도 갈 곳이 없는 번호는
        // 사용자에게 "근거가 있다"는 잘못된 신호만 준다.
        if (!refs || refs.length === 0) return null;
        return (
          <button
            key={i}
            type="button"
            onClick={() => onNavigate(refs[0])}
            aria-label={`${refs[0].pageNumber}쪽 근거 보기`}
            className="mx-0.5 rounded bg-blue-50 px-1.5 align-baseline text-xs font-medium text-blue-700 hover:bg-blue-100 focus:outline-2 focus:outline-offset-2 focus:outline-blue-600"
          >
            {/* 사람이 읽는 번호는 1부터 — 내부 인덱스를 그대로 보이지 않는다 */}
            {t.claimIndex + 1}
          </button>
        );
      })}
    </p>
  );
}

/** 본문 아래 출처 목록. 사용자가 그 근거를 얼마나 믿을지 스스로 판단할 수 있어야 한다.
 *
 * `number`는 본문의 인용 번호와 짝이다. 본문에 마커가 없는 화면(요약)은 null을 주어
 * 번호를 붙이지 않는다 — 대응할 곳이 없는 순번은 사용자를 찾아 헤매게 만든다.
 */
export function SourceList({
  groups,
  onNavigate,
}: {
  groups: SourceGroup[];
  onNavigate: (source: CitationSource) => void;
}) {
  const visible = groups.filter((g) => g.refs.length > 0);
  if (visible.length === 0) return null;
  return (
    <section className="mt-4 border-t border-slate-200 pt-3">
      <h3 className="mb-2 text-xs font-medium text-slate-500">출처</h3>
      <ol className="flex flex-col gap-1">
        {visible.flatMap((g) =>
          g.refs.map((s, i) => (
            <li key={`${g.number}-${s.pageNumber}-${i}`}>
              <button
                type="button"
                onClick={() => onNavigate(s)}
                // 인용 pill과 같은 이름을 쓴다 — 같은 곳으로 가는 두 버튼의 이름이 다르면
                // 스크린리더 사용자에게는 서로 다른 기능으로 들린다.
                aria-label={`${s.pageNumber}쪽 근거 보기`}
                className="w-full rounded px-1 py-1 text-left text-sm text-slate-700 hover:bg-slate-50 focus:outline-2 focus:outline-offset-2 focus:outline-blue-600"
              >
                {g.number !== null && i === 0 && (
                  <span className="mr-1.5 text-xs font-medium text-blue-700">{g.number}</span>
                )}
                {s.pageNumber}쪽
                {s.sectionTitle ? ` · ${s.sectionTitle}` : ""}
                {/* 판독 품질을 숨기지 않는다. 다만 'OCR'이 아니라 사람 말로 쓴다. */}
                {s.sourceMethod === "ocr" ? " · 스캔 인식" : ""}
              </button>
            </li>
          )),
        )}
      </ol>
    </section>
  );
}
