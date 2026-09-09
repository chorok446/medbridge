// @vitest-environment node
import { fileURLToPath } from "node:url";
import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

const eslint = new ESLint({
  cwd: fileURLToPath(new URL("../../", import.meta.url)),
});

describe("생성된 PDF.js 리소스의 린트 경계", () => {
  it("패키지에서 복사한 PDF.js JavaScript만 제외한다", async () => {
    expect(await eslint.isPathIgnored("public/pdfjs/wasm/openjpeg_nowasm_fallback.js"))
      .toBe(true);
    expect(await eslint.isPathIgnored("public/pdfjs/wasm/quickjs-eval.js"))
      .toBe(true);
  }, 30_000);

  it("앱 소스와 리소스 준비 스크립트 및 다른 공개 스크립트는 검사한다", async () => {
    for (const path of [
      "components/pdf-viewer.tsx",
      "scripts/prepare-pdfjs-assets.mjs",
      "public/custom-script.js",
    ]) {
      expect(await eslint.isPathIgnored(path)).toBe(false);
    }
  });
});
