"use client";

import {
  type CitationSource,
  type CitationToken,
  dedupeDisplayedSources,
  tokenizeCitations,
} from "@/lib/citations";

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
  tokens: precomputed,
}: {
  content: string;
  sources: CitationSlots;
  onNavigate: (source: CitationSource) => void;
  /** 호출자가 이미 토크나이즈했다면 재사용한다 — 같은 인자로 두 번 돌리면
   *  claimCount 기준이 갈라졌을 때 인용 pill과 출처 목록이 어긋난다. */
  tokens?: CitationToken[];
}) {
  const tokens = precomputed ?? tokenizeCitations(content, sources.length);
  return (
    <p className="max-w-[68ch] whitespace-pre-wrap text-[1.0625rem] leading-[1.7] text-slate-900">
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
  // 같은 줄로 보이는 출처는 묶음 안에서 접는다 — 한 주장이 같은 페이지의 블록
  // 여러 개에 근거를 두면 똑같은 "N쪽" 줄이 수십 번 반복된다.
  const visible = groups
    .map((g) => ({ ...g, refs: dedupeDisplayedSources(g.refs) }))
    .filter((g) => g.refs.length > 0);
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
                // 이름의 앞부분은 인용 pill과 같게 유지하고("N쪽 근거 보기"), 절 제목·
                // 판독 방법은 뒤에 붙인다 — 같은 페이지의 서로 다른 두 줄이 스크린리더에
                // 똑같은 이름으로 들리면 눈에 보이는 구분이 무의미해진다.
                aria-label={`${s.pageNumber}쪽 근거 보기${
                  s.sectionTitle ? ` · ${s.sectionTitle}` : ""
                }${s.sourceMethod === "ocr" ? " · 스캔 인식" : ""}`}
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
