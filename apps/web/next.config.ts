import type { NextConfig } from "next";

// Tauri에 정적 파일로 포함되는 데스크톱 GUI.
// 서버 기능(리라이트·SSR)은 쓰지 않고, sidecar 주소는 런타임에 lib/api/base.ts가 해석한다.
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
