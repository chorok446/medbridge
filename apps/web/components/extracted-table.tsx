"use client";

import type { ExtractionTable } from "@/types/extraction";

interface Props {
  table: ExtractionTable;
  /** 표 제목을 누르면 원문 위치로 이동한다. */
  onLocate: () => void;
}

/** 첫 글자가 숫자면 수치 칸으로 본다 — "12.5", "100mg", "3~5회" 모두 오른쪽 정렬이 읽기 좋다. */
function isMeasure(value: string): boolean {
  return /^[\d.,+\-−]/.test(value.trim());
}

function cellText(value: string | null): string {
  // 셀 안의 줄바꿈은 pymupdf가 원문 줄바꿈을 그대로 넘긴 것이다. 좁은 칸에서 그대로
  // 두면 한 칸이 세로로 길어져 행 높이가 들쭉날쭉해진다.
  return (value ?? "").replace(/\s*\n\s*/g, " ").trim();
}

/** 추출된 표 하나 — 마크다운 원문이 아니라 진짜 표로 그린다.
 *
 * 이전에는 `to_markdown()` 결과를 `<pre>` 안에 12px 고정폭으로 뿌렸다. 사용자에게
 * `| 약물 | 용량 |` 파이프 원문이 그대로 보였는데, DESIGN.md가 이름을 들어 금지한
 * 것이고("마크다운 원문(`| 파이프 |`) 노출 금지") 개발자 도구처럼 보이는 화면이라는
 * PRODUCT.md anti-reference에도 정면으로 걸린다.
 *
 * 좁은 패널에서도 읽히게 하는 장치는 두 가지다: 머리글 행 고정과 첫 열 고정. 표를
 * 옆으로 밀어도 "이 값이 무슨 항목의 무슨 열인지"를 잃지 않는다.
 */
