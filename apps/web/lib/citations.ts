/** 답변 산문 안의 인용 마커를 렌더 가능한 토큰으로 쪼갠다.
 *
 * 서버(`verify()`)가 이미 해석되지 않는 마커를 지우지만, 프론트도 범위를 다시 확인한다.
 * 없는 근거를 가리키는 번호를 눌러 엉뚱한 페이지로 이동하는 것이 가장 나쁜 실패다 —
 * 사용자는 그 페이지가 근거라고 믿게 된다.
 */
export type CitationToken =
  | { kind: "text"; text: string }
  | { kind: "citation"; claimIndex: number };

/** 인용 번호가 가리키는 원문 위치. 질문 답변과 요약이 같은 모양으로 넘긴다. */
export interface CitationSource {
  pageNumber: number;
  sectionTitle?: string | null;
  sourceMethod: "digital" | "ocr";
  bbox: [number, number, number, number];
}

// `c` 접두사를 요구해 문서 본문에 흔한 대괄호(`[1]`, `[표 3]`)를 인용으로 오인하지 않는다.
const CITATION_RE = /\[c(\d{1,2})\]/g;

export function tokenizeCitations(content: string, claimCount: number): CitationToken[] {
  const tokens: CitationToken[] = [];
  let cursor = 0;
  for (const match of content.matchAll(CITATION_RE)) {
    const index = Number(match[1]);
    if (index >= claimCount) continue; // 범위 밖 → 본문 글자로 남긴다
    const start = match.index ?? 0;
    if (start > cursor) tokens.push({ kind: "text", text: content.slice(cursor, start) });
    tokens.push({ kind: "citation", claimIndex: index });
    cursor = start + match[0].length;
  }
  if (cursor < content.length) tokens.push({ kind: "text", text: content.slice(cursor) });
  return tokens;
}

export function hasCitations(tokens: CitationToken[]): boolean {
  return tokens.some((t) => t.kind === "citation");
}

/** 화면에 똑같이 보이는 출처를 접는다. 출처 목록에는 페이지·절 제목·판독 방법만
 *  보이므로, 같은 페이지의 다른 블록을 가리키는 출처는 사용자에게 구분할 수 없는
 *  같은 줄의 반복일 뿐이다(긴 문서에서 "132쪽" 수십 줄). 첫 출처를 남겨 이동 위치는
 *  유지한다 — 블록 단위 정밀 이동보다 목록을 읽을 수 있는 것이 먼저다. */
export function dedupeDisplayedSources(refs: CitationSource[]): CitationSource[] {
  const seen = new Set<string>();
  const out: CitationSource[] = [];
  for (const r of refs) {
    const key = `${r.pageNumber}|${r.sectionTitle ?? ""}|${r.sourceMethod}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(r);
  }
  return out;
}
