// @vitest-environment node
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { preparePdfjsAssets } from "../prepare-pdfjs-assets.mjs";

const require = createRequire(import.meta.url);
const packageRoot = dirname(require.resolve("pdfjs-dist/package.json"));
const digest = (data: Buffer) => createHash("sha256").update(data).digest("hex");

describe("PDF.js 오프라인 리소스 준비", () => {
  it("설치된 패키지의 리소스와 라이선스를 빠짐없이 복사한다", async () => {
    const destination = await mkdtemp(join(tmpdir(), "medbridge-pdfjs-assets-"));
    try {
      // 반복 dev/build에서도 이미 생성된 디렉터리를 사용할 수 있다.
      await preparePdfjsAssets(destination);
      await preparePdfjsAssets(destination);
      expect((await readdir(destination)).sort()).toEqual([
        "LICENSE", "cmaps", "standard_fonts", "wasm",
      ]);
      expect(digest(await readFile(join(destination, "LICENSE")))).toBe(
        digest(await readFile(join(packageRoot, "LICENSE"))),
      );
      for (const folder of ["cmaps", "standard_fonts", "wasm"]) {
        const sourceFiles = (await readdir(join(packageRoot, folder), { recursive: true })).sort();
        expect(sourceFiles.length).toBeGreaterThan(0);
        expect((await readdir(join(destination, folder), { recursive: true })).sort()).toEqual(
          sourceFiles,
        );
        for (const file of sourceFiles) {
          expect(digest(await readFile(join(destination, folder, file)))).toBe(
            digest(await readFile(join(packageRoot, folder, file))),
          );
        }
      }
    } finally {
      // mkdtemp가 반환한 이 테스트 전용 디렉터리만 정리한다.
      expect(dirname(destination)).toBe(resolve(tmpdir()));
      expect(basename(destination)).toMatch(/^medbridge-pdfjs-assets-/);
      await rm(destination, { recursive: true, force: true });
    }
  }, 30_000);
});