export function ExtractedTable({ table, onLocate }: Props) {
  // 첫 행은 내용이 비어 있어도 머리글 자리를 지킨다. 빈 행을 걷어내며 첫 행까지
  // 지우면, 병합 머리글을 [null, null, null]로 넘기는 표에서 첫 자료 행이 <thead>로
  // 승격되고 스크린리더는 그 열의 모든 값을 "심박수" 아래에 있는 것으로 읽는다.
  const [rawHeader, ...rawBody] = table.cells;
  const headerRow = rawHeader ?? [];
  const bodyRows = rawBody.filter((row) => row.some((c) => cellText(c)));
  // 읽어내기 자체가 실패한 표에만 경고를 붙인다. 이전에는 `confidence < 0.8`로 판정했는데
  // 추출기가 성공한 표에 0.7을 상수로 박아 넣으므로(engine.py) 모든 표에 빠짐없이 떴다.
  // 항상 뜨는 경고는 아무것도 알려주지 않으면서 표 하나하나의 높이만 잡아먹는다.
  // 규칙 기반 추출이라는 일반적인 한계는 목록 맨 위에 한 번만 적는다.
  // 격자는 있는데 칸이 전부 빈 표(머리글까지 null)도 읽어내지 못한 것으로 본다 —
  // 빈 머리글 한 줄만 그려놓으면 실패를 설명하는 대신 흐린다.
  const hasContent = [headerRow, ...bodyRows].some((row) => row.some((c) => cellText(c)));
  const failed = table.extractionStatus !== "extracted" || !hasContent;
  const label = `${table.pageNumber}쪽의 표 ${table.tableIndex + 1}`;

  // 정렬은 셀이 아니라 열 단위로 정한다. 셀마다 따로 판정하면 같은 열에서 "20mg"은
  // 오른쪽, "정맥/경구"는 왼쪽으로 흩어지고, 머리글만 늘 왼쪽이라 값과 어긋난다.
  const alignRight = headerRow.map((_, c) => {
    const values = bodyRows.map((row) => cellText(row[c] ?? null)).filter(Boolean);
    if (values.length === 0) return false;
    return values.filter(isMeasure).length / values.length >= 0.6;
  });

  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-4 pt-3 pb-2">
        <button
          type="button"
          onClick={onLocate}
          className="rounded-sm text-sm font-semibold text-blue-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
        >
          {label}
        </button>
        {!failed && (
          <span className="text-xs text-slate-500">
            {/* 머리글을 뺀 실제 자료 행 수 — API의 rowCount는 머리글을 포함해 한 행 많다.
                읽어내지 못한 표에 "0행 × 0열"을 붙이면 실패를 설명하는 대신 흐린다. */}
            {bodyRows.length}행 × {headerRow.length}열
          </span>
        )}
      </div>

      {failed ? (
        <p className="mx-4 mb-4 rounded bg-amber-50 px-3 py-2 text-sm leading-relaxed text-amber-900">
          이 표는 내용을 읽어내지 못했어요. 제목을 눌러 원문에서 확인해 주세요.
        </p>
      ) : (
        // 표가 칸보다 넓으면 표만 옆으로 밀린다 — 화면 전체가 가로로 흔들리지 않는다.
        //
        // tabIndex/role/aria-label이 함께 필요하다: 표 안에는 초점이 닿는 것이 하나도
        // 없어서(전부 th/td 글자뿐) 이 상자가 초점을 받지 못하면 마우스 없는 사용자는
        // 칸 밖으로 밀려난 열을 영영 볼 수 없다. 넓은 표를 옆으로 밀어 읽는 것이 이
        // 화면의 핵심이라 바로 그 자리에서 막힌다(WCAG 2.1.1). 초점을 받는 요소에는
        // 이름이 있어야 하므로 role="region" + aria-label을 같이 준다.
        <div
          role="region"
          aria-label={label}
          tabIndex={0}
          className="max-h-96 overflow-auto border-t border-slate-200 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-600"
        >
          {/* border-separate를 쓴다 — border-collapse에서는 테두리가 셀이 아니라 표에
              속해서, 고정된 머리글·첫 열을 스크롤하면 그 테두리만 사라진다. */}
          {/* w-max + min-w-full: 칸보다 좁으면 채우고, 넓으면 눌리지 않고 그대로 밀린다.
              w-full만 주면 좁은 패널에서 열이 짓눌려 뒤쪽 열의 글이 여러 줄로 접히고,
              그 열은 화면 밖인데 행 높이만 세 배가 되어 빈 줄만 보였다. */}
          <table className="w-max min-w-full border-separate border-spacing-0 text-sm">
            <caption className="sr-only">{label}</caption>
            <thead>
              <tr>
                {headerRow.map((cell, i) => (
                  <th
                    key={i}
                    scope="col"
                    className={`sticky top-0 whitespace-nowrap border-b border-slate-300 bg-slate-50 px-3 py-2.5 align-bottom font-semibold text-slate-900 ${
                      alignRight[i] ? "text-right" : "text-left"
                    } ${i === 0 ? "left-0 z-30 border-r border-r-slate-200" : "z-20"}`}
                  >
                    {cellText(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bodyRows.map((row, r) => {
                const last = r === bodyRows.length - 1;
                const rule = last ? "" : "border-b border-slate-100";
                return (
                  <tr key={r}>
                    {row.map((cell, c) => {
                      const value = cellText(cell);
                      if (c === 0) {
                        return (
                          <th
                            key={c}
                            scope="row"
                            // 행 머리글은 줄바꿈하지 않는다 — 좁은 칸에서 "푸로세미드"가
                            // "푸로/세미/드" 세 줄로 쪼개져 행 높이만 늘고 읽기 어려웠다.
                            // 대신 표가 옆으로 밀린다(넓게 보기 스위치가 그걸 푼다).
                            className={`sticky left-0 z-10 whitespace-nowrap border-r border-slate-200 bg-white px-3 py-2.5 text-left align-top font-medium text-slate-900 ${rule}`}
                          >
                            {value}
                          </th>
                        );
                      }
                      return (
                        <td
                          key={c}
                          className={`px-3 py-2.5 align-top leading-relaxed text-slate-700 ${rule} ${
                            alignRight[c]
                              ? "whitespace-nowrap text-right tabular-nums"
                              : "text-left"
                          }`}
                        >
                          {value}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
