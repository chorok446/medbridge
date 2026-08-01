import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "../../../"); // apps/web
const SKIP_DIRS = new Set(["node_modules", ".next", "out", "coverage", "__tests__"]);

function collectSourceFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      collectSourceFiles(full, acc);
    } else if (/\.(ts|tsx)$/.test(entry)) {
      acc.push(full);
    }
  }
  return acc;
}

describe("실행 코드에 특정 실기기에서 관측된 포트가 하드코딩되지 않는다", () => {
  it("8998이 apps/web 소스 어디에도 리터럴로 남아있지 않다", () => {
    const offenders = collectSourceFiles(ROOT).filter((f) => readFileSync(f, "utf-8").includes("8998"));
    expect(offenders).toEqual([]);
  });

  it("sidecar 주소를 만드는 곳은 base.ts 하나뿐이고, 개발용 기본값(8765)만 문서화된 예외로 허용한다", () => {
    const hits = collectSourceFiles(ROOT).filter((f) => {
      if (f.replaceAll("\\", "/").endsWith("lib/api/base.ts")) return false;
      const text = readFileSync(f, "utf-8");
      return /127\.0\.0\.1:\d+/.test(text);
    });
    expect(hits).toEqual([]);
  });
});
