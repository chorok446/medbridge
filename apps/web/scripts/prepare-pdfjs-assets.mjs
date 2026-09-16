import { cp, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const packageRoot = dirname(require.resolve("pdfjs-dist/package.json"));
const outputRoot = fileURLToPath(new URL("../public/pdfjs/", import.meta.url));

// Next 정적 export와 Tauri 설치본에 동일 버전의 리소스·라이선스를 포함한다.
export async function preparePdfjsAssets(destination = outputRoot) {
  await mkdir(destination, { recursive: true });
  for (const entry of ["cmaps", "standard_fonts", "wasm", "LICENSE"]) {
    await cp(join(packageRoot, entry), join(destination, entry), { recursive: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await preparePdfjsAssets();
}
