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
